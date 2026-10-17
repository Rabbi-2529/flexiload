from dataclasses import asdict
from datetime import date
from decimal import Decimal, InvalidOperation
import hashlib
import logging
import re
import uuid

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import login, logout
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db import transaction as db_transaction
from django.db.models import Max, Q, Sum
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from rest_framework.authtoken.models import Token

from .forms import AdminTopupForm, EmailAuthenticationForm, ModemConnectForm
from .modem import (
    ModemError,
    carrier_response_is_failure,
    carrier_response_is_success,
    classify_sms_result,
    detect_serial_ports,
    extract_carrier_transaction_id,
    extract_sms_transaction_id,
    get_modem,
    normalize_bd_number,
    operator_matches_network,
    verify_sim_number,
)
from .models import (
    SimProfile,
    StoredModemMessage,
    TopupTransaction,
    TransactionStatus,
)
from .services import execute_topup
from .webhooks import send_transaction_webhook


_reconcile_logger = logging.getLogger("topups.transactions")


def dashboard_login(request):
    if request.user.is_authenticated:
        return redirect("dashboard:home")
    form = EmailAuthenticationForm(request, data=request.POST or None)
    if request.method == "POST" and form.is_valid():
        login(request, form.get_user())
        return redirect(request.GET.get("next") or "dashboard:home")
    return render(request, "dashboard/login.html", {"form": form})


@login_required
def dashboard_logout(request):
    if request.method == "POST":
        logout(request)
    return redirect("dashboard:login")


def _profiles_for(user):
    queryset = SimProfile.objects.select_related("owner")
    return queryset if user.is_staff else queryset.filter(owner=user)


def _transactions_for(user):
    queryset = TopupTransaction.objects.select_related("user", "sim_profile")
    return queryset if user.is_staff else queryset.filter(user=user)


@login_required
def home(request):
    transactions = _transactions_for(request.user)
    profiles = _profiles_for(request.user)
    context = {
        "active_page": "home",
        "profile_count": profiles.filter(is_active=True).count(),
        "verified_count": profiles.filter(
            is_active=True,
            sim_verified_at__isnull=False,
            sim_number__gt="",
        ).count(),
        "transaction_count": transactions.count(),
        "success_count": transactions.filter(status=TransactionStatus.SUCCESS).count(),
        "total_amount": transactions.filter(
            status=TransactionStatus.SUCCESS
        ).aggregate(total=Sum("amount"))["total"]
        or 0,
        "recent_transactions": transactions[:8],
        "modem_backend": settings.MODEM_BACKEND,
    }
    return render(request, "dashboard/home.html", context)


@login_required
def modems(request):
    return render(
        request,
        "dashboard/modems.html",
        {
            "active_page": "modems",
            "profiles": _profiles_for(request.user),
            "scan_results": request.session.get("modem_scan_results", []),
            "modem_backend": settings.MODEM_BACKEND,
        },
    )


@login_required
def scan_modems(request):
    if request.method == "POST":
        try:
            results = [asdict(item) for item in detect_serial_ports()]
            request.session["modem_scan_results"] = results
            responsive = sum(item["responsive"] for item in results)
            messages.success(
                request,
                f"Scanned {len(results)} ports; {responsive} answered AT commands.",
            )
        except ModemError as exc:
            messages.error(request, str(exc))
    return redirect("dashboard:modems")


