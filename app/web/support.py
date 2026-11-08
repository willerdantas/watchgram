"""Página Ajuda: pedidos de ajuda em formato de conversa.

O usuário vê só os próprios pedidos; o superadmin (suporte) vê e responde todos.
"""

from fastapi import APIRouter, BackgroundTasks, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models import SupportTicket, User
from app.services import support
from app.web.auth import current_user, get_session
from app.web.routes import back, flash, page

router = APIRouter(prefix="/ajuda")

FILTERS = {"ativos": ("open", "answered"), "encerrados": ("resolved", "closed")}


async def _own_ticket(session: AsyncSession, user: User, ticket_id: int) -> SupportTicket:
    ticket = await session.scalar(
        select(SupportTicket)
        .where(SupportTicket.id == ticket_id)
        .options(selectinload(SupportTicket.messages), selectinload(SupportTicket.user))
    )
    if ticket is None or (ticket.user_id != user.id and not user.is_superadmin):
        raise HTTPException(status_code=404)
    return ticket


async def _close_stale(session: AsyncSession, background: BackgroundTasks) -> None:
    """O agendador encerra os pedidos vencidos; aqui garante a tela certa mesmo entre uma rodada e outra."""
    for ticket_id in await support.close_stale(session):
        background.add_task(support.notify_owner, ticket_id, True)


def _text(value: str) -> str:
    return (value or "").strip()


@router.get("", response_class=HTMLResponse)
async def tickets_list(
    request: Request,
    background: BackgroundTasks,
    filtro: str = "ativos",
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
):
    await _close_stale(session, background)
    query = select(SupportTicket).options(selectinload(SupportTicket.user)).order_by(SupportTicket.updated_at.desc())
    counts_query = select(SupportTicket.status, func.count()).group_by(SupportTicket.status)
    if not user.is_superadmin:
        query = query.where(SupportTicket.user_id == user.id)
        counts_query = counts_query.where(SupportTicket.user_id == user.id)
    if filtro in FILTERS:
        query = query.where(SupportTicket.status.in_(FILTERS[filtro]))
    return page(
        request,
        user,
        "support_list.html",
        "support",
        tickets=(await session.scalars(query.limit(200))).all(),
        filtro=filtro if filtro in FILTERS else "todos",
        counts=dict((await session.execute(counts_query)).all()),
        labels=support.STAFF_STATUS_LABELS if user.is_superadmin else support.STATUS_LABELS,
        max_subject=support.MAX_SUBJECT,
        max_text=support.MAX_TEXT,
    )


@router.post("")
async def ticket_create(
    request: Request,
    background: BackgroundTasks,
    subject: str = Form(""),
    text: str = Form(""),
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
):
    if user.is_superadmin:
        raise HTTPException(status_code=404)  # o suporte responde, não abre pedidos
    subject, text = _text(subject), _text(text)
    if not subject or not text:
        flash(request, "Informe o assunto e descreva o que precisa.", "error")
        return back("/ajuda#novo")
    ticket = await support.create_ticket(session, user, subject, text)
    background.add_task(support.notify_staff, ticket.id, "novo pedido")
    flash(request, f"Pedido #{ticket.id} aberto. Você será avisado quando o suporte responder.")
    return back(f"/ajuda/{ticket.id}")


@router.get("/{ticket_id}", response_class=HTMLResponse)
async def ticket_view(
    request: Request,
    ticket_id: int,
    background: BackgroundTasks,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
):
    await _close_stale(session, background)
    ticket = await _own_ticket(session, user, ticket_id)
    return page(
        request,
        user,
        "support_ticket.html",
        "support",
        ticket=ticket,
        deadline=support.deadline(ticket),
        labels=support.STAFF_STATUS_LABELS if user.is_superadmin else support.STATUS_LABELS,
        is_owner=ticket.user_id == user.id,
        max_text=support.MAX_TEXT,
    )


@router.post("/{ticket_id}/mensagem")
async def ticket_message(
    request: Request,
    ticket_id: int,
    background: BackgroundTasks,
    text: str = Form(""),
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
):
    await _close_stale(session, background)
    ticket = await _own_ticket(session, user, ticket_id)
    if ticket.is_closed:
        flash(request, "Este pedido está encerrado. Abra um novo pedido se ainda precisar de ajuda.", "error")
        return back(f"/ajuda/{ticket.id}")
    if not _text(text):
        flash(request, "Escreva a mensagem.", "error")
        return back(f"/ajuda/{ticket.id}#responder")
    await support.add_message(session, ticket, user, _text(text))
    if user.is_superadmin:
        background.add_task(support.notify_owner, ticket.id)
        flash(request, "Resposta enviada. O usuário tem 48 horas para aprovar a solução.")
    else:
        background.add_task(support.notify_staff, ticket.id, "nova mensagem")
        flash(request, "Mensagem enviada ao suporte.")
    return back(f"/ajuda/{ticket.id}#fim")


async def _awaiting_owner(request: Request, session: AsyncSession, user: User, ticket_id: int):
    """Pedido com solução aguardando a decisão de quem pediu (só ele aprova ou recusa)."""
    ticket = await _own_ticket(session, user, ticket_id)
    if ticket.user_id != user.id:
        raise HTTPException(status_code=404)
    if ticket.status != "answered":
        flash(request, "Não há solução aguardando aprovação neste pedido.", "error")
        return ticket, False
    return ticket, True


@router.post("/{ticket_id}/aprovar")
async def ticket_approve(
    request: Request,
    ticket_id: int,
    background: BackgroundTasks,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
):
    await _close_stale(session, background)
    ticket, ok = await _awaiting_owner(request, session, user, ticket_id)
    if ok:
        await support.approve(session, ticket, user)
        background.add_task(support.notify_staff, ticket.id, "solução aprovada")
        flash(request, "Obrigado! Pedido encerrado como resolvido.")
    return back(f"/ajuda/{ticket.id}")


@router.post("/{ticket_id}/recusar")
async def ticket_reject(
    request: Request,
    ticket_id: int,
    background: BackgroundTasks,
    text: str = Form(""),
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
):
    await _close_stale(session, background)
    ticket, ok = await _awaiting_owner(request, session, user, ticket_id)
    if ok:
        await support.reject(session, ticket, user, _text(text))
        background.add_task(support.notify_staff, ticket.id, "a solução não resolveu")
        flash(request, "Avisamos o suporte que a solução não resolveu. O pedido continua aberto.")
    return back(f"/ajuda/{ticket.id}#fim")
