"""Sends the newsletter over SMTP (Gmail by default)."""

from __future__ import annotations

import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr, make_msgid

from config import Settings


class MailConfigError(RuntimeError):
    pass


def build_message(settings: Settings, subject: str, text: str, html: str) -> EmailMessage:
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = formataddr((settings.sender_name, settings.smtp_username))
    message["To"] = settings.recipient
    message["Message-ID"] = make_msgid(domain="alphaone.local")
    message.set_content(text)
    message.add_alternative(html, subtype="html")
    return message


def send(settings: Settings, subject: str, text: str, html: str) -> None:
    if not settings.smtp_username or not settings.smtp_password:
        raise MailConfigError("SMTP_USERNAME and SMTP_PASSWORD must be set to send email")

    message = build_message(settings, subject, text, html)
    context = ssl.create_default_context()
    if settings.smtp_port == 465:
        with smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port, context=context, timeout=60) as smtp:
            smtp.login(settings.smtp_username, settings.smtp_password)
            smtp.send_message(message)
    else:
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=60) as smtp:
            smtp.starttls(context=context)
            smtp.login(settings.smtp_username, settings.smtp_password)
            smtp.send_message(message)
