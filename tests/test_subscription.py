"""Assinatura da plataforma: vencida, só o bot para; o painel continua para renovar."""

import asyncio
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import runtime, scheduler
from app.bot.runner import SUBSCRIPTION_EXPIRED
from app.db import SessionLocal
from app.main import app, bots
from app.models import PlatformPayment, User
from app.services import platform
from app.utils import utcnow
from tests.tenant import TID

B_LOGIN = {"username": "outro", "password": "senha-do-outro"}


async def _set_until(user_id: int, until) -> None:
    async with SessionLocal() as s:
        user = await s.get(User, user_id)
        user.subscription_until = until
        user.email = "b@example.com"
        await s.commit()


async def _user(user_id: int) -> User:
    async with SessionLocal() as s:
        return await s.get(User, user_id)


class FakeMP:
    """Mercado Pago da plataforma: aprova quando `paid` estiver True."""

    paid = False
    created = 0
    last_ref = ""

    async def create_pix(self, **kwargs):
        FakeMP.created += 1
        return {"id": 900 + FakeMP.created,
                "point_of_interaction": {"transaction_data": {"qr_code": "PIX-COPIA-COLA", "qr_code_base64": "iVBOR"}}}

    async def get_payment(self, payment_id):
        return {"id": payment_id, "status": "approved" if FakeMP.paid else "pending",
                "external_reference": FakeMP.last_ref, "date_approved": None}

    async def search_payments(self, ref):
        return []


@pytest.fixture
def fake_mp(monkeypatch):
    FakeMP.paid, FakeMP.created, FakeMP.last_ref = False, 0, ""
    monkeypatch.setattr(platform, "_client", lambda settings: FakeMP())
    return FakeMP


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


def _login(client, creds):
    client.cookies.clear()
    assert client.post("/login", data=creds, follow_redirects=False).headers["location"] == "/"


# -------------------------------------------------------------- regras


async def test_superadmin_is_exempt_and_new_user_needs_subscription(session, other_tenant):
    assert (await _user(TID)).subscription_active
    assert not (await _user(other_tenant)).subscription_active  # criado no teste sem trial


def test_new_user_gets_trial_days(client):
    _login(client, {"username": "admin", "password": "senha-teste"})
    client.post("/admin/usuarios", data={"username": "novo", "password": "senha-do-novo"})

    async def until():
        async with SessionLocal() as s:
            return await s.scalar(select(User.subscription_until).where(User.username == "novo"))

    left = client.portal.call(until) - utcnow()
    assert timedelta(days=6, hours=23) < left <= timedelta(days=7)


async def test_expired_tenant_bot_does_not_start(session, other_tenant):
    await _set_until(other_tenant, utcnow() - timedelta(minutes=1))
    await bots.start(other_tenant, "123:TOKEN-QUALQUER")  # nem chega a falar com o Telegram
    assert runtime.get_bot(other_tenant) is None
    assert runtime.errors[other_tenant] == SUBSCRIPTION_EXPIRED


async def test_scheduler_pauses_expired_area_and_stops_bot(session, other_tenant):
    await _set_until(other_tenant, utcnow() - timedelta(minutes=1))
    assert other_tenant not in await scheduler.active_tenants()
    assert TID in await scheduler.active_tenants()

    calls = []

    class FakeManager:
        async def stop(self, tenant_id, reason=None):
            calls.append(("stop", tenant_id, reason))
            runtime.bots.pop(tenant_id, None)

        async def start_tenant(self, tenant_id):
            calls.append(("start", tenant_id))

    runtime.bots[other_tenant] = object()  # bot rodando
    await scheduler.enforce_subscriptions(FakeManager())
    assert calls == [("stop", other_tenant, SUBSCRIPTION_EXPIRED)]


async def test_renewal_adds_to_current_expiry_or_from_now(session, other_tenant):
    future = utcnow() + timedelta(days=10)
    await _set_until(other_tenant, future)
    async with SessionLocal() as s:
        await platform.grant_days(s, await s.get(User, other_tenant), 30)
    assert abs((await _user(other_tenant)).subscription_until - (future + timedelta(days=30))) < timedelta(seconds=5)

    await _set_until(other_tenant, utcnow() - timedelta(days=5))
    async with SessionLocal() as s:
        await platform.grant_days(s, await s.get(User, other_tenant), 30)
    left = (await _user(other_tenant)).subscription_until - utcnow()
    assert timedelta(days=29, hours=23) < left <= timedelta(days=30)


async def test_concurrent_platform_approval_extends_once(session, other_tenant):
    await _set_until(other_tenant, None)
    async with SessionLocal() as s:
        payment = PlatformPayment(user_id=other_tenant, plan_code="mensal", days=30, amount_cents=4990,
                                  method="pix", external_ref="plat-x")
        s.add(payment)
        await s.commit()
        payment_id = payment.id

    async def approve():
        async with SessionLocal() as s:
            return await platform.approve(s, await s.get(PlatformPayment, payment_id))

    assert sorted(await asyncio.gather(approve(), approve(), approve())) == [False, False, True]
    left = (await _user(other_tenant)).subscription_until - utcnow()
    assert timedelta(days=29, hours=23) < left <= timedelta(days=30)


# ------------------------------------------------------------- painel


def test_expired_user_still_uses_panel_with_banner(client, other_tenant):
    client.portal.call(_set_until, other_tenant, utcnow() - timedelta(days=1))
    _login(client, B_LOGIN)
    for path in ("/", "/membros", "/planos", "/config", "/assinatura"):
        r = client.get(path)
        assert r.status_code == 200, path
        assert "bot está parado" in r.text, path


