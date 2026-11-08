from datetime import timedelta

from sqlalchemy import select

from app import scheduler
from app.models import Event, Member, Payment, Plan
from app.services import payments
from app.services.store import set_settings
from app.utils import utcnow
from tests.tenant import TID


async def _setup(session, **member_kwargs):
    await set_settings(session, TID, {"admin_ids": "999", "mp_access_token": "TEST-token", "group_chat_id": "-100123"})
    member = Member(tenant_id=TID, telegram_id=111, first_name="Ana", email="ana@example.com", **member_kwargs)
    session.add(member)
    await session.commit()
    return member


async def test_approve_releases_access_and_is_idempotent(session, sent_messages):
    member = await _setup(session)
    plan = await session.get(Plan, (TID, "mensal"))
    payment = await payments.start_manual_pix(session, member, plan)

    assert await payments.approve(session, payment) is True
    assert await payments.approve(session, payment) is False  # 2ª chamada não estende de novo

    await session.refresh(member)
    assert member.status == "active"
    assert abs((member.expires_at - utcnow()) - timedelta(days=30)) < timedelta(minutes=1)
    # Assinante recebe o convite; admin recebe aviso de venda.
    recipients = [chat for chat, _ in sent_messages]
    assert recipients == [111, 999]
    assert "Pagamento confirmado" in sent_messages[0][1]


class FakeMP:
    def __init__(self, payment=None, preapproval=None):
        self.payment = payment
        self.preapproval = preapproval

    async def get_payment(self, payment_id):
        return self.payment

    async def search_payments(self, ref):
        return [self.payment] if self.payment else []

    async def get_preapproval(self, preapproval_id):
        return self.preapproval


async def test_pix_pending_is_approved_by_polling(session, monkeypatch):
    member = await _setup(session)
    plan = await session.get(Plan, (TID, "semestral"))
    payment = Payment(tenant_id=TID, member_id=member.id, plan_code=plan.code, amount_cents=plan.price_cents,
        method="pix", external_ref="ref-1", mp_id="555",
    )
    session.add(payment)
    await session.commit()

    fake = FakeMP(payment={"id": 555, "status": "approved", "date_approved": "2026-11-02T10:00:00.000-03:00"})
    monkeypatch.setattr(payments, "mp_client", lambda settings: fake)
    await payments.check_pending(session, TID)

    await session.refresh(payment)
    await session.refresh(member)
    assert payment.status == "approved"
    assert member.plan_code == "semestral"
    assert abs((member.expires_at - utcnow()) - timedelta(days=180)) < timedelta(minutes=1)


async def test_recurring_sync_registers_new_charges_once(session, monkeypatch):
    member = await _setup(session, mp_preapproval_id="pre-1", recurring_status="pending")
    pre = {"status": "authorized", "summarized": {"last_charged_date": "2026-11-02T10:00:00.000-03:00", "last_charged_amount": 24.9}}
    monkeypatch.setattr(payments, "mp_client", lambda settings: FakeMP(preapproval=pre))

    await payments.sync_recurring(session, None, member)
    await payments.sync_recurring(session, None, member)  # mesma cobrança: não duplica

    approved = (await session.scalars(select(Payment).where(Payment.status == "approved"))).all()
    assert len(approved) == 1
    assert approved[0].amount_cents == 2490
    assert member.recurring_status == "authorized"
    assert member.status == "active"

    # Cobrança do mês seguinte estende de novo.
    first_expiry = member.expires_at
    pre["summarized"]["last_charged_date"] = "2026-12-02T10:00:00.000-03:00"
    await payments.sync_recurring(session, None, member)
    assert member.expires_at == first_expiry + timedelta(days=30)


async def test_lifecycle_expires_and_reminds(session, sent_messages):
    await set_settings(session, TID, {"grace_hours": "0", "reminder_days": "3,0", "remove_on_expire": "1"})
    expired = Member(tenant_id=TID, telegram_id=1, first_name="Velho", status="active", plan_code="mensal",
                     expires_at=utcnow() - timedelta(hours=1), in_group=True)
    soon = Member(tenant_id=TID, telegram_id=2, first_name="Quase", status="active", plan_code="mensal",
                  expires_at=utcnow() + timedelta(days=2))
    session.add_all([expired, soon])
    await session.commit()

    await scheduler.process_lifecycle(TID)
    await scheduler.process_lifecycle(TID)  # não repete o lembrete

    await session.refresh(expired)
    assert expired.status == "expired"
    assert expired.in_group is False
    kinds = (await session.scalars(select(Event.kind))).all()
    assert kinds == ["expired"]
    to_soon = [text for chat, text in sent_messages if chat == 2]
    assert len(to_soon) == 1 and "vence em" in to_soon[0]
    assert any(chat == 1 and "expirou" in text for chat, text in sent_messages)
