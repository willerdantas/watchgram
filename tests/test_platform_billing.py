"""Assinatura da plataforma: lembretes de vencimento (e-mail/Telegram) e renovação automática no cartão."""

import asyncio
from functools import partial
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app import runtime, scheduler
from app.bot.runner import SUBSCRIPTION_EXPIRED
from app.db import SessionLocal
from app.main import app
from app.models import PlatformPayment, User
from app.services import mailer, membership, platform
from app.services.store import set_settings
from app.utils import utcnow

B_LOGIN = {"username": "outro", "password": "senha-do-outro"}


@pytest.fixture
def emails(monkeypatch):
    sent = []
    monkeypatch.setattr(mailer, "_send_sync", lambda settings, to, subject, body: sent.append((to, subject, body)))
    return sent


async def _setup_user(user_id, until, **fields):
    async with SessionLocal() as s:
        await platform.set_platform_settings(
            s, {"smtp_host": "smtp.teste", "smtp_from": "plataforma@teste", "mp_access_token": "APP_USR-plat"}
        )
        await set_settings(s, user_id, {"admin_ids": "4242"})
        user = await s.get(User, user_id)
        user.subscription_until = until
        user.email = "b@example.com"
        for key, value in fields.items():
            setattr(user, key, value)
        await s.commit()


async def _user(user_id) -> User:
    async with SessionLocal() as s:
        return await s.get(User, user_id)


# --------------------------------------------------------------- lembretes


def test_due_offset_and_mark_keep_expired_flag():
    now = utcnow()
    assert membership.due_offset(now + timedelta(days=2, hours=5), "", [7, 3, 1, 0], now) == 3
    assert membership.mark_offsets("x", 3, [7, 3, 1, 0]) == "7,3,x"
    assert membership.due_offset(now + timedelta(days=2), "7,3,x", [7, 3, 1, 0], now) is None


async def test_reminder_sent_by_email_and_telegram_once(session, other_tenant, emails, sent_messages):
    await _setup_user(other_tenant, utcnow() + timedelta(days=2, hours=5))
    async with SessionLocal() as s:
        await platform.send_reminders(s)
        await platform.send_reminders(s)  # não repete

    assert [(to, subject) for to, subject, _ in emails] == [("b@example.com", "Sua assinatura vence em 3 dia(s)")]
    assert "o seu bot para de funcionar" in emails[0][2]
    assert [(chat, "vence em 3 dia(s)" in text) for chat, text in sent_messages] == [(4242, True)]
    assert (await _user(other_tenant)).platform_reminders_sent == "7,3"


async def test_no_reminder_with_recurring_and_reset_after_renewal(session, other_tenant, emails):
    await _setup_user(other_tenant, utcnow() + timedelta(days=1), platform_recurring_status="authorized",
                      platform_preapproval_id="pre-1")
    async with SessionLocal() as s:
        await platform.send_reminders(s)
    assert emails == []

    await _setup_user(other_tenant, utcnow() + timedelta(days=1), platform_recurring_status=None,
                      platform_reminders_sent="7,3,1")
    async with SessionLocal() as s:
        await platform.grant_days(s, await s.get(User, other_tenant), 30)
    assert (await _user(other_tenant)).platform_reminders_sent == ""


async def test_expired_notice_sent_once_before_bot_stops(session, other_tenant, emails, sent_messages):
    await _setup_user(other_tenant, utcnow() - timedelta(minutes=5))
    calls = []

    class FakeManager:
        async def stop(self, tenant_id, reason=None):
            # O aviso pelo Telegram precisa sair antes de o bot parar.
            calls.append(("stop", tenant_id, reason, len(sent_messages)))
            runtime.bots.pop(tenant_id, None)

        async def start_tenant(self, tenant_id):
            calls.append(("start", tenant_id))

    runtime.bots[other_tenant] = object()
    await scheduler.enforce_subscriptions(FakeManager())
    await scheduler.enforce_subscriptions(FakeManager())

    assert calls == [("stop", other_tenant, SUBSCRIPTION_EXPIRED, 1)]
    assert [subject for _, subject, _ in emails] == ["Sua assinatura venceu: o bot foi parado"]
    assert "x" in (await _user(other_tenant)).platform_reminders_sent


async def test_email_failure_does_not_break(session, other_tenant, monkeypatch, sent_messages):
    def boom(*args):
        raise OSError("connection refused")

    monkeypatch.setattr(mailer, "_send_sync", boom)
    await _setup_user(other_tenant, utcnow() + timedelta(hours=10))
    async with SessionLocal() as s:
        await platform.send_reminders(s)
    assert len(sent_messages) == 1  # o Telegram saiu mesmo com o e-mail falhando


# ------------------------------------------------------ renovação automática


class FakeMP:
    preapproval = {}
    created = None
    cancelled = None

    async def create_preapproval(self, **kwargs):
        FakeMP.created = kwargs
        return {"id": "pre-77", "status": "pending", "init_point": "https://mp.test/autorizar"}

    async def get_preapproval(self, preapproval_id):
        await asyncio.sleep(0)
        return FakeMP.preapproval

    async def cancel_preapproval(self, preapproval_id):
        FakeMP.cancelled = preapproval_id
        return {}


@pytest.fixture
def fake_mp(monkeypatch):
    FakeMP.preapproval, FakeMP.created, FakeMP.cancelled = {}, None, None
    monkeypatch.setattr(platform, "_client", lambda settings: FakeMP())
    return FakeMP


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


def _login(client):
    client.cookies.clear()
    client.post("/login", data=B_LOGIN)


