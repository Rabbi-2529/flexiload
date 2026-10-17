from dataclasses import asdict
import uuid

from django.conf import settings
from django.contrib import admin
from django.contrib import messages
from django.shortcuts import redirect
from django.urls import path
from django.utils import timezone

from .forms import AdminTopupForm, ModemConnectForm, SimProfileAdminForm
from .modem import (
    ModemError,
    detect_serial_ports,
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


@admin.register(SimProfile)
class SimProfileAdmin(admin.ModelAdmin):
    form = SimProfileAdminForm
    readonly_fields = (
        "sim_number",
        "sim_verified_at",
        "sim_verification_response",
    )
    change_list_template = "admin/topups/simprofile/change_list.html"
    list_display = (
        "name",
        "owner",
        "operator",
        "sim_number",
        "port",
        "sim_verified_at",
        "is_active",
        "updated_at",
    )
    list_filter = ("operator", "is_active")
    search_fields = ("name", "owner__username", "port")

    def get_urls(self):
        custom_urls = [
            path(
                "scan/",
                self.admin_site.admin_view(self.scan_modems),
                name="topups_simprofile_scan",
            ),
            path(
                "connect/",
                self.admin_site.admin_view(self.connect_modem),
                name="topups_simprofile_connect",
            ),
            path(
                "topup/",
                self.admin_site.admin_view(self.send_topup),
                name="topups_simprofile_topup",
            ),
            path(
                "<int:profile_id>/verify/",
                self.admin_site.admin_view(self.verify_profile),
                name="topups_simprofile_verify",
            ),
        ]
        return custom_urls + super().get_urls()

    def changelist_view(self, request, extra_context=None):
        scan_results = request.session.get("modem_scan_results", [])
        context = {
            "scan_results": scan_results,
            "connected_profiles": SimProfile.objects.select_related("owner").all(),
            "recent_transactions": TopupTransaction.objects.select_related(
                "user",
                "sim_profile",
            )[:20],
            "connect_form": ModemConnectForm(),
            "topup_form": AdminTopupForm(user=request.user),
            "modem_backend": settings.MODEM_BACKEND,
            "title": "Modem control",
        }
        if extra_context:
            context.update(extra_context)
        return super().changelist_view(request, extra_context=context)

    def scan_modems(self, request):
        if request.method != "POST":
            return redirect("admin:topups_simprofile_changelist")
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
        return redirect("admin:topups_simprofile_changelist")

    def connect_modem(self, request):
        if request.method != "POST":
            return redirect("admin:topups_simprofile_changelist")

        form = ModemConnectForm(request.POST)
        scanned = {
            item["device"]: item
            for item in request.session.get("modem_scan_results", [])
            if item.get("responsive")
        }
        if not form.is_valid():
            messages.error(request, "Enter a valid operator and 4 to 12 digit PIN.")
            return redirect("admin:topups_simprofile_changelist")

        port = form.cleaned_data["port"]
        if port not in scanned:
            messages.error(request, "Scan again and select an AT-responsive port.")
            return redirect("admin:topups_simprofile_changelist")

        detected = scanned[port]
        operator = form.cleaned_data["operator"]
        detected_operator = detected.get("operator")
        if not operator_matches_network(
            operator,
            detected_operator,
            detected.get("operator_name", ""),
        ):
            messages.error(
                request,
                "Selected operator does not match the network detected on this port.",
            )
            return redirect("admin:topups_simprofile_changelist")

        name = form.cleaned_data["name"] or f"{operator.title()} {port}"
        profile = SimProfile.objects.filter(owner=request.user, port=port).first()
        if profile is None:
            profile = SimProfile(owner=request.user, port=port)
        profile.name = name
        profile.operator = operator
        try:
            sim_number, response = verify_sim_number(port, operator)
        except ModemError as exc:
            messages.error(
                request,
                f"{port} answered AT commands but SIM verification failed: {exc}",
            )
            return redirect("admin:topups_simprofile_changelist")

        profile.sim_number = sim_number
        profile.sim_verified_at = timezone.now()
        profile.sim_verification_response = response
        profile.is_active = True
        profile.set_pin(form.cleaned_data["pin"])
        profile.save()
        messages.success(
            request,
            f"{name} connected on {port}; SIM number verified as {sim_number}.",
        )
        return redirect("admin:topups_simprofile_changelist")

    def verify_profile(self, request, profile_id):
        if request.method != "POST":
            return redirect("admin:topups_simprofile_changelist")
        try:
            profile = SimProfile.objects.get(pk=profile_id)
            sim_number, response = verify_sim_number(
                profile.port or settings.MODEM_PORT,
                profile.operator,
            )
        except SimProfile.DoesNotExist:
            messages.error(request, "SIM profile not found.")
        except ModemError as exc:
            SimProfile.objects.filter(pk=profile_id).update(
                sim_number="",
                sim_verified_at=None,
                sim_verification_response=str(exc),
            )
            messages.error(request, f"SIM verification failed: {exc}")
        else:
            profile.sim_number = sim_number
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
            messages.success(
                request,
                f"{profile.name} verified on {profile.port}: {sim_number}",
            )
        return redirect("admin:topups_simprofile_changelist")

    def send_topup(self, request):
        if request.method != "POST":
            return redirect("admin:topups_simprofile_changelist")

        form = AdminTopupForm(request.POST, user=request.user)
        if not form.is_valid():
            request.session["topup_form_errors"] = form.errors.get_json_data()
            messages.error(request, "Top-up was not sent. Check the submitted fields.")
            return redirect("admin:topups_simprofile_changelist")

        transaction, _ = execute_topup(
            request.user,
            {
                "sim_profile": form.cleaned_data["sim_profile"],
                "product": form.cleaned_data["product"],
                "phone_number": form.cleaned_data["phone_number"],
                "amount": form.cleaned_data["amount"],
                "pin": form.cleaned_data.get("pin"),
                "idempotency_key": f"admin-{uuid.uuid4()}",
            },
        )
        request.session["last_topup_transaction"] = str(transaction.pk)
        if transaction.status == TransactionStatus.SUCCESS:
            messages.success(
                request,
                f"Top-up submitted successfully. Transaction: {transaction.pk}",
            )
        else:
            messages.error(
                request,
                f"Top-up failed: {transaction.error_message or transaction.modem_response}",
            )
        return redirect("admin:topups_simprofile_changelist")

@admin.register(TopupTransaction)
class TopupTransactionAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "user",
        "operator",
        "product",
        "phone_number",
        "amount",
        "status",
        "retry_count",
        "last_retried_at",
        "created_at",
    )
    list_filter = ("operator", "product", "status", "created_at")
    search_fields = ("id", "user__username", "phone_number", "idempotency_key")
    readonly_fields = (
        "id",
        "user",
        "operator",
        "product",
        "phone_number",
        "amount",
        "status",
        "modem_response",
        "error_message",
        "idempotency_key",
        "retry_count",
        "last_retried_at",
        "created_at",
        "completed_at",
    )

    def get_readonly_fields(self, request, obj=None):
        readonly = list(super().get_readonly_fields(request, obj))
        # Allow changing sim_profile only when the transaction is not yet successful
        if obj is not None and obj.status == TransactionStatus.SUCCESS:
            readonly.append("sim_profile")
        return readonly

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return True


@admin.register(StoredModemMessage)
class StoredModemMessageAdmin(admin.ModelAdmin):
    list_display = (
        "stored_at",
        "sim_profile",
        "sender",
        "modem_timestamp",
        "status",
    )
    list_filter = ("sim_profile__operator", "status", "stored_at")
    search_fields = ("sender", "text", "modem_timestamp", "sim_profile__sim_number")
    readonly_fields = (
        "sim_profile",
        "fingerprint",
        "modem_index",
        "status",
        "sender",
        "modem_timestamp",
        "text",
        "stored_at",
    )

    def has_add_permission(self, request):
        return False