@login_required
def connect_modem(request):
    if request.method != "POST":
        return redirect("dashboard:modems")
    form = ModemConnectForm(request.POST)
    scanned = {
        item["device"]: item
        for item in request.session.get("modem_scan_results", [])
        if item.get("responsive")
    }
    if not form.is_valid():
        messages.error(request, "Enter a valid operator and 4 to 12 digit PIN.")
        return redirect("dashboard:modems")
    port = form.cleaned_data["port"]
    if port not in scanned:
        messages.error(request, "Scan again and select an AT-responsive port.")
        return redirect("dashboard:modems")
    detected_operator = scanned[port].get("operator")
    network_name = scanned[port].get("operator_name", "")
    operator = form.cleaned_data["operator"]
    if not operator_matches_network(operator, detected_operator, network_name):
        messages.error(request, "Selected operator does not match the detected network.")
        return redirect("dashboard:modems")
    try:
        sim_number, response = verify_sim_number(port, operator)
    except ModemError as exc:
        messages.error(request, f"SIM verification failed on {port}: {exc}")
        return redirect("dashboard:modems")
    profile = SimProfile.objects.filter(owner=request.user, port=port).first()
    if profile is None:
        profile = SimProfile(owner=request.user, port=port)
    profile.name = form.cleaned_data["name"] or f"{operator.title()} {port}"
    profile.operator = operator
    profile.sim_number = sim_number
    profile.sim_verified_at = timezone.now()
    profile.sim_verification_response = response
    profile.is_active = True
    profile.set_pin(form.cleaned_data["pin"])
    profile.save()
    messages.success(request, f"Connected {sim_number} on {port}.")
    return redirect("dashboard:modems")


@login_required
def verify_modem(request, profile_id):
    if request.method != "POST":
        return redirect("dashboard:modems")
    profile = get_object_or_404(_profiles_for(request.user), pk=profile_id)
    try:
        number, response = verify_sim_number(
            profile.port or settings.MODEM_PORT,
            profile.operator,
        )
    except ModemError as exc:
        profile.sim_number = ""
        profile.sim_verified_at = None
        profile.sim_verification_response = str(exc)
        profile.save(
            update_fields=[
                "sim_number",
                "sim_verified_at",
                "sim_verification_response",
                "updated_at",
            ]
        )
        messages.error(request, f"Verification failed: {exc}")
    else:
        profile.sim_number = number
        profile.sim_verified_at = timezone.now()
        profile.sim_verification_response = response
        profile.save(
            update_fields=[
                "sim_number",
                "sim_verified_at",
                "sim_verification_response",
                "updated_at",
            ]
        )
        messages.success(request, f"Verified {number} on {profile.port}.")
    return redirect("dashboard:modems")


@login_required
def topup(request):
    form = AdminTopupForm(request.POST or None, user=request.user)
    transaction = None
    if request.method == "POST" and form.is_valid():
        transaction, _ = execute_topup(
            request.user,
            {
                "sim_profile": form.cleaned_data["sim_profile"],
                "product": form.cleaned_data["product"],
                "phone_number": form.cleaned_data["phone_number"],
                "amount": form.cleaned_data["amount"],
                "pin": form.cleaned_data.get("pin"),
                "idempotency_key": f"dashboard-{uuid.uuid4()}",
            },
        )
        if transaction.status == TransactionStatus.SUCCESS:
            messages.success(request, "Top-up command completed successfully.")
        elif transaction.status == TransactionStatus.PENDING:
            messages.info(
                request,
                "Top-up queued. It will be sent automatically when the SIM is "
                "free; watch this transaction for the result.",
            )
        elif transaction.status == TransactionStatus.PROCESSING:
            messages.warning(
                request,
                "Top-up is under process. Waiting for the carrier confirmation SMS.",
            )
        else:
            messages.error(request, "Top-up failed. Review the complete modem response.")
        return redirect("dashboard:transaction_detail", transaction_id=transaction.pk)
    return render(
        request,
        "dashboard/topup.html",
        {
            "active_page": "topup",
            "form": form,
            "modem_backend": settings.MODEM_BACKEND,
            "transaction": transaction,
        },
    )


def _display_transaction_id(transaction):
    """The carrier transaction ID to show for a transaction.

    Only successful transactions have a real transaction ID; while a recharge
    is pending/processing the only value available is the throwaway USSD token
    (e.g. ATUS=200&DATE=07), and a failed one has none — so we show nothing
    unless the transaction actually succeeded.
    """
    if transaction.status != TransactionStatus.SUCCESS:
        return ""
    resp = transaction.modem_response or ""
    if "SMS CONFIRMATION:" in resp:
        return (
            extract_sms_transaction_id(resp.split("SMS CONFIRMATION:")[-1])
            or extract_carrier_transaction_id(resp)
            or ""
        )
    return extract_carrier_transaction_id(resp) or ""


