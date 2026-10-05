"""Sends the newsletter over SMTP (Gmail by default)."""

from __future__ import annotations

import logging
import smtplib
import ssl
import time
from contextlib import contextmanager
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid, parseaddr
from typing import Callable, Iterator

from config import Settings

log = logging.getLogger("alphaone.mailer")

RETRY_DELAYS = (10, 30, 90)  # seconds to wait before each retry of a failed send


class MailConfigError(RuntimeError):
    pass


def sender_address(settings: Settings) -> str:
    """ALPHAONE_FROM (a bare address or "Name <address>"), else the SMTP username."""
    address = parseaddr(settings.sender or settings.smtp_username)[1]
    local, _, domain = address.rpartition("@")
    if not local or not domain:
        raise MailConfigError(
            "the sender must be an email address: set ALPHAONE_FROM (SMTP_USERNAME is used when it is "
            "unset, and it isn't one)"
        )
    return address


def build_message(settings: Settings, subject: str, text: str, html: str) -> EmailMessage:
    sender = sender_address(settings)
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = formataddr((settings.sender_name, sender))
    message["To"] = settings.recipient
    message["Date"] = formatdate(usegmt=True)
    message["Message-ID"] = make_msgid(domain=sender.rpartition("@")[2])
    message.set_content(text)
    message.add_alternative(html, subtype="html")
    return message


def _require_credentials(settings: Settings) -> None:
    if not settings.smtp_username or not settings.smtp_password:
        raise MailConfigError("SMTP_USERNAME and SMTP_PASSWORD must be set to send email")


@contextmanager
def _session(settings: Settings) -> Iterator[smtplib.SMTP]:
    """A logged-in SMTP connection. Closing it never raises: by then the work is done."""
    _require_credentials(settings)
    context = ssl.create_default_context()
    if settings.smtp_port == 465:
        smtp = smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port, context=context, timeout=60)
    else:
        smtp = smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=60)
    try:
        if settings.smtp_port != 465:
            smtp.starttls(context=context)
        smtp.login(settings.smtp_username, settings.smtp_password)
        yield smtp
    finally:
        try:
            smtp.quit()
        except (smtplib.SMTPException, OSError):
            smtp.close()


def verify(settings: Settings) -> None:
    """Log in and offer the sender before any paid work, so bad mail settings fail fast and for free."""
    _require_credentials(settings)
    sender = sender_address(settings)

    def check() -> None:
        with _session(settings) as smtp:
            code, reply = smtp.mail(sender)
            if code != 250:
                raise smtplib.SMTPSenderRefused(code, reply, sender)
            smtp.rset()

    _with_retries(check)


def _retryable(exc: Exception) -> bool:
    if isinstance(exc, smtplib.SMTPRecipientsRefused):
        return all(400 <= code < 500 for code, _ in exc.recipients.values())
    if isinstance(exc, smtplib.SMTPResponseException):  # includes bad logins (535)
        return 400 <= exc.smtp_code < 500
    if isinstance(exc, (smtplib.SMTPNotSupportedError, ssl.SSLCertVerificationError)):
        return False
    return isinstance(exc, OSError)  # disconnects, timeouts, resets, refused connections


def _with_retries(action: Callable[[], None]) -> None:
    for delay in (*RETRY_DELAYS, None):
        try:
            return action()
        except Exception as exc:
            if delay is None or not _retryable(exc):
                raise
            log.warning("mail server error (%r) - retrying in %ds", exc, delay)
            time.sleep(delay)


def send(settings: Settings, subject: str, text: str, html: str) -> None:
    message = build_message(settings, subject, text, html)  # one Message-ID across retries

    def deliver() -> None:
        with _session(settings) as smtp:
            smtp.send_message(message)

    _with_retries(deliver)
