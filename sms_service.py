import os

import requests


class SMSConfigurationError(RuntimeError):
    pass


def send_sms(recipient, body):
    api_key = os.environ.get("SEMAPHORE_API_KEY", "").strip()
    if not api_key:
        raise SMSConfigurationError("SEMAPHORE_API_KEY must be configured.")

    try:
        timeout = float(os.environ.get("SMS_TIMEOUT_SECONDS", "15"))
    except ValueError as exc:
        raise SMSConfigurationError("SMS_TIMEOUT_SECONDS must be numeric.") from exc
    if timeout <= 0:
        raise SMSConfigurationError("SMS_TIMEOUT_SECONDS must be greater than zero.")
    if recipient.startswith("+63"):
        recipient = "0" + recipient[3:]

    response = requests.post(
        "https://api.semaphore.co/api/v4/messages",
        data={"apikey": api_key, "number": recipient, "message": body},
        timeout=timeout,
    )
    response.raise_for_status()
    try:
        result = response.json()
    except ValueError as exc:
        raise RuntimeError("Semaphore returned an unreadable response.") from exc
    if isinstance(result, dict) and result.get("error"):
        raise RuntimeError(f"Semaphore rejected the SMS request: {result['error']}")
    if not isinstance(result, list) or not result:
        raise RuntimeError("Semaphore did not confirm that the SMS was queued.")
    if any(
        not isinstance(item, dict) or item.get("status") in {"Failed", "Error"}
        for item in result
    ):
        raise RuntimeError("Semaphore could not queue the SMS message.")
