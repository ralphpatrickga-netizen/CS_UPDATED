import os
import smtplib
import ssl
import logging
from email.message import EmailMessage

logger = logging.getLogger(__name__)

class EmailConfigurationError(RuntimeError):
    pass


def send_email(recipient, subject, body, html_body=None):
    host = os.environ.get("SMTP_HOST", "").strip()
    sender = os.environ.get("SMTP_FROM", "").strip()
    if not host or not sender:
        raise EmailConfigurationError("SMTP_HOST and SMTP_FROM must be configured.")

    try:
        port = int(os.environ.get("SMTP_PORT", "587"))
        timeout = float(os.environ.get("SMTP_TIMEOUT_SECONDS", "15"))
    except ValueError as exc:
        raise EmailConfigurationError("SMTP_PORT and SMTP_TIMEOUT_SECONDS must be numeric.") from exc
    if not 1 <= port <= 65535 or timeout <= 0:
        raise EmailConfigurationError("SMTP_PORT must be valid and SMTP_TIMEOUT_SECONDS must be positive.")

    use_ssl = os.environ.get("SMTP_USE_SSL", "false").lower() in {"1", "true", "yes"}
    use_tls = os.environ.get("SMTP_USE_TLS", "true").lower() in {"1", "true", "yes"}
    if use_ssl and use_tls:
        raise EmailConfigurationError("Enable only one of SMTP_USE_SSL or SMTP_USE_TLS.")
    username = os.environ.get("SMTP_USERNAME", "").strip()
    password = os.environ.get("SMTP_PASSWORD", "")
    if not username or not password:
        raise EmailConfigurationError(
            "SMTP_USERNAME and SMTP_PASSWORD are required for authenticated email delivery."
        )

    message = EmailMessage()
    message["From"] = sender
    message["To"] = recipient
    message["Subject"] = subject
    message.set_content(body)
    if html_body:
        message.add_alternative(html_body, subtype="html")

    smtp_class = smtplib.SMTP_SSL if use_ssl else smtplib.SMTP
    context = ssl.create_default_context()
    logger.info("Connecting to configured SMTP service.")
    try:
        with smtp_class(host, port, timeout=timeout, context=context) if use_ssl else smtp_class(
            host, port, timeout=timeout
        ) as smtp:
            logger.info("SMTP connection established.")
            if use_tls:
                smtp.starttls(context=context)
            smtp.login(username, password)
            logger.info("SMTP authentication completed.")
            smtp.send_message(message)
            logger.info("SMTP accepted message for submission; inbox delivery is not confirmed.")
    except (smtplib.SMTPException, OSError) as exc:
        logger.error(
            "SMTP operation failed (type=%s, smtp_code=%s).",
            type(exc).__name__,
            getattr(exc, "smtp_code", None),
        )
        raise