def _report_queryset(request):
    """Transactions for the report, filtered by the request's GET params."""
    queryset = _transactions_for(request.user).order_by("-created_at")
    filters = {
        "from": (request.GET.get("from") or "").strip(),
        "to": (request.GET.get("to") or "").strip(),
        "status": (request.GET.get("status") or "").strip(),
        "operator": (request.GET.get("operator") or "").strip(),
    }
    if filters["from"]:
        queryset = queryset.filter(created_at__date__gte=filters["from"])
    if filters["to"]:
        queryset = queryset.filter(created_at__date__lte=filters["to"])
    valid_status = {value for value, _ in TransactionStatus.choices}
    if filters["status"] in valid_status:
        queryset = queryset.filter(status=filters["status"])
    if filters["operator"]:
        queryset = queryset.filter(operator=filters["operator"])
    return queryset, filters


@login_required
def transaction_report(request):
    queryset, filters = _report_queryset(request)

    if request.GET.get("download") == "pdf":
        return _download_report_pdf(queryset, filters, request.user)

    totals = {
        "count": queryset.count(),
        "success": queryset.filter(status=TransactionStatus.SUCCESS).count(),
        "failed": queryset.filter(status=TransactionStatus.FAILED).count(),
        "pending": queryset.filter(
            status__in=[TransactionStatus.PENDING, TransactionStatus.PROCESSING]
        ).count(),
        "success_amount": queryset.filter(
            status=TransactionStatus.SUCCESS
        ).aggregate(total=Sum("amount"))["total"]
        or 0,
    }
    rows = list(queryset[:1000])
    for row in rows:
        row.carrier_id = _display_transaction_id(row)

    return render(
        request,
        "dashboard/report.html",
        {
            "active_page": "report",
            "rows": rows,
            "totals": totals,
            "filters": filters,
            "status_choices": TransactionStatus.choices,
            "query_string": request.GET.urlencode(),
        },
    )


