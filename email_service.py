import os
import smtplib
import ssl
from email.message import EmailMessage


class EmailConfigurationError(RuntimeError):
    pass


def send_email(recipient, subject, body):
    host = os.environ.get("SMTP_HOST", "").strip()
    sender = os.environ.get("SMTP_FROM", "").strip()
    if not host or not sender:
        raise EmailConfigurationError("SMTP_HOST and SMTP_FROM must be configured.")

    try:
        port = int(os.environ.get("SMTP_PORT", "587"))
        timeout = float(os.environ.get("SMTP_TIMEOUT_SECONDS", "15"))
    except ValueError as exc:
        raise EmailConfigurationError("SMTP_PORT and SMTP_TIMEOUT_SECONDS must be numeric.") from exc

    use_ssl = os.environ.get("SMTP_USE_SSL", "false").lower() in {"1", "true", "yes"}
    use_tls = os.environ.get("SMTP_USE_TLS", "true").lower() in {"1", "true", "yes"}
    if use_ssl and use_tls:
        raise EmailConfigurationError("Enable only one of SMTP_USE_SSL or SMTP_USE_TLS.")

    message = EmailMessage()
    message["From"] = sender
    message["To"] = recipient
    message["Subject"] = subject
    message.set_content(body)

    smtp_class = smtplib.SMTP_SSL if use_ssl else smtplib.SMTP
    context = ssl.create_default_context()
    with smtp_class(host, port, timeout=timeout, context=context) if use_ssl else smtp_class(
        host, port, timeout=timeout
    ) as smtp:
        if use_tls:
            smtp.starttls(context=context)
        username = os.environ.get("SMTP_USERNAME", "")
        password = os.environ.get("SMTP_PASSWORD", "")
        if username:
            smtp.login(username, password)
        smtp.send_message(message)