def test_pay_with_pix_and_confirm(client, other_tenant, fake_mp):
    async def configure():
        async with SessionLocal() as s:
            await platform.set_platform_settings(s, {"mp_access_token": "APP_USR-plataforma"})

    client.portal.call(configure)
    client.portal.call(_set_until, other_tenant, utcnow() - timedelta(days=1))
    _login(client, B_LOGIN)

    r = client.post("/assinatura/pagar", data={"plan_code": "trimestral", "method": "pix"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/assinatura/pagamento/")
    payment_url = r.headers["location"]
    page = client.get(payment_url).text
    assert "PIX-COPIA-COLA" in page and "data:image/png;base64,iVBOR" in page

    # Pedir de novo reaproveita a mesma cobrança.
    again = client.post("/assinatura/pagar", data={"plan_code": "trimestral", "method": "pix"}, follow_redirects=False)
    assert again.headers["location"] == payment_url and fake_mp.created == 1

    async def ref():
        async with SessionLocal() as s:
            return (await s.get(PlatformPayment, int(payment_url.rsplit("/", 1)[1]))).external_ref

    fake_mp.last_ref = client.portal.call(ref)
    assert "Ainda não identificamos" in client.post(f"{payment_url}/verificar").text
    fake_mp.paid = True
    assert "Assinatura renovada" in client.post(f"{payment_url}/verificar").text

    left = client.portal.call(_user, other_tenant).subscription_until - utcnow()
    assert timedelta(days=89, hours=23) < left <= timedelta(days=90)
    assert "Sua assinatura está vencida" not in client.get("/").text


def test_pay_requires_email_and_valid_plan(client, other_tenant):
    _login(client, B_LOGIN)
    assert "Informe seu e-mail" in client.post("/assinatura/pagar", data={"plan_code": "mensal", "method": "pix"}).text
    assert client.post("/assinatura/pagar", data={"plan_code": "xpto", "method": "pix"}).status_code == 404
    assert client.post("/assinatura/pagar", data={"plan_code": "mensal", "method": "boleto"}).status_code == 404


def test_cannot_see_other_users_platform_payment(client, other_tenant):
    async def payment_of_superadmin():
        async with SessionLocal() as s:
            p = PlatformPayment(user_id=TID, plan_code="mensal", days=30, amount_cents=4990, method="pix",
                                external_ref="plat-a", pix_code="PIX-DO-ADMIN")
            s.add(p)
            await s.commit()
            return p.id

    pid = client.portal.call(payment_of_superadmin)
    _login(client, B_LOGIN)
    assert client.get(f"/assinatura/pagamento/{pid}").status_code == 404
    assert client.post(f"/assinatura/pagamento/{pid}/verificar").status_code == 404


def test_superadmin_grants_days_and_edits_platform(client, other_tenant):
    _login(client, {"username": "admin", "password": "senha-teste"})
    client.post(f"/admin/usuarios/{other_tenant}/dias", data={"days": "15"})
    left = client.portal.call(_user, other_tenant).subscription_until - utcnow()
    assert timedelta(days=14, hours=23) < left <= timedelta(days=15)

    page = client.post("/admin/plataforma", data={
        "trial_days": "3", "mensal_name": "Mensal", "mensal_price": "59,90", "mensal_days": "30", "mensal_active": "on",
        "trimestral_name": "Tri", "trimestral_price": "150,00", "trimestral_days": "90",
        "semestral_name": "Sem", "semestral_price": "280,00", "semestral_days": "180", "semestral_active": "on",
    }).text
    assert "Configurações da plataforma salvas" in page and "59,90" in page

    async def enable_payments():
        async with SessionLocal() as s:
            await platform.set_platform_settings(s, {"mp_access_token": "APP_USR-plataforma"})

    client.portal.call(enable_payments)
    _login(client, B_LOGIN)
    page = client.get("/assinatura").text
    assert "R$ 59,90" in page and "R$ 150,00" not in page  # trimestral desativado
    assert client.get("/admin/plataforma").status_code == 404
    assert client.post(f"/admin/usuarios/{other_tenant}/dias", data={"days": "999"}).status_code == 404


def test_platform_webhook_route_is_not_taken_by_tenant_route(client, other_tenant, fake_mp):
    async def pending():
        async with SessionLocal() as s:
            await platform.set_platform_settings(s, {"mp_access_token": "APP_USR-plataforma"})
            p = PlatformPayment(user_id=other_tenant, plan_code="mensal", days=30, amount_cents=4990,
                                method="pix", external_ref="plat-wh", mp_id="77")
            s.add(p)
            await s.commit()

    client.portal.call(pending)
    client.portal.call(_set_until, other_tenant, None)
    fake_mp.paid, fake_mp.last_ref = True, "plat-wh"
    r = client.post("/webhooks/mercadopago/plataforma", json={"type": "payment", "data": {"id": "77"}})
    assert r.json() == {"ok": True}
    assert client.portal.call(_user, other_tenant).subscription_active


def test_tenant_webhook_ignored_while_subscription_expired(client, other_tenant, monkeypatch):
    from app.services import payments

    called = []

    async def fake_handle(*args):
        called.append(args)

    monkeypatch.setattr(payments, "handle_webhook", fake_handle)
    client.portal.call(_set_until, other_tenant, utcnow() - timedelta(days=1))
    key = client.portal.call(_user, other_tenant).webhook_key
    assert client.post(f"/webhooks/mercadopago/{key}", json={"type": "payment", "data": {"id": "1"}}).json() == {"ok": True}
    assert called == []