def _download_report_pdf(queryset, filters, user):
    try:
        from reportlab.lib import colors
        from reportlab.lib.pagesizes import A4, landscape
        from reportlab.lib.units import mm
        from reportlab.platypus import (
            SimpleDocTemplate,
            Table,
            TableStyle,
            Paragraph,
            Spacer,
        )
        from reportlab.lib.styles import getSampleStyleSheet
    except ImportError:
        return HttpResponse(
            "The PDF library is not installed. Run: pip install reportlab",
            status=500,
            content_type="text/plain",
        )

    import io

    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=landscape(A4),
        title="Transaction Report",
        leftMargin=14 * mm,
        rightMargin=14 * mm,
        topMargin=14 * mm,
        bottomMargin=14 * mm,
    )
    styles = getSampleStyleSheet()
    story = []

    story.append(Paragraph("Transaction Report", styles["Title"]))
    generated = timezone.localtime(timezone.now()).strftime("%Y-%m-%d %H:%M")
    scope = []
    if filters.get("from"):
        scope.append(f"From {filters['from']}")
    if filters.get("to"):
        scope.append(f"To {filters['to']}")
    if filters.get("status"):
        scope.append(f"Status: {filters['status']}")
    if filters.get("operator"):
        scope.append(f"Operator: {filters['operator']}")
    subtitle = " | ".join(scope) if scope else "All transactions"
    story.append(
        Paragraph(
            f"{subtitle}<br/>Generated {generated}", styles["Normal"]
        )
    )
    story.append(Spacer(1, 8))

    rows = list(queryset[:5000])
    success = sum(1 for t in rows if t.status == TransactionStatus.SUCCESS)
    failed = sum(1 for t in rows if t.status == TransactionStatus.FAILED)
    success_amount = sum(
        t.amount for t in rows if t.status == TransactionStatus.SUCCESS
    )
    story.append(
        Paragraph(
            f"Total: {len(rows)} &nbsp;&nbsp; Success: {success} "
            f"&nbsp;&nbsp; Failed: {failed} &nbsp;&nbsp; "
            f"Success amount: BDT {success_amount}",
            styles["Normal"],
        )
    )
    story.append(Spacer(1, 10))

    header = [
        "Date",
        "Destination",
        "Operator",
        "Product",
        "Amount",
        "Status",
        "Transaction ID",
    ]
    data = [header]
    for t in rows:
        carrier_id = _display_transaction_id(t) or "-"
        data.append(
            [
                timezone.localtime(t.created_at).strftime("%Y-%m-%d %H:%M"),
                t.phone_number,
                t.get_operator_display(),
                t.get_product_display(),
                f"BDT {t.amount}",
                t.get_status_display(),
                carrier_id,
            ]
        )

    table = Table(
        data,
        repeatRows=1,
        colWidths=[32 * mm, 30 * mm, 30 * mm, 26 * mm, 24 * mm, 24 * mm, 60 * mm],
    )
    style = TableStyle(
        [
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1f2933")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("FONTSIZE", (0, 0), (-1, -1), 8),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f2f4f7")]),
            ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#cbd2d9")),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("LEFTPADDING", (0, 0), (-1, -1), 4),
            ("RIGHTPADDING", (0, 0), (-1, -1), 4),
            ("TOPPADDING", (0, 0), (-1, -1), 3),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ]
    )
    # Colour the status cell (column 5) by outcome.
    for index, t in enumerate(rows, start=1):
        if t.status == TransactionStatus.SUCCESS:
            style.add("TEXTCOLOR", (5, index), (5, index), colors.HexColor("#0b7a34"))
        elif t.status == TransactionStatus.FAILED:
            style.add("TEXTCOLOR", (5, index), (5, index), colors.HexColor("#b42318"))
        else:
            style.add("TEXTCOLOR", (5, index), (5, index), colors.HexColor("#a15c00"))
    table.setStyle(style)

    if rows:
        story.append(table)
    else:
        story.append(Paragraph("No transactions match this filter.", styles["Normal"]))

    doc.build(story)
    pdf = buffer.getvalue()
    buffer.close()

    stamp = timezone.localtime(timezone.now()).strftime("%Y%m%d_%H%M")
    response = HttpResponse(pdf, content_type="application/pdf")
    response["Content-Disposition"] = (
        f'attachment; filename="transaction_report_{stamp}.pdf"'
    )
    return response


@login_required
def transactions(request):
    queryset = _transactions_for(request.user)
    return render(
        request,
        "dashboard/transactions.html",
        {
            "active_page": "transactions",
            "transactions": queryset,
            "status_choices": TransactionStatus.choices,
        },
    )


def _message_fingerprint(profile, modem_message):
    """Stable identity for a received SMS, used for de-duplication and to
    confirm a message is stored before it is deleted from the modem."""
    fingerprint_source = "\x1f".join(
        [
            str(profile.pk),
            modem_message.sender,
            modem_message.timestamp,
            modem_message.text,
        ]
    )
    return hashlib.sha256(fingerprint_source.encode("utf-8")).hexdigest()


def _archive_modem_messages(profile, modem_messages):
    imported = 0
    existing = 0
    legacy_underprocess = TopupTransaction.objects.filter(
        sim_profile=profile,
        status=TransactionStatus.SUCCESS,
    ).filter(
        Q(modem_response__icontains="underprocess")
        | Q(modem_response__icontains="under process")
    ).exclude(modem_response__icontains="SMS CONFIRMATION:")
    legacy_underprocess.update(
        status=TransactionStatus.PROCESSING,
        completed_at=None,
        error_message=(
            "The carrier accepted the request and is processing it. "
            "Waiting for the confirmation SMS."
        ),
    )
    profile.stored_messages.filter(text__contains="+CMGL:").delete()
    for modem_message in modem_messages:
        fingerprint = _message_fingerprint(profile, modem_message)
        _, created = StoredModemMessage.objects.get_or_create(
            fingerprint=fingerprint,
            defaults={
                "sim_profile": profile,
                "modem_index": modem_message.index,
                "status": modem_message.status,
                "sender": modem_message.sender,
                "operator": profile.operator,
                "modem_timestamp": modem_message.timestamp,
                "text": modem_message.text,
                "carrier_transaction_id": extract_sms_transaction_id(
                    modem_message.text
                ),
                "parsed_status": classify_sms_result(modem_message.text),
            },
        )
        if created:
            imported += 1
        else:
            existing += 1
    updated = _reconcile_archived_messages(profile)
    return imported, existing, updated


