from datetime import timedelta

from sqlalchemy import select

from app.models import Event, Member, Plan
from app.services import membership
from app.utils import utcnow
from tests.tenant import TID


async def _member(session, **kwargs) -> Member:
    member = Member(tenant_id=TID, telegram_id=kwargs.pop("telegram_id", 111), first_name="Ana", **kwargs)
    session.add(member)
    await session.flush()
    return member


async def test_extend_new_then_renew(session):
    plan = await session.get(Plan, (TID, "mensal"))
    member = await _member(session)

    assert membership.extend(session, member, plan) == "new"
    first_expiry = member.expires_at
    assert member.status == "active"
    assert abs((first_expiry - utcnow()) - timedelta(days=30)) < timedelta(minutes=1)

    # Renovação antecipada soma ao vencimento atual, não a partir de hoje.
    assert membership.extend(session, member, plan) == "renew"
    assert member.expires_at == first_expiry + timedelta(days=30)
    await session.commit()

    kinds = (await session.scalars(select(Event.kind).order_by(Event.id))).all()
    assert kinds == ["new", "renew"]


async def test_extend_after_expiry_counts_from_now(session):
    plan = await session.get(Plan, (TID, "trimestral"))
    member = await _member(session, status="expired", expires_at=utcnow() - timedelta(days=10))
    assert membership.extend(session, member, plan) == "reactivate"
    assert abs((member.expires_at - utcnow()) - timedelta(days=90)) < timedelta(minutes=1)


async def test_due_reminder_picks_closest_unsent(session):
    member = await _member(session, status="active", expires_at=utcnow() + timedelta(days=2, hours=5))
    days = [7, 3, 1, 0]
    # 2 dias restantes: 7 e 3 já "passaram"; deve enviar só o de 3 (o mais próximo).
    assert membership.due_reminder(member, days) == 3
    membership.register_reminder(member, 3, days)
    assert member.reminders_sent == "7,3"
    assert membership.due_reminder(member, days) is None

    member.expires_at = utcnow() + timedelta(hours=10)
    assert membership.due_reminder(member, days) == 0


async def test_due_reminder_ignores_expired(session):
    member = await _member(session, status="active", expires_at=utcnow() - timedelta(hours=1))
    assert membership.due_reminder(member, [3, 0]) is None
