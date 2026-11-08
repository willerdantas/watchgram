"""Envio de e-mail por SMTP (biblioteca padrão), configurado pelo superadmin em Plataforma."""

import asyncio
import logging
import smtplib
import ssl
from email.message import EmailMessage

log = logging.getLogger(__name__)


def smtp_configured(settings: dict[str, str]) -> bool:
    return bool(settings.get("smtp_host") and settings.get("smtp_from"))


def _send_sync(settings: dict[str, str], to: str, subject: str, body: str) -> None:
    msg = EmailMessage()
    msg["From"] = settings["smtp_from"]
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)
    host = settings["smtp_host"]
    port = int(settings.get("smtp_port") or 587)
    context = ssl.create_default_context()
    if port == 465:
        server = smtplib.SMTP_SSL(host, port, timeout=20, context=context)
    else:
        server = smtplib.SMTP(host, port, timeout=20)
    with server:
        if port != 465 and settings.get("smtp_tls", "1") == "1":
            server.starttls(context=context)
        if settings.get("smtp_user"):
            server.login(settings["smtp_user"], settings.get("smtp_password", ""))
        server.send_message(msg)


async def send_email(settings: dict[str, str], to: str, subject: str, body: str) -> bool:
    if not smtp_configured(settings) or not to:
        return False
    try:
        await asyncio.to_thread(_send_sync, settings, to, subject, body)
        return True
    except (OSError, smtplib.SMTPException) as exc:
        log.warning("Falha ao enviar e-mail para %s: %s", to, exc)
        return False
