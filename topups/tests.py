from unittest.mock import patch

from cryptography.fernet import Fernet
from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from .modem import (
    DetectedPort,
    SIM_NUMBER_CODES,
    extract_bangladesh_number,
    normalize_operator,
    ussd_session_is_interactive,
)
from .models import SimProfile, TopupTransaction
from .ussd import build_ussd


TEST_KEY = Fernet.generate_key().decode("ascii")


@override_settings(PIN_ENCRYPTION_KEY=TEST_KEY, MODEM_BACKEND="mock")
class TopupApiTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("agent", password="strong-test-pass")
        self.other = User.objects.create_user("other", password="strong-test-pass")
        token = Token.objects.create(user=self.user)
        self.client = APIClient()
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {token.key}")

        self.sim = SimProfile(owner=self.user, name="GP counter", operator="grameenphone")
        self.sim.set_pin("1234")
        self.sim.save()

    def test_pin_is_encrypted(self):
        self.assertNotIn("1234", self.sim.encrypted_pin)
        self.assertEqual(self.sim.get_pin(), "1234")

    def test_send_topup_with_mock_modem(self):
        response = self.client.post(
            "/api/transactions/send/",
            {
                "sim_profile_id": self.sim.id,
                "product": "flexiload",
                "phone_number": "01712345678",
                "amount": "50",
                "idempotency_key": "order-100",
            },
            format="json",
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["status"], "success")
        self.assertEqual(TopupTransaction.objects.count(), 1)

    def test_idempotency_returns_existing_transaction(self):
        payload = {
            "sim_profile_id": self.sim.id,
            "product": "topup",
            "phone_number": "01712345678",
            "amount": "20",
            "idempotency_key": "same-order",
        }
        first = self.client.post("/api/transactions/send/", payload, format="json")
        second = self.client.post("/api/transactions/send/", payload, format="json")
        self.assertEqual(first.status_code, 201)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(first.data["id"], second.data["id"])
        self.assertEqual(TopupTransaction.objects.count(), 1)

    def test_cannot_use_another_users_sim(self):
        other_sim = SimProfile(
            owner=self.other,
            name="Other SIM",
            operator="robi",
        )
        other_sim.set_pin("5678")
        other_sim.save()
        response = self.client.post(
            "/api/transactions/send/",
            {
                "sim_profile_id": other_sim.id,
                "product": "topup",
                "phone_number": "01812345678",
                "amount": "20",
            },
            format="json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(TopupTransaction.objects.count(), 0)

    def test_sim_response_never_contains_pin(self):
        response = self.client.get(f"/api/sim-profiles/{self.sim.id}/")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("pin", response.data)
        self.assertNotIn("encrypted_pin", response.data)


class UssdCommandTests(TestCase):
    def test_grameenphone_command(self):
        code, dcs = build_ussd(
            "grameenphone",
            "flexiload",
            "01712345678",
            50,
            "1234",
        )
        self.assertEqual(code, "*444*0*01712345678*50*1234#")
        self.assertEqual(dcs, 15)

    def test_banglalink_command(self):
        code, dcs = build_ussd(
            "banglalink",
            "offer",
            "01912345678",
            100,
            "9999",
        )
        self.assertEqual(code, "*555*01912345678*100*9999#")
        self.assertEqual(dcs, 0)

    def test_interactive_menu_is_not_a_completed_topup(self):
        response = '+CUSD: 1,"0:Default Recharge\\n*:Next",15\nOK'
        self.assertTrue(ussd_session_is_interactive(response))

    @override_settings(PIN_ENCRYPTION_KEY=TEST_KEY, MODEM_BACKEND="mock")
    def test_banglalink_topup_completes_interactive_session(self):
        user = User.objects.create_user(
            username="banglalink-agent",
            email="bl@example.com",
            password="strong-test-pass",
        )
        profile = SimProfile(
            owner=user,
            name="Banglalink modem",
            operator="banglalink",
            port="COM49",
            sim_number="01900000000",
            sim_verified_at=timezone.now(),
        )
        profile.set_pin("9999")
        profile.save()
        from .services import execute_topup

        transaction, _ = execute_topup(
            user,
            {
                "sim_profile": profile,
                "product": "topup",
                "phone_number": "01912345678",
                "amount": 20,
                "idempotency_key": "bl-interactive-1",
            },
        )
        self.assertEqual(transaction.status, "success")
        self.assertIn("Default Recharge", transaction.modem_response)
        self.assertIn("Recharge request accepted", transaction.modem_response)


@override_settings(PIN_ENCRYPTION_KEY=TEST_KEY)
class ModemAdminTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_superuser(
            "admin",
            "admin@example.com",
            "strong-test-pass",
        )
        self.client.force_login(self.admin)

    @patch("topups.admin.detect_serial_ports")
    def test_scan_and_connect_modem_from_admin(self, detect_serial_ports):
        detect_serial_ports.return_value = [
            DetectedPort(
                device="COM51",
                description="XR21V1414 USB UART Ch A",
                responsive=True,
                operator="grameenphone",
                operator_name="Grameenphone",
                sim_status="READY",
                signal="20,99",
            )
        ]
        scan_response = self.client.post("/admin/topups/simprofile/scan/")
        self.assertEqual(scan_response.status_code, 302)

        page = self.client.get("/admin/topups/simprofile/")
        self.assertContains(page, "COM51")
        self.assertContains(page, "AT modem detected")

        connect_response = self.client.post(
            "/admin/topups/simprofile/connect/",
            {
                "port": "COM51",
                "name": "Counter GP",
                "operator": "grameenphone",
                "pin": "1234",
            },
        )
        self.assertEqual(connect_response.status_code, 302)
        profile = SimProfile.objects.get(port="COM51")
        self.assertEqual(profile.owner, self.admin)
        self.assertEqual(profile.operator, "grameenphone")
        self.assertEqual(profile.get_pin(), "1234")
        self.assertEqual(profile.sim_number, "01700000000")
        self.assertIsNotNone(profile.sim_verified_at)

    def test_connect_requires_responsive_scanned_port(self):
        response = self.client.post(
            "/admin/topups/simprofile/connect/",
            {
                "port": "COM99",
                "operator": "robi",
                "pin": "1234",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertFalse(SimProfile.objects.exists())

    def test_operator_normalization(self):
        self.assertEqual(normalize_operator("Grameenphone"), "grameenphone")
        self.assertEqual(normalize_operator("47003"), "banglalink")
        self.assertEqual(normalize_operator("Unknown Network"), "")

    def test_all_operator_number_check_codes(self):
        self.assertEqual(SIM_NUMBER_CODES["grameenphone"], "*2#")
        self.assertEqual(SIM_NUMBER_CODES["banglalink"], "*511#")
        self.assertEqual(SIM_NUMBER_CODES["airtel"], "*282#")
        self.assertEqual(SIM_NUMBER_CODES["robi"], "*140*2*4#")

    def test_extracts_number_from_plain_and_ucs2_ussd(self):
        self.assertEqual(
            extract_bangladesh_number("Your number is 01712345678"),
            "01712345678",
        )
        encoded = "Your number is 01912345678".encode("utf-16-be").hex().upper()
        self.assertEqual(
            extract_bangladesh_number(f'+CUSD: 0,"{encoded}",72'),
            "01912345678",
        )

    @override_settings(MODEM_BACKEND="mock")
    def test_admin_topup_page_submits_ussd_transaction(self):
        profile = SimProfile(
            owner=self.admin,
            name="GP Modem",
            operator="grameenphone",
            port="COM51",
        )
        profile.set_pin("1234")
        profile.sim_number = "01700000000"
        profile.sim_verified_at = timezone.now()
        profile.save()

        page = self.client.get("/admin/topups/simprofile/")
        self.assertContains(page, "Dial Flexiload USSD")
        self.assertContains(page, "Dial USSD and submit top-up")

        response = self.client.post(
            "/admin/topups/simprofile/topup/",
            {
                "sim_profile": profile.pk,
                "product": "skitto",
                "phone_number": "01712345678",
                "amount": "50.00",
                "pin": "",
                "confirm": "on",
            },
        )
        self.assertEqual(response.status_code, 302)
        transaction = TopupTransaction.objects.get()
        self.assertEqual(transaction.user, self.admin)
        self.assertEqual(transaction.sim_profile, profile)
        self.assertEqual(transaction.product, "skitto")
        self.assertEqual(transaction.status, "success")

    @override_settings(MODEM_BACKEND="mock")
    def test_admin_topup_requires_confirmation(self):
        profile = SimProfile(
            owner=self.admin,
            name="Robi Modem",
            operator="robi",
            port="COM52",
        )
        profile.set_pin("5678")
        profile.sim_number = "01800000000"
        profile.sim_verified_at = timezone.now()
        profile.save()

        response = self.client.post(
            "/admin/topups/simprofile/topup/",
            {
                "sim_profile": profile.pk,
                "product": "topup",
                "phone_number": "01812345678",
                "amount": "20.00",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertFalse(TopupTransaction.objects.exists())


@override_settings(PIN_ENCRYPTION_KEY=TEST_KEY, MODEM_BACKEND="mock")
class CustomDashboardTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            "dashboard-user",
            password="strong-test-pass",
            is_staff=True,
        )
        self.client.force_login(self.user)
        self.profile = SimProfile(
            owner=self.user,
            name="Dashboard GP",
            operator="grameenphone",
            port="COM51",
            sim_number="01700000000",
            sim_verified_at=timezone.now(),
        )
        self.profile.set_pin("1234")
        self.profile.save()

    def test_custom_dashboard_pages_render(self):
        for url, text in [
            ("/dashboard/", "Operations overview"),
            ("/dashboard/modems/", "Modems & SIMs"),
            ("/dashboard/topup/", "Send mobile top-up"),
            ("/dashboard/transactions/", "Transaction history"),
            ("/dashboard/api-docs/", "API documentation"),
        ]:
            response = self.client.get(url)
            self.assertEqual(response.status_code, 200)
            self.assertContains(response, text)

    def test_dashboard_topup_redirects_to_full_response(self):
        response = self.client.post(
            "/dashboard/topup/",
            {
                "sim_profile": self.profile.pk,
                "product": "flexiload",
                "phone_number": "01712345678",
                "amount": "50.00",
                "confirm": "on",
            },
        )
        transaction = TopupTransaction.objects.get()
        self.assertRedirects(
            response,
            f"/dashboard/transactions/{transaction.pk}/",
        )
        detail = self.client.get(response["Location"])
        self.assertContains(detail, "Complete modem response")
        self.assertContains(detail, "MOCK: request accepted")

    def test_dashboard_requires_login(self):
        self.client.logout()
        response = self.client.get("/dashboard/transactions/")
        self.assertRedirects(
            response,
            "/dashboard/login/?next=/dashboard/transactions/",
        )


@override_settings(AUTHENTICATION_BACKENDS=["topups.authentication.EmailBackend"])
class EmailAuthenticationTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="internal-operator",
            email="operator@example.com",
            password="strong-test-pass",
        )

    def test_dashboard_login_uses_email_and_password(self):
        response = self.client.post(
            "/dashboard/login/",
            {
                "email": "OPERATOR@example.com",
                "password": "strong-test-pass",
            },
        )
        self.assertRedirects(response, "/dashboard/")
        self.assertEqual(
            int(self.client.session["_auth_user_id"]),
            self.user.pk,
        )

    def test_dashboard_does_not_accept_username(self):
        response = self.client.post(
            "/dashboard/login/",
            {
                "email": "internal-operator",
                "password": "strong-test-pass",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_api_login_uses_email_and_password(self):
        response = self.client.post(
            "/api/auth/login/",
            {
                "email": "operator@example.com",
                "password": "strong-test-pass",
            },
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("token", response.json())

    def test_duplicate_email_is_rejected(self):
        User.objects.create_user(
            username="duplicate",
            email="operator@example.com",
            password="strong-test-pass",
        )
        response = self.client.post(
            "/dashboard/login/",
            {
                "email": "operator@example.com",
                "password": "strong-test-pass",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("_auth_user_id", self.client.session)
