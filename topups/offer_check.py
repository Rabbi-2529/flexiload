"""Offer Check — browse operator offers by DIALING the modem (USSD).

This does NOT call any external aggregator API. It opens the operator's own
recharge USSD session on a connected SIM and walks the interactive package
menu, listing every package it finds as an "offer". Buying an offer re-uses the
normal recharge flow (execute_topup), which for Banglalink already selects the
menu option whose price equals the amount.

Only Banglalink exposes an interactive package menu over USSD, so offer
browsing by dialing is available for Banglalink. Grameenphone / Robi / Airtel
use direct (non-interactive) recharge codes, so there is no menu to browse —
for those the page explains the amount is recharged directly.
"""
import logging
import re
import time

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect, render

from rest_framework.decorators import (
    api_view,
    authentication_classes,
    permission_classes,
)
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework import status as http_status

from .models import SimProfile
from .modem import (
    ModemError,
    extract_carrier_message,
    get_modem,
    menu_option_price,
    parse_ussd_menu_options,
)
from .services import execute_topup
from .ussd import build_ussd


logger = logging.getLogger("topups.transactions")

# The poller holds each COM port only briefly per cycle, so if a dial finds the
# port busy we wait a moment and try again a few times to catch a free gap.
OFFER_DIAL_ATTEMPTS = 4
OFFER_DIAL_RETRY_WAIT = 1.5  # seconds between attempts

# UI operator value -> django operator
OPERATOR_MAP = {
    "grameen": "grameenphone",
    "gp": "grameenphone",
    "grameenphone": "grameenphone",
    "rb": "robi",
    "robi": "robi",
    "at": "airtel",
    "airtel": "airtel",
    "bl": "banglalink",
    "banglalink": "banglalink",
}

# Operators whose recharge USSD opens an interactive package/offer menu we can
# browse by dialing. The others are direct-dial (no menu).
MENU_OPERATORS = {"banglalink"}

OPERATOR_CHOICES = [
    ("grameen", "Grameenphone"),
    ("rb", "Robi"),
    ("at", "Airtel"),
    ("bl", "Banglalink"),
    ("teletalk", "Teletalk"),
]

CATEGORY_LABELS = [
    ("all", "All Offers"),
    ("30days", "30 Days"),
    ("7days", "7 Days"),
    ("data", "Data"),
    ("minutes", "Minutes"),
    ("combo", "Combo"),
    ("social", "Social"),
    ("streaming", "Streaming"),
    ("unlimited", "Unlimited"),
]

# Menu entries that are prompts/navigation, not packages.
_SKIP_OPTION = re.compile(
    r"^(enter|back|next|exit|cancel|default\s+recharge|main\s+menu)\b",
    re.IGNORECASE,
)


def _clean_summary(summary):
    summary = re.sub(r"C-[^\s]*", "", str(summary or "")).strip()
    summary = re.sub(r"\bC\s*\d+\s*Tk\b", "", summary, flags=re.IGNORECASE).strip()
    if "|" in summary:
        summary = summary.split("|", 1)[0].strip()
    return summary or "Special Offer"


def _commission_from_text(body):
    """The commission shown in a menu option, e.g. "C 1TK" / "C-1TK" -> 1.

    Returns an int (taka) or None if the option shows no commission.
    """
    if not body:
        return None
    m = re.search(r"\bC\s*-?\s*(\d+)\s*Tk\b", str(body), re.IGNORECASE)
    return int(m.group(1)) if m else None


def _validity_from_text(text):
    m = re.search(r"(\d+)\s*(day|days|hour|hours|hr|hrs|h)\b", text, re.IGNORECASE)
    if not m:
        return "N/A"
    n = int(m.group(1))
    unit = "Day" if m.group(2).lower().startswith("day") else "Hour"
    return f"{n} {unit}" + ("s" if n > 1 else "")


def _categories(summary, validity):
    s = summary.lower()
    v = str(validity or "").lower()
    cat = {
        "30days": ("30 day" in s or "30days" in s or "30d" in s or "30" in v),
        "7days": ("7 day" in s or "7days" in s or "7d" in s or "7" in v),
        "data": ("gb" in s or "data" in s or "internet" in s or "mb" in s),
        "minutes": ("min" in s or "minute" in s or "call" in s or "voice" in s),
        "social": any(w in s for w in (
            "facebook", "whatsapp", "messenger", "instagram", "imo",
            "viber", "social",
        )),
        "streaming": any(w in s for w in (
            "youtube", "tiktok", "snapchat", "likee", "streaming", "video",
        )),
        "unlimited": "unlimited" in s,
    }
    cat["combo"] = (cat["data"] and cat["minutes"]) or "combo" in s or "both" in s
    if cat["combo"]:
        cat["data"] = True
        cat["minutes"] = True
    return cat