# def _message_amount(text):
#     match = re.search(
#         r"(?:\b(?:TK|BDT)\s*([0-9][0-9,]*(?:\.[0-9]{1,2})?)"
#         r"|\b([0-9][0-9,]*(?:\.[0-9]{1,2})?)\s*(?:TK|BDT)\b)",
#         text,
#         re.IGNORECASE,
#     )
#     if not match:
#         return None
#     try:
#         amount = Decimal((match.group(1) or match.group(2)).replace(",", ""))
#     except InvalidOperation:
#         return None
#     return int(amount) if amount == amount.to_integral_value() else None


SEQUENTIAL_OPERATORS = {"grameenphone"}


def _reconcile_transaction_from_message(profile, modem_message):
    """Attach a received SMS to the transaction it belongs to.

    Safe by construction:

    * Status is read from THIS SMS only.
    * A confirmation only applies to a transaction created at/before the SMS
      was received, so a stale SMS can never complete a newer recharge.
    * A SUCCESS SMS carries the recipient number, so it is matched by number;
      it may also correct a transaction that an earlier duplicate/failure SMS
      tentatively failed -- a late success wins (this is the GP case where the
      "Duplicate transaction detected" arrives first and the real success a
      little later).
    * A FAILURE SMS never downgrades a transaction that is already success.
    * A numberless failure (e.g. GP "Duplicate transaction detected. Ref no")
      is applied only when exactly ONE Grameenphone recharge is in flight, so
      it can never be guessed onto the wrong number.
    * Each carrier transaction ID is consumed only once.
    """
    text = modem_message.text or ""
    status = classify_sms_result(text)
    if not status:
        # Not a recharge result (promo, OTP, balance notice, ...).
        return False

    sms_transaction_id = extract_sms_transaction_id(text)
    if sms_transaction_id and TopupTransaction.objects.filter(
        sim_profile=profile,
        modem_response__icontains=sms_transaction_id,
    ).exists():
        return False  # this SMS has already been consumed

    recipient = _message_target_number(text)
    received_at = modem_message.stored_at

    # Only transactions started at/before this SMS was received are candidates.
    pool = list(
        TopupTransaction.objects.filter(
            sim_profile=profile, created_at__lte=received_at
        )
    )
    if recipient:
        pool = [t for t in pool if normalize_bd_number(t.phone_number) == recipient]
    elif profile.operator not in SEQUENTIAL_OPERATORS:
        # A numberless SMS can only be trusted for a strictly sequential
        # operator (Grameenphone). For anyone else we cannot tell which
        # recharge it belongs to, so we leave it alone.
        return False

    processing = sorted(
        [t for t in pool if t.status == TransactionStatus.PROCESSING],
        key=lambda t: t.created_at,
    )

    transaction = None
    if status == "success":
        if processing:
            transaction = processing[0]
        elif recipient:
            # No in-flight recharge for this number: a genuine success SMS may
            # be correcting one we had tentatively marked failed.
            failed = sorted(
                [t for t in pool if t.status == TransactionStatus.FAILED],
                key=lambda t: t.created_at,
                reverse=True,
            )
            transaction = failed[0] if failed else None
    else:  # failed
        if recipient:
            transaction = processing[0] if processing else None
        elif len(processing) == 1:
            # Numberless GP failure and exactly one recharge in flight.
            transaction = processing[0]
        else:
            _reconcile_logger.warning(
                "Numberless failed SMS on %s not applied (%s recharge(s) in "
                "flight): %r",
                profile.port,
                len(processing),
                text[:160],
            )
            return False

    if transaction is None:
        return False
    return _apply_result_to_transaction(
        transaction, text, status, sms_transaction_id
    )


