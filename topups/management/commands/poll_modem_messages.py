import time
from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import close_old_connections
from django.utils import timezone

from topups.dashboard_views import (
    _archive_modem_messages,
    _message_fingerprint,
)
from topups.models import SimProfile, StoredModemMessage, TopupTransaction, TransactionStatus
from topups.modem import ModemError, get_modem
from topups.services import dispatch_topup


class Command(BaseCommand):
    help = "Continuously import modem SMS and reconcile processing transactions."

    def add_arguments(self, parser):
        parser.add_argument(
            "--interval",
            type=int,
            default=settings.MODEM_SMS_POLL_INTERVAL,
            help="Seconds between modem scans.",
        )
        parser.add_argument(
            "--once",
            action="store_true",
            help="Scan every connected modem once and exit.",
        )

    def handle(self, *args, **options):
        interval = max(5, options["interval"])
        self.stdout.write(
            self.style.SUCCESS(
                f"Modem SMS poller started; interval={interval}s"
            )
        )
        while True:
            close_old_connections()
            self._poll_once(verbose=options["once"])
            close_old_connections()
            if options["once"]:
                return
            try:
                time.sleep(interval)
            except KeyboardInterrupt:
                self.stdout.write(self.style.WARNING("Modem SMS poller stopped."))
                return

    def _poll_once(self, verbose=False):
        modem = get_modem()
        profiles = SimProfile.objects.filter(
            is_active=True,
        ).exclude(port="").order_by("port", "pk")

        for profile in profiles:
            try:
                # 1. Read SMS from the modem for this SIM.
                modem_messages = modem.read_messages(profile.port)

                # 2. Store + reconcile (strict matching happens inside archive).
                #    This completes whichever recharge is currently in flight.
                imported, existing, updated = _archive_modem_messages(
                    profile,
                    modem_messages,
                )

                # 3. Delete each SMS from the modem, but only after it has been
                #    confirmed stored in the database.
                deleted = self._delete_stored_from_modem(
                    modem, profile, modem_messages
                )

                # 4. Fail a recharge that has been waiting too long with no
                #    confirmation, so the queue does not stall. (A late success
                #    SMS can still correct it afterwards.)
                timed_out = self._timeout_stuck(profile)

                # 5. If nothing is in flight on this SIM, send the next queued
                #    recharge — one at a time.
                dispatched = self._dispatch_next(profile)

                if (
                    imported or existing or updated or deleted
                    or timed_out or dispatched or verbose
                ):
                    self.stdout.write(
                        f"{profile.port}: imported={imported}, "
                        f"existing={existing}, updated={updated}, "
                        f"deleted={deleted}, timed_out={timed_out}, "
                        f"dispatched={dispatched}"
                    )

            except ModemError as exc:
                self.stderr.write(f"{profile.port}: {exc}")
                continue

    def _timeout_stuck(self, profile):
        """Fail recharges stuck PROCESSING past the timeout so the queue moves."""
        minutes = getattr(settings, "MODEM_PROCESSING_TIMEOUT_MINUTES", 6)
        cutoff = timezone.now() - timezone.timedelta(minutes=minutes)
        stuck = TopupTransaction.objects.filter(
            sim_profile=profile,
            status=TransactionStatus.PROCESSING,
            dispatched_at__lt=cutoff,
        )
        count = 0
        for txn in stuck:
            txn.status = TransactionStatus.FAILED
            txn.completed_at = timezone.now()
            if not txn.error_message:
                txn.error_message = (
                    "No confirmation SMS was received within the timeout. "
                    "Marked failed so the queue can continue; it will be "
                    "corrected automatically if a success SMS arrives later."
                )
            txn.save(update_fields=["status", "completed_at", "error_message"])
            count += 1
        return count

    def _dispatch_next(self, profile):
        """Send the oldest queued recharge if this SIM has none in flight."""
        in_flight = TopupTransaction.objects.filter(
            sim_profile=profile,
            status=TransactionStatus.PROCESSING,
        ).exists()
        if in_flight:
            return 0

        nxt = (
            TopupTransaction.objects.filter(
                sim_profile=profile,
                status=TransactionStatus.PENDING,
            )
            .order_by("created_at")
            .first()
        )
        if nxt is None:
            return 0

        try:
            dispatch_topup(nxt)
        except Exception as exc:  # never let one bad send kill the poller
            self.stderr.write(
                f"{profile.port}: could not dispatch {nxt.id}: {exc}"
            )
            return 0
        return 1

    def _delete_stored_from_modem(self, modem, profile, modem_messages):
        """Delete each SMS from the modem, but only after it is safely stored.

        A message is deleted only when a matching StoredModemMessage row exists
        (storage succeeded). If storage failed, or the modem no longer holds the
        same message at that index, the SMS is kept so it can be processed again
        on the next scan. Controlled by settings.MODEM_DELETE_AFTER_STORE.
        """
        if not getattr(settings, "MODEM_DELETE_AFTER_STORE", True):
            return 0
        if not hasattr(modem, "delete_message"):
            return 0

        deleted = 0
        for message in modem_messages:
            if message.index is None:
                continue

            fingerprint = _message_fingerprint(profile, message)
            is_stored = StoredModemMessage.objects.filter(
                sim_profile=profile,
                fingerprint=fingerprint,
            ).exists()
            if not is_stored:
                # Not confirmed in the database yet: never delete.
                continue

            try:
                if modem.delete_message(
                    message.index,
                    profile.port,
                    expected_text=message.text,
                ):
                    deleted += 1
            except ModemError as exc:
                self.stderr.write(
                    f"{profile.port}: could not delete SMS index "
                    f"{message.index}: {exc}"
                )
        return deleted
