"""Pedidos de ajuda: cada pedido é uma conversa entre o usuário e o suporte (superadmin).

Fluxo: o usuário abre o pedido (open) -> o suporte responde (answered) -> o usuário
aprova a solução (resolved) ou diz que não resolveu (volta a open). Sem resposta do
usuário em 48 horas após a resposta do suporte, o pedido é encerrado (closed).
"""

import logging
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import config
from app.db import SessionLocal
from app.models import SupportMessage, SupportTicket, User
from app.utils import utcnow

log = logging.getLogger(__name__)

AUTO_CLOSE = timedelta(hours=48)
MAX_SUBJECT = 120
MAX_TEXT = 4000

STATUS_LABELS = {
    "open": "Aguardando o suporte",
    "answered": "Aguardando sua aprovação",
    "resolved": "Resolvido",
    "closed": "Encerrado sem resposta",
}
STAFF_STATUS_LABELS = STATUS_LABELS | {"answered": "Aguardando o usuário"}


def deadline(ticket: SupportTicket) -> datetime | None:
    """Até quando o usuário pode aprovar a solução."""
    if ticket.status != "answered" or ticket.answered_at is None:
        return None
    return ticket.answered_at + AUTO_CLOSE


def _log(ticket: SupportTicket, kind: str, text: str, author: User | None = None) -> SupportMessage:
    now = utcnow()
    message = SupportMessage(
        author_id=author.id if author else None,
        is_staff=bool(author and author.is_superadmin),
        kind=kind,
        text=text,
        created_at=now,
    )
    ticket.messages.append(message)
    ticket.updated_at = now
    return message


async def create_ticket(session: AsyncSession, user: User, subject: str, text: str) -> SupportTicket:
    ticket = SupportTicket(user_id=user.id, subject=subject[:MAX_SUBJECT], status="open", messages=[])
    _log(ticket, "message", text[:MAX_TEXT], user)
    session.add(ticket)
    await session.commit()
    return ticket


async def add_message(session: AsyncSession, ticket: SupportTicket, author: User, text: str) -> None:
    """Resposta do suporte pede a aprovação do usuário; mensagem do usuário devolve ao suporte."""
    _log(ticket, "message", text[:MAX_TEXT], author)
    if author.is_superadmin:
        ticket.status = "answered"
        ticket.answered_at = ticket.updated_at
    else:
        ticket.status = "open"
    await session.commit()


async def approve(session: AsyncSession, ticket: SupportTicket, user: User) -> None:
    _log(ticket, "approved", "Solução aprovada. Pedido encerrado.", user)
    ticket.status = "resolved"
    ticket.closed_at = ticket.updated_at
    await session.commit()


async def reject(session: AsyncSession, ticket: SupportTicket, user: User, comment: str) -> None:
    _log(ticket, "rejected", comment[:MAX_TEXT] or "A solução não resolveu.", user)
    ticket.status = "open"
    await session.commit()


async def close_stale(session: AsyncSession) -> list[int]:
    """Encerra os pedidos cuja solução não foi aprovada nem recusada em 48 horas."""
    limit = utcnow() - AUTO_CLOSE
    tickets = (
        await session.scalars(
            select(SupportTicket)
            .where(SupportTicket.status == "answered", SupportTicket.answered_at <= limit)
            .with_for_update()
        )
    ).all()
    for ticket in tickets:
        await session.refresh(ticket, ["messages"])
        _log(ticket, "auto_closed", "Encerrado automaticamente: a solução não foi aprovada nem recusada em 48 horas.")
        ticket.status = "closed"
        ticket.closed_at = ticket.updated_at
    await session.commit()
    return [t.id for t in tickets]


def ticket_url(ticket_id: int) -> str:
    if config.PUBLIC_URL.startswith("https://"):
        return f"{config.PUBLIC_URL}/ajuda/{ticket_id}"
    return f"no painel, em Ajuda (pedido #{ticket_id})"


# ----------------------------------------------------------------- avisos
# Rodam em segundo plano (e-mail e Telegram podem demorar), cada um com a própria sessão.


async def _last_text(session: AsyncSession, ticket_id: int) -> str:
    last = await session.scalar(
        select(SupportMessage).where(SupportMessage.ticket_id == ticket_id).order_by(SupportMessage.id.desc()).limit(1)
    )
    return last.text if last else ""


async def notify_staff(ticket_id: int, event: str) -> None:
    from app.services.platform import notify_user

    try:
        async with SessionLocal() as session:
            ticket = await session.get(SupportTicket, ticket_id)
            owner = await session.get(User, ticket.user_id)
            subject = f"Ajuda #{ticket.id} ({owner.username}): {event}"
            text = f"Assunto: {ticket.subject}\n\n{await _last_text(session, ticket_id)}\n\nResponder: {ticket_url(ticket.id)}"
            for admin in (await session.scalars(select(User).where(User.is_superadmin.is_(True)))).all():
                await notify_user(session, admin, subject, text)
    except Exception:  # noqa: BLE001 - aviso não pode derrubar a requisição
        log.exception("Falha ao avisar o suporte sobre o pedido %s", ticket_id)


async def notify_owner(ticket_id: int, auto_closed: bool = False) -> None:
    from app.services.platform import notify_user

    try:
        async with SessionLocal() as session:
            ticket = await session.get(SupportTicket, ticket_id)
            owner = await session.get(User, ticket.user_id)
            if auto_closed:
                subject = f"Pedido de ajuda #{ticket.id} encerrado"
                text = (f"O pedido \"{ticket.subject}\" foi encerrado porque a solução não foi aprovada em 48 horas.\n\n"
                        "Se ainda precisar de ajuda, abra um novo pedido no painel, em Ajuda.")
            else:
                subject = f"Resposta ao seu pedido de ajuda #{ticket.id}"
                text = (f"Assunto: {ticket.subject}\n\n{await _last_text(session, ticket_id)}\n\n"
                        f"Aprove a solução ou diga que não resolveu em até 48 horas: {ticket_url(ticket.id)}\n"
                        "Sem resposta nesse prazo, o pedido é encerrado automaticamente.")
            await notify_user(session, owner, subject, text)
    except Exception:  # noqa: BLE001 - aviso não pode derrubar a requisição
        log.exception("Falha ao avisar o usuário sobre o pedido %s", ticket_id)
