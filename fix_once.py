# One-time cleanup + re-check. Run from the project folder with:
#     python manage.py shell < fix_once.py
from topups.models import (
    SimProfile,
    StoredModemMessage,
    TopupTransaction,
    TransactionStatus,
)
from topups.dashboard_views import _reconcile_archived_messages, _message_target_number
from topups.modem import (
    classify_sms_result,
    extract_sms_transaction_id,
    normalize_bd_number,
)

# 1) Delete stale MOCK sample messages (the wrong-operator "Banglalink" rows
#    left over from mock mode: sender 'Banglalink' + the fixed mock timestamp).
mock = StoredModemMessage.objects.filter(
    sender="Banglalink", modem_timestamp="26/06/13,10:30:00+24"
)
print("Stale mock messages deleted:", mock.count())
mock.delete()

# 2) Correct transactions that were wrongly marked FAILED even though a real
#    SUCCESS SMS for that exact number exists and is not already used by another
#    transaction. These are re-opened and then re-matched by the corrected
#    logic. Transactions that genuinely failed (no matching success SMS, or the
#    success SMS already belongs to a different transaction) are left untouched.
reopened = 0
for profile in SimProfile.objects.filter(is_active=True):
    stored = list(profile.stored_messages.all())
    for t in TopupTransaction.objects.filter(
        sim_profile=profile, status=TransactionStatus.FAILED
    ):
        target = normalize_bd_number(t.phone_number)
        match = None
        for m in stored:
            if (
                classify_sms_result(m.text) == "success"
                and _message_target_number(m.text) == target
                and m.stored_at >= t.created_at
            ):
                cid = extract_sms_transaction_id(m.text)
                if cid and TopupTransaction.objects.filter(
                    sim_profile=profile, modem_response__icontains=cid
                ).exclude(pk=t.pk).exists():
                    continue  # this success SMS already belongs to another txn
                match = m
                break
        if match:
            base = t.modem_response.split("SMS CONFIRMATION:")[0].rstrip()
            t.modem_response = base
            t.status = TransactionStatus.PROCESSING
            t.error_message = ""
            t.completed_at = None
            t.save(
                update_fields=[
                    "modem_response",
                    "status",
                    "error_message",
                    "completed_at",
                ]
            )
            reopened += 1
            print(f"Re-opened {t.phone_number} ({str(t.id)[:8]}) - real success SMS found")
    _reconcile_archived_messages(profile)

print("Transactions re-opened for correction:", reopened)
for num in ("01XXXXXXXXX", "01YYYYYYYYY")  # replace with numbers to check:
    q = TopupTransaction.objects.filter(phone_number=num)
    if q.exists():
        t = q.latest("created_at")
        print(f"{num}: {t.status}")