def _apply_result_to_transaction(transaction, text, status, sms_transaction_id):
    # Never downgrade a delivered (success) transaction, and don't re-fail one
    # that is not currently processing.
    if status == "success" and transaction.status == TransactionStatus.SUCCESS:
        return False
    if status == "failed" and transaction.status != TransactionStatus.PROCESSING:
        return False

    # Keep only the most recent confirmation on the record.
    base = transaction.modem_response.split("SMS CONFIRMATION:")[0].rstrip()
    transaction.modem_response = f"{base}\n\nSMS CONFIRMATION:\n{text}".strip()

    if status == "success":
        transaction.status = TransactionStatus.SUCCESS
        transaction.error_message = ""
    else:
        transaction.status = TransactionStatus.FAILED
        error_match = re.search(
            r"Failed due to\s*(\d+)\s*:\s*([^.]+)", text, re.IGNORECASE
        )
        transaction.error_message = (
            f"Failed due to {error_match.group(1)}: {error_match.group(2).strip()}"
            if error_match
            else text[:200]
        )

    transaction.completed_at = timezone.now()
    with db_transaction.atomic():
        transaction.save(
            update_fields=[
                "status",
                "modem_response",
                "error_message",
                "completed_at",
            ]
        )
    # The webhook is sent automatically by the model's status-change signal.
    _reconcile_logger.info(
        "Reconciled transaction=%s number=%s -> %s (sms_txn_id=%s)",
        transaction.id,
        transaction.phone_number,
        transaction.status,
        sms_transaction_id or "-",
    )
    return True


def _reconcile_archived_messages(profile):
    updated = 0
    # Oldest first so sequential (FIFO) operators such as Grameenphone are
    # matched in the same order the recharges were sent.
    for modem_message in profile.stored_messages.order_by("stored_at", "id"):
        if _reconcile_transaction_from_message(profile, modem_message):
            updated += 1
    return updated

def _message_target_number(text):
    """Extract the recipient Bangladesh mobile number from an SMS body.

    Returns the canonical 01XXXXXXXXX form, handling 017XXXXXXXX,
    88017XXXXXXXX and +88017XXXXXXXX, or "" when the message has no such
    number (so the caller can avoid guessing).
    """
    if not text:
        return ""
    match = re.search(r"(?<!\d)(?:\+?88)?0?1[3-9]\d{8}(?!\d)", text)
    return normalize_bd_number(match.group(0)) if match else ""


def _message_amount(text):
    """Extract amount from SMS text"""
    # Look for BDT or TK followed by amount
    match = re.search(
        r"(?:\b(?:TK|BDT)\s*([0-9][0-9,]*(?:\.[0-9]{1,2})?)"
        r"|\b([0-9][0-9,]*(?:\.[0-9]{1,2})?)\s*(?:TK|BDT)\b)",
        text,
        re.IGNORECASE,
    )
    if not match:
        return None
    try:
        amount_str = (match.group(1) or match.group(2)).replace(",", "")
        amount = Decimal(amount_str)
    except (InvalidOperation, ValueError):
        return None
    return int(amount) if amount == amount.to_integral_value() else None

def _message_date_value(modem_message):
    value = modem_message.modem_timestamp.strip()
    match = re.match(r"(\d{4})/(\d{2})/(\d{2})", value)
    if match:
        year, month, day = (int(part) for part in match.groups())
    else:
        match = re.match(r"(\d{2})/(\d{2})/(\d{2})", value)
        if not match:
            return timezone.localtime(modem_message.stored_at).date().isoformat()
        short_year, month, day = (int(part) for part in match.groups())
        year = 2000 + short_year
    try:
        return date(year, month, day).isoformat()
    except ValueError:
        return timezone.localtime(modem_message.stored_at).date().isoformat()