def test_recurrence_frequency():
    assert platform.recurrence(30) == (1, "months")
    assert platform.recurrence(90) == (3, "months")
    assert platform.recurrence(45) == (45, "days")


def test_start_recurring_redirects_to_mercado_pago(client, other_tenant, fake_mp):
    client.portal.call(_setup_user, other_tenant, None)
    _login(client)
    r = client.post("/assinatura/pagar", data={"plan_code": "trimestral", "method": "card_recurring"},
                    follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "https://mp.test/autorizar"
    assert fake_mp.created["frequency"] == 3 and fake_mp.created["frequency_type"] == "months"
    user = client.portal.call(_user, other_tenant)
    assert (user.platform_preapproval_id, user.platform_recurring_plan) == ("pre-77", "trimestral")
    assert "Aguardando a autorização" in client.get("/assinatura").text


async def test_recurring_charge_renews_once_even_concurrently(session, other_tenant, fake_mp):
    await _setup_user(other_tenant, None, platform_preapproval_id="pre-77", platform_recurring_plan="mensal",
                      platform_recurring_status="pending")
    fake_mp.preapproval = {"status": "authorized",
                           "summarized": {"last_charged_date": "2026-11-01T10:00:00.000-03:00", "last_charged_amount": 49.9}}

    async def sync():
        async with SessionLocal() as s:
            try:
                return await platform.sync_recurring(s, await s.get(User, other_tenant))
            except Exception:  # noqa: BLE001 - no SQLite a 2ª pode bater na unique
                return False

    results = await asyncio.gather(sync(), sync())
    assert sorted(results) == [False, True]
    user = await _user(other_tenant)
    assert user.recurring_active
    left = user.subscription_until - utcnow()
    assert timedelta(days=29, hours=23) < left <= timedelta(days=30)
    assert await sync() is False  # mesma cobrança, nada novo

    async with SessionLocal() as s:
        n = await s.scalar(
            select(func.count()).select_from(PlatformPayment).where(PlatformPayment.method == "card_recurring")
        )
    assert n == 1


async def test_recurring_grace_keeps_bot_for_three_days(session, other_tenant):
    await _setup_user(other_tenant, utcnow() - timedelta(days=1), platform_recurring_status="authorized")
    assert (await _user(other_tenant)).subscription_active
    await _setup_user(other_tenant, utcnow() - timedelta(days=4), platform_recurring_status="authorized")
    assert not (await _user(other_tenant)).subscription_active
    await _setup_user(other_tenant, utcnow() - timedelta(hours=1), platform_recurring_status="cancelled")
    assert not (await _user(other_tenant)).subscription_active


async def test_last_chance_sync_keeps_bot_running(session, other_tenant, fake_mp, emails):
    await _setup_user(other_tenant, utcnow() - timedelta(days=4), platform_preapproval_id="pre-77",
                      platform_recurring_plan="mensal", platform_recurring_status="authorized",
                      platform_last_charge_at=utcnow() - timedelta(days=34))
    fake_mp.preapproval = {"status": "authorized", "summarized": {"last_charged_date": utcnow().isoformat()}}
    calls = []

    class FakeManager:
        async def stop(self, tenant_id, reason=None):
            calls.append("stop")

        async def start_tenant(self, tenant_id):
            calls.append("start")

    runtime.bots[other_tenant] = object()
    await scheduler.enforce_subscriptions(FakeManager())
    assert calls == ["start"] and emails == []
    assert (await _user(other_tenant)).subscription_active


def test_cancel_recurring(client, other_tenant, fake_mp):
    client.portal.call(partial(_setup_user, other_tenant, utcnow() + timedelta(days=10),
                               platform_preapproval_id="pre-77", platform_recurring_status="authorized"))
    _login(client)
    assert "Cancelar renovação automática" in client.get("/assinatura").text
    page = client.post("/assinatura/recorrencia/cancelar").text
    assert "Renovação automática cancelada" in page and fake_mp.cancelled == "pre-77"
    assert client.portal.call(_user, other_tenant).platform_recurring_status == "cancelled"
    assert client.post("/assinatura/recorrencia/cancelar").status_code == 404


def test_platform_webhook_subscription_topic_renews(client, other_tenant, fake_mp):
    client.portal.call(partial(_setup_user, other_tenant, None, platform_preapproval_id="pre-77",
                               platform_recurring_plan="semestral", platform_recurring_status="pending"))
    fake_mp.preapproval = {"status": "authorized", "summarized": {"last_charged_date": utcnow().isoformat()}}
    r = client.post("/webhooks/mercadopago/plataforma", json={"type": "subscription_preapproval", "data": {"id": "pre-77"}})
    assert r.json() == {"ok": True}
    left = client.portal.call(_user, other_tenant).subscription_until - utcnow()
    assert timedelta(days=179, hours=23) < left <= timedelta(days=180)


def test_admin_test_email(client, emails):
    client.post("/login", data={"username": "admin", "password": "senha-teste"})
    page = client.post("/admin/plataforma/testar-email", data={"test_to": "eu@example.com"}).text
    assert "Configure e salve o servidor" in page

    async def configure():
        async with SessionLocal() as s:
            await platform.set_platform_settings(s, {"smtp_host": "smtp.teste", "smtp_from": "p@teste"})

    client.portal.call(configure)
    page = client.post("/admin/plataforma/testar-email", data={"test_to": "eu@example.com"}).text
    assert "E-mail de teste enviado para eu@example.com" in page
    assert emails[0][:2] == ("eu@example.com", "Teste de e-mail da plataforma")