def _browse_strategy():
    """Reply chooser that only pages through the menu (never buys).

    Returns ``"*"`` to go to the next page while the page offers ``*:Next``,
    otherwise ``None`` so the session ends without selecting any package.
    """
    def choose(menu_text):
        options = parse_ussd_menu_options(menu_text)
        if any(key == "*" for key, _ in options):
            return "*"
        return None

    return choose


def _parse_offers_from_session(response, amount):
    """Turn every package option across all menu pages into an offer list."""
    offers = []
    seen = set()
    for key, body in parse_ussd_menu_options(response or ""):
        # Skip navigation / cash / prompt entries.
        if key in ("*",):
            continue
        if key == "0":
            # 0 is almost always "Default Recharge" (plain cash) — not an offer.
            if _SKIP_OPTION.match(body or ""):
                continue
        if not body or _SKIP_OPTION.match(body):
            continue
        summary = _clean_summary(body)
        if summary == "Special Offer" and not menu_option_price(body):
            continue
        dedupe_key = re.sub(r"\s+", " ", summary.lower())
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)

        price = menu_option_price(body)
        commission = _commission_from_text(body)
        validity = _validity_from_text(body)
        offers.append({
            "id": key,
            "amount": price if price is not None else amount,
            "summary": summary,
            "validity": validity,
            "ussd": key,  # the menu key you reply with to buy this package
            "type": "Package",
            "commission": commission if commission is not None else 0,
            "highlighted": price is not None and price == amount,
            "cat": _categories(summary, validity),
        })
    return offers


def fetch_offers(ui_operator, number, amount, user=None):
    """Dial the operator menu and return the packages found as offers.

    Raises ValueError on bad input, RuntimeError on modem/SIM problems.
    """
    ui_operator = (ui_operator or "").strip().lower()
    number = (number or "").strip()
    if not re.fullmatch(r"(?:\+?88)?01[3-9]\d{8}", number):
        raise ValueError("Enter a valid Bangladesh mobile number.")
    try:
        amount = int(float(amount))
    except (TypeError, ValueError):
        raise ValueError("Enter a valid amount.")
    if amount <= 0:
        raise ValueError("Enter a valid amount.")

    django_operator = OPERATOR_MAP.get(ui_operator)
    if django_operator is None:
        raise ValueError("Offer check is not supported for this operator.")

    counts = {key: 0 for key, _ in CATEGORY_LABELS}
    filters = [
        {"key": key, "label": label, "count": 0}
        for key, label in CATEGORY_LABELS
    ]

    if django_operator not in MENU_OPERATORS:
        # No interactive menu to browse for this operator.
        return {
            "offers": [],
            "counts": counts,
            "filters": filters,
            "message": (
                f"{django_operator.title()} does not open a package menu over "
                f"USSD, so there are no offers to browse. The amount you enter "
                f"is recharged directly."
            ),
            "ui_operator": ui_operator,
        }

    # Pick a connected SIM for this operator.
    profile = SimProfile.objects.filter(
        operator=django_operator, is_active=True
    ).exclude(port="")
    if user is not None and not getattr(user, "is_staff", False):
        profile = profile.filter(owner=user)
    profile = profile.order_by("pk").first()
    if profile is None:
        raise RuntimeError(f"No active {django_operator} SIM is connected.")

    try:
        pin = profile.get_pin()
    except Exception as exc:  # noqa: BLE001
        logger.error("Offer check could not read SIM PIN: %s", exc)
        raise RuntimeError("The SIM PIN is not configured for this operator.")

    try:
        code, dcs = build_ussd(django_operator, "flexiload", number, amount, pin)
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(str(exc))

    # Dial and page through the whole menu (_browse_strategy sends "*" / Next
    # until the last page). If the modem port is briefly busy (the poller is
    # reading SMS on it), wait and retry a few times to catch a free gap.
    modem = get_modem()
    result = None
    last_error = None
    for attempt in range(1, OFFER_DIAL_ATTEMPTS + 1):
        try:
            result = modem.send_ussd_session(
                code,
                dcs=dcs,
                port=profile.port or None,
                reply_strategy=_browse_strategy(),
            )
            break
        except ModemError as exc:
            last_error = exc
            logger.warning(
                "Offer dial attempt %s/%s busy for %s: %s",
                attempt, OFFER_DIAL_ATTEMPTS, django_operator, exc,
            )
            if attempt < OFFER_DIAL_ATTEMPTS:
                time.sleep(OFFER_DIAL_RETRY_WAIT)
        except Exception as exc:  # noqa: BLE001
            logger.error("Offer check dial failed for %s: %s", django_operator, exc)
            raise RuntimeError("Could not read the offer menu from the modem. Please try again.")

    if result is None:
        logger.error(
            "Offer check dial failed for %s after %s attempts: %s",
            django_operator, OFFER_DIAL_ATTEMPTS, last_error,
        )
        raise RuntimeError(
            "Could not read the offer menu — the modem line was busy. Please try again."
        )

    offers = _parse_offers_from_session(result.response, amount)

    counts["all"] = len(offers)
    for offer in offers:
        for key, value in offer["cat"].items():
            if value:
                counts[key] += 1
    filters = [
        {"key": key, "label": label, "count": counts.get(key, 0)}
        for key, label in CATEGORY_LABELS
    ]

    message = "" if offers else (
        extract_carrier_message(result.response)
        or "No packages were listed for this number and amount."
    )
    logger.info(
        "Offer check (dial) operator=%s number=%s*** amount=%s offers=%s",
        django_operator, number[:5], amount, len(offers),
    )
    return {
        "offers": offers,
        "counts": counts,
        "filters": filters,
        "message": message,
        "ui_operator": ui_operator,
    }