def _message_rows(profile, user, limit=None):
    if limit is None:
        limit = getattr(settings, "MODEM_MESSAGES_PAGE_LIMIT", 300)

    transactions = list(
        _transactions_for(user).filter(sim_profile=profile)[:500]
    )

    # Build lookup maps once so each message is an O(1) dict lookup instead of
    # scanning all transactions. First transaction per key wins, matching the
    # old "first match in the list" behaviour.
    by_number = {}
    by_carrier = {}
    for transaction in transactions:
        number = normalize_bd_number(transaction.phone_number)
        if number and number not in by_number:
            by_number[number] = transaction
        cid = extract_carrier_transaction_id(transaction.modem_response)
        if cid:
            cid = cid.lower()
            if cid not in by_carrier:
                by_carrier[cid] = transaction

    rows = []
    # Model default ordering is newest-first; only render the most recent
    # `limit` messages per SIM so the page stays fast with large inboxes.
    for modem_message in profile.stored_messages.all()[:limit]:
        target_number = _message_target_number(modem_message.text)
        carrier_id = extract_carrier_transaction_id(modem_message.text)

        matched_transaction = None
        if carrier_id:
            matched_transaction = by_carrier.get(carrier_id.lower())
        if matched_transaction is None and target_number:
            matched_transaction = by_number.get(target_number)

        rows.append(
            {
                "message": modem_message,
                "target_number": target_number,
                "carrier_transaction_id": carrier_id,
                "transaction": matched_transaction,
                "message_date": _message_date_value(modem_message),
            }
        )
    return rows


@login_required
def modem_messages(request):
    inboxes = []
    profiles = list(
        _profiles_for(request.user)
        .filter(is_active=True, port__gt="")
        .order_by("name")
    )
    profiles.sort(key=lambda profile: profile.operator != "grameenphone")
    for profile in profiles:
        # NOTE: reconciliation (matching SMS to transactions) is done by the
        # modem poller when messages arrive, and by the Fetch/Sync buttons.
        # The page only DISPLAYS stored messages, so it must not re-reconcile
        # on every view -- that was the main cause of the slow load.
        inboxes.append(
            {
                "profile": profile,
                "messages": _message_rows(profile, request.user),
                "error": "",
            }
        )
    message_version = (
        StoredModemMessage.objects.filter(sim_profile__in=profiles).aggregate(
            latest_id=Max("id")
        )["latest_id"]
        or 0
    )
    return render(
        request,
        "dashboard/modem_messages.html",
        {
            "active_page": "messages",
            "inboxes": inboxes,
            "message_total": sum(len(inbox["messages"]) for inbox in inboxes),
            "modem_backend": settings.MODEM_BACKEND,
            "message_version": message_version,
            "message_poll_interval_seconds": max(
                5,
                settings.MODEM_SMS_POLL_INTERVAL,
            ),
        },
    )


@login_required
def fetch_all_modem_messages(request):
    if request.method != "POST":
        return redirect("dashboard:messages")

    modem = get_modem()
    imported = 0
    existing = 0
    failed = []
    profiles = list(
        _profiles_for(request.user).filter(is_active=True, port__gt="")
    )

    for profile in profiles:
        try:
            modem_messages = modem.read_messages(profile.port)
            new_count, existing_count, _updated = _archive_modem_messages(
                profile,
                modem_messages,
            )
            imported += new_count
            existing += existing_count
        except ModemError as exc:
            failed.append(f"{profile.port}: {exc}")

    if imported or existing:
        messages.success(
            request,
            f"Fetch complete: {imported} new message(s), "
            f"{existing} already archived.",
        )
    elif not failed:
        messages.info(request, "Fetch complete: no SMS messages were found.")

    if failed:
        messages.error(
            request,
            "Could not fetch from " + " | ".join(failed),
        )
    return redirect("dashboard:messages")


