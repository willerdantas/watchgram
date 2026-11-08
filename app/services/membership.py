"""Regras do ciclo de vida da assinatura (sem chamadas ao Telegram)."""

from datetime import datetime, timedelta

from aiogram.types import User
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Event, Member, Plan
from app.utils import utcnow


async def get_or_create_member(session: AsyncSession, tenant_id: int, user: User) -> Member:
    member = await get_member_by_tg(session, tenant_id, user.id)
    if member is None:
        member = Member(tenant_id=tenant_id, telegram_id=user.id, username=user.username, first_name=user.first_name)
        session.add(member)
        await session.flush()
    else:
        member.username = user.username
        member.first_name = user.first_name
    return member


async def get_member_by_tg(session: AsyncSession, tenant_id: int, telegram_id: int) -> Member | None:
    return await session.scalar(select(Member).where(Member.tenant_id == tenant_id, Member.telegram_id == telegram_id))


def is_active(member: Member | None) -> bool:
    # O status só sai de "active" no agendador, depois da carência configurada.
    return member is not None and member.status == "active"


def extend(session: AsyncSession, member: Member, plan: Plan, days: int | None = None) -> str:
    """Soma os dias do plano ao vencimento. Retorna o tipo de evento gerado."""
    now = utcnow()
    days = plan.days if days is None else days
    was_active = member.status == "active" and member.expires_at and member.expires_at > now
    if member.status == "none":
        kind = "new"
    elif was_active:
        kind = "renew"
    else:
        kind = "reactivate"

    base = member.expires_at if was_active else now
    member.expires_at = base + timedelta(days=days)
    member.status = "active"
    member.plan_code = plan.code
    member.reminders_sent = ""
    session.add(Event(tenant_id=member.tenant_id, member_id=member.id, kind=kind, detail=f"{plan.code}:{days}d"))
    return kind


def add_days(session: AsyncSession, member: Member, days: int, detail: str = "manual") -> str:
    """Liberação manual pelo admin, sem plano associado."""
    now = utcnow()
    was_active = member.status == "active" and member.expires_at and member.expires_at > now
    kind = "renew" if was_active else ("new" if member.status == "none" else "reactivate")
    base = member.expires_at if was_active else now
    member.expires_at = base + timedelta(days=days)
    member.status = "active"
    member.reminders_sent = ""
    session.add(Event(tenant_id=member.tenant_id, member_id=member.id, kind=kind, detail=f"{detail}:{days}d"))
    return kind


def set_in_group(session: AsyncSession, member: Member, in_group: bool, detail: str = "") -> bool:
    """Muda a presença no grupo e registra a entrada/saída uma vez só. Retorna se mudou."""
    if member.in_group == in_group:
        return False
    member.in_group = in_group
    kind = "joined" if in_group else "left"
    session.add(Event(tenant_id=member.tenant_id, member_id=member.id, kind=kind, detail=detail))
    return True


def mark_expired(session: AsyncSession, member: Member, reason: str = "expired") -> None:
    member.status = "expired"
    member.reminders_sent = ""
    session.add(Event(tenant_id=member.tenant_id, member_id=member.id, kind=reason, detail=member.plan_code or ""))


def due_offset(expires_at: datetime | None, sent: str, reminder_days: list[int], now: datetime | None = None) -> int | None:
    """Offset (em dias) do lembrete a enviar agora, ou None.

    Envia apenas o lembrete mais próximo do vencimento ainda não enviado,
    para não disparar vários de uma vez.
    """
    if expires_at is None:
        return None
    remaining = expires_at - (now or utcnow())
    if remaining.total_seconds() < 0:
        return None
    days_left = remaining.days  # dias cheios restantes
    already = {int(x) for x in (sent or "").split(",") if x.strip().isdigit()}
    candidates = [d for d in reminder_days if d >= days_left and d not in already]
    return min(candidates) if candidates else None


def mark_offsets(sent: str, offset: int, reminder_days: list[int]) -> str:
    """Registra o lembrete enviado e também os offsets maiores, que perderam a vez."""
    parts = [x for x in (sent or "").split(",") if x.strip()]
    numbers = {int(x) for x in parts if x.isdigit()} | {d for d in reminder_days if d >= offset}
    others = [x for x in parts if not x.isdigit()]
    return ",".join([str(d) for d in sorted(numbers, reverse=True)] + others)


def due_reminder(member: Member, reminder_days: list[int], now: datetime | None = None) -> int | None:
    if member.status != "active":
        return None
    return due_offset(member.expires_at, member.reminders_sent, reminder_days, now)


def register_reminder(member: Member, offset: int, reminder_days: list[int]) -> None:
    member.reminders_sent = mark_offsets(member.reminders_sent, offset, reminder_days)
