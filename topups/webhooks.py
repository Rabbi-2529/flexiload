import json
import logging
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from django.conf import settings

from .modem import extract_carrier_message, extract_carrier_transaction_id


logger = logging.getLogger("topups.webhooks")


def _final_message(transaction):
    marker = "SMS CONFIRMATION:"
    if marker in transaction.modem_response:
        return transaction.modem_response.rsplit(marker, 1)[-1].strip()
    return extract_carrier_message(transaction.modem_response)


def send_transaction_webhook(transaction):
    url = settings.IGL_WEBHOOK_URL.strip()
    if not url:
        logger.warning(
            "Transaction webhook skipped transaction=%s: IGL_WEBHOOK_URL is empty",
            transaction.id,
        )
        return False
    if not transaction.idempotency_key:
        logger.warning(
            "Transaction webhook skipped transaction=%s: idempotency_key is empty",
            transaction.id,
        )
        return False

    message = _final_message(transaction)
    # A transaction ID is only meaningful for a successful recharge. Never
    # send the throwaway USSD token for pending/failed transactions.
    carrier_transaction_id = (
        extract_carrier_transaction_id(message) or None
        if transaction.status == "success"
        else None
    )
    payload = {
        "id": str(transaction.id),
        "status": transaction.status,
        "idempotency_key": transaction.idempotency_key,
        "carrier_transaction_id": carrier_transaction_id,
        "clear_carrier_transaction_id": transaction.status == "failed",
        "message": message,
        "source_sim_number": transaction.sim_profile.sim_number,
        "targeted_number": transaction.phone_number,
        "amount": transaction.amount,
        "operator": transaction.operator,
        "product": transaction.product,
        "retry_count": transaction.retry_count,
        "last_retried_at": (
            transaction.last_retried_at.isoformat()
            if transaction.last_retried_at
            else None
        ),
        "completed_at": (
            transaction.completed_at.isoformat()
            if transaction.completed_at
            else None
        ),
    }
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "Flexiload-Django-Webhook/1.0",
    }
    if settings.IGL_WEBHOOK_SECRET:
        headers["X-IGL-Webhook-Secret"] = settings.IGL_WEBHOOK_SECRET
    request = Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urlopen(request, timeout=settings.IGL_WEBHOOK_TIMEOUT) as response:
            delivered = 200 <= response.status < 300
    except HTTPError as exc:
        try:
            response_body = exc.read().decode("utf-8", errors="replace")
        except OSError:
            response_body = ""
        logger.error(
            "Transaction webhook rejected transaction=%s url=%s http_status=%s "
            "response=%s",
            transaction.id,
            url,
            exc.code,
            response_body[:1000],
        )
        return False
    except (URLError, OSError, TimeoutError) as exc:
        logger.error(
            "Transaction webhook failed transaction=%s url=%s error=%s",
            transaction.id,
            url,
            exc,
        )
        return False

    if delivered:
        logger.info(
            "Transaction webhook delivered transaction=%s status=%s",
            transaction.id,
            transaction.status,
        )
    else:
        logger.error(
            "Transaction webhook returned a non-success response transaction=%s status=%s",
            transaction.id,
            response.status,
        )
    return delivered