@login_required
def sync_modem_messages(request):
    if request.method != "POST":
        return JsonResponse(
            {"error": "POST required."},
            status=405,
        )

    modem = get_modem()
    imported = 0
    existing = 0
    errors = []
    profiles = _profiles_for(request.user).filter(is_active=True, port__gt="")

    for profile in profiles:
        try:
            modem_messages = modem.read_messages(profile.port)
            new_count, existing_count, _updated = _archive_modem_messages(
                profile,
                modem_messages,
            )
            imported += new_count
            existing += existing_count
        except ModemError as exc:
            errors.append(
                {
                    "port": profile.port,
                    "message": str(exc),
                }
            )

    return JsonResponse(
        {
            "imported": imported,
            "existing": existing,
            "errors": errors,
            "message_version": (
                StoredModemMessage.objects.filter(
                    sim_profile__in=profiles
                ).aggregate(latest_id=Max("id"))["latest_id"]
                or 0
            ),
        }
    )


@login_required
def clear_modem_messages(request, profile_id):
    if request.method != "POST":
        return redirect("dashboard:messages")
    profile = get_object_or_404(
        _profiles_for(request.user).filter(is_active=True),
        pk=profile_id,
    )
    try:
        get_modem().clear_messages(profile.port or None)
    except ModemError as exc:
        messages.error(request, f"Could not clear messages on {profile.port}: {exc}")
    else:
        deleted, _ = profile.stored_messages.all().delete()
        messages.success(
            request,
            f"Cleared the modem and {deleted} archived message(s) for {profile.port}.",
        )
    return redirect("dashboard:messages")


@login_required
def transaction_detail(request, transaction_id):
    transaction = get_object_or_404(
        _transactions_for(request.user),
        pk=transaction_id,
    )
    if request.method == "POST":
        if not request.user.is_staff:
            raise PermissionDenied
        action = request.POST.get("action")
        if action == "update_status":
            status = request.POST.get("status", "")
            valid_statuses = {value for value, _ in TransactionStatus.choices}
            if status not in valid_statuses:
                messages.error(request, "Select a valid transaction status.")
            else:
                transaction.status = status
                transaction.completed_at = (
                    timezone.now()
                    if status in {TransactionStatus.SUCCESS, TransactionStatus.FAILED}
                    else None
                )
                if status == TransactionStatus.SUCCESS:
                    transaction.error_message = ""
                elif status == TransactionStatus.FAILED and not transaction.error_message:
                    transaction.error_message = "Manually marked as failed."
                transaction.save(
                    update_fields=["status", "completed_at", "error_message"]
                )
                # The status-change signal sends the webhook automatically.
                messages.success(
                    request,
                    f"Status changed to {transaction.get_status_display()}."
                    + (
                        " The webhook is being sent."
                        if status
                        in {TransactionStatus.SUCCESS, TransactionStatus.FAILED}
                        else ""
                    ),
                )
        elif action == "send_webhook":
            if transaction.status not in {
                TransactionStatus.SUCCESS,
                TransactionStatus.FAILED,
            }:
                messages.error(request, "Set a final status before sending the webhook.")
            elif send_transaction_webhook(transaction):
                messages.success(request, "Webhook delivered to Laravel successfully.")
            else:
                messages.error(
                    request,
                    "Webhook was not delivered. Check logs/transactions.log.",
                )
        else:
            messages.error(request, "Unknown transaction action.")
        return redirect("dashboard:transaction_detail", transaction_id=transaction.pk)
    return render(
        request,
        "dashboard/transaction_detail.html",
        {
            "active_page": "transactions",
            "transaction": transaction,
            "status_choices": TransactionStatus.choices,
            "webhook_configured": bool(settings.IGL_WEBHOOK_URL),
        },
    )


@login_required
def api_docs(request):
    token = Token.objects.filter(user=request.user).first()
    return render(
        request,
        "dashboard/api_docs.html",
        {
            "active_page": "api_docs",
            "api_token": token.key if token else "",
        },
    )
