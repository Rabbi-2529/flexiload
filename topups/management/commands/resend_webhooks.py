from django.core.management.base import BaseCommand

from topups.models import TopupTransaction, TransactionStatus
from topups.webhooks import send_transaction_webhook


class Command(BaseCommand):
    help = (
        "Resend transaction webhooks for completed transactions. Use this to "
        "catch up transactions that finished but whose webhook was never "
        "delivered (so the downstream system is still showing them as "
        "processing)."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--since",
            help="Only transactions created on/after this date, YYYY-MM-DD.",
        )
        parser.add_argument(
            "--number",
            help="Only this destination phone number (e.g. 01958666966).",
        )
        parser.add_argument(
            "--status",
            choices=["success", "failed", "both"],
            default="success",
            help="Which final statuses to resend (default: success).",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="List what would be sent without actually sending.",
        )

    def handle(self, *args, **options):
        queryset = TopupTransaction.objects.all().order_by("created_at")

        if options["status"] == "success":
            queryset = queryset.filter(status=TransactionStatus.SUCCESS)
        elif options["status"] == "failed":
            queryset = queryset.filter(status=TransactionStatus.FAILED)
        else:
            queryset = queryset.filter(
                status__in=[TransactionStatus.SUCCESS, TransactionStatus.FAILED]
            )

        if options["since"]:
            queryset = queryset.filter(created_at__date__gte=options["since"])
        if options["number"]:
            queryset = queryset.filter(phone_number=options["number"])

        total = queryset.count()
        self.stdout.write(f"Matching transactions: {total}")

        delivered = 0
        failed = 0
        skipped = 0
        for txn in queryset.iterator():
            if not txn.idempotency_key:
                skipped += 1
                continue
            if options["dry_run"]:
                self.stdout.write(
                    f"  would resend {txn.phone_number} {txn.status} {txn.id}"
                )
                continue
            ok = send_transaction_webhook(txn)
            if ok:
                delivered += 1
            else:
                failed += 1
            self.stdout.write(
                f"  {txn.phone_number} {txn.status} -> "
                f"{'delivered' if ok else 'NOT delivered (check logs)'}"
            )

        if options["dry_run"]:
            self.stdout.write(self.style.WARNING("Dry run: nothing was sent."))
        else:
            self.stdout.write(
                self.style.SUCCESS(
                    f"Done. delivered={delivered}, not_delivered={failed}, "
                    f"skipped_no_key={skipped}"
                )
            )