@login_required
def offer_check_page(request):
    context = {
        "active_page": "offers",
        "operators": OPERATOR_CHOICES,
        "categories": CATEGORY_LABELS,
        "form": {"operator": "bl", "number": "", "amount": "10"},
        "result": None,
        "error": "",
    }
    if request.method == "POST":
        operator = (request.POST.get("operator") or "").strip()
        number = (request.POST.get("number") or "").strip()
        amount = (request.POST.get("amount") or "10").strip()
        context["form"] = {"operator": operator, "number": number, "amount": amount}
        try:
            context["result"] = fetch_offers(operator, number, amount, user=request.user)
        except (ValueError, RuntimeError) as exc:
            context["error"] = str(exc)
    return render(request, "dashboard/offer_check.html", context)


@login_required
def offer_buy(request):
    """Buy an offer by queueing a recharge for the offer amount through the
    existing flow. For Banglalink, dispatch_topup walks the package menu and
    selects the option whose price equals the amount, so no extra logic is
    needed here."""
    if request.method != "POST":
        return redirect("dashboard:offers")

    ui_operator = (request.POST.get("operator") or "").strip().lower()
    number = (request.POST.get("number") or "").strip()
    amount = (request.POST.get("amount") or "").strip()
    product = (request.POST.get("product") or "prepaid").strip()

    django_operator = OPERATOR_MAP.get(ui_operator)
    if django_operator is None:
        messages.error(request, "This operator is not supported for buying.")
        return redirect("dashboard:offers")

    try:
        amount_val = int(float(amount))
    except (TypeError, ValueError):
        messages.error(request, "Invalid offer amount.")
        return redirect("dashboard:offers")

    profile = SimProfile.objects.filter(
        operator=django_operator, is_active=True
    ).exclude(port="")
    if not request.user.is_staff:
        profile = profile.filter(owner=request.user)
    profile = profile.order_by("pk").first()
    if profile is None:
        messages.error(request, f"No active {django_operator} SIM is connected.")
        return redirect("dashboard:offers")

    import uuid
    transaction, _ = execute_topup(
        request.user,
        {
            "sim_profile": profile,
            "product": product if product in {"prepaid", "postpaid"} else "prepaid",
            "phone_number": number,
            "amount": amount_val,
            "idempotency_key": f"offer-{uuid.uuid4()}",
        },
    )
    messages.success(
        request,
        f"Offer queued: ৳{amount_val} to {number}. Watch the transaction for the result.",
    )
    return redirect("dashboard:transaction_detail", transaction_id=transaction.pk)


# Public endpoint: no login/token required (AllowAny, no authentication).
# user=None means fetch_offers won't filter SIMs by owner; it uses any active
# SIM for the operator.
@api_view(["POST"])
@authentication_classes([])
@permission_classes([AllowAny])
def offer_check_api(request):
    operator = (request.data.get("operator") or "").strip()
    number = (request.data.get("number") or "").strip()
    amount = request.data.get("amount") or 10
    try:
        result = fetch_offers(operator, number, amount)
    except (ValueError, RuntimeError) as exc:
        return Response({"error": str(exc)}, status=http_status.HTTP_400_BAD_REQUEST)
    return Response(
        {
            "operator": operator,
            "number": number,
            "amount": amount,
            "count": result["counts"]["all"],
            "offers": result["offers"],
        }
    )
