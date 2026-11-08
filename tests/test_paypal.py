"""PayPal em moeda estrangeira: configuração, cobrança, conciliação, webhook e estatísticas."""

import asyncio
import json
from datetime import timedelta
from functools import partial

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.bot import handlers_member
from app.bot.keyboards import available_methods, methods_kb
from app.db import SessionLocal
from app.main import app
from app.models import Member, Payment, Plan, User
from app.services import payments, paypal, stats
from app.services.store import get_settings, set_settings
from app.utils import money, parse_brl, utcnow
from tests.tenant import TID

A_LOGIN = {"username": "admin", "password": "senha-teste"}
CREDS = {"enable_paypal": "1", "paypal_client_id": "client", "paypal_client_secret": "segredo-do-app",
         "paypal_currency": "USD"}


class FakePayPal:
    """API do PayPal: `order` é o que o GET devolve; `captured` o que a captura devolve."""

    order = {}
    captured = {}
    captures = 0
    created = None

    async def create_order(self, **kwargs):
        FakePayPal.created = kwargs
        return {"id": "ORDER1", "status": "PAYER_ACTION_REQUIRED",
                "links": [{"rel": "payer-action", "href": "https://paypal.test/aprovar"}]}

    async def get_order(self, order_id):
        await asyncio.sleep(0)
        return FakePayPal.order

    async def capture_order(self, order_id):
        FakePayPal.captures += 1
        return FakePayPal.captured


def _captured(status="COMPLETED", value="19.90", currency="USD"):
    return {"id": "ORDER1", "status": "COMPLETED", "purchase_units": [
        {"payments": {"captures": [{"status": status, "create_time": "2026-11-07T12:00:00Z",
                                    "amount": {"currency_code": currency, "value": value}}]}}]}


@pytest.fixture
def fake_paypal(monkeypatch):
    FakePayPal.order, FakePayPal.captured, FakePayPal.captures = {}, {}, 0
    monkeypatch.setattr(payments, "paypal_client", lambda settings: FakePayPal())
    return FakePayPal


@pytest.fixture
def client():
    with TestClient(app) as c:
        c.post("/login", data=A_LOGIN)
        yield c


async def _setup(tenant_id=TID, order_id="ORDER1", **payment_fields):
    """Área com PayPal ligado e um membro com cobrança pendente de USD 19.90."""
    async with SessionLocal() as s:
        await set_settings(s, tenant_id, CREDS)
        plan = await s.get(Plan, (tenant_id, "mensal"))
        plan.foreign_price_cents = 1990
        member = Member(tenant_id=tenant_id, telegram_id=777, first_name="Ann")
        s.add(member)
        await s.flush()
        fields = {"tenant_id": tenant_id, "member_id": member.id, "plan_code": "mensal", "amount_cents": 1990,
                  "currency": "USD", "method": "paypal", "external_ref": f"pp-{tenant_id}", "mp_id": order_id}
        payment = Payment(**(fields | payment_fields))
        s.add(payment)
        await s.commit()
        return payment.id


async def _state(payment_id):
    async with SessionLocal() as s:
        payment = await s.get(Payment, payment_id)
        return payment, await s.get(Member, payment.member_id)


async def _sync(payment_id):
    async with SessionLocal() as s:
        payment = await s.get(Payment, payment_id)
        await payments.sync_paypal(s, await get_settings(s, payment.tenant_id), payment)


async def _key(tenant_id):
    async with SessionLocal() as s:
        return (await s.get(User, tenant_id)).webhook_key


# ------------------------------------------------------------------ cliente


def test_helpers():
    assert (paypal.to_value(1990), paypal.to_cents("19.90"), paypal.to_cents("abc")) == ("19.90", 1990, None)
    assert paypal.approval_link({"links": [{"rel": "payer-action", "href": "a"}]}) == "a"
    assert paypal.approval_link({"links": [{"rel": "approve", "href": "b"}]}) == "b"
    assert money(129990, "USD") == "USD 1,299.90" and money(2990) == "R$ 29,90"
    assert parse_brl("1,299.90") == 129990 and parse_brl("1.299,90") == 129990 and parse_brl("19.90") == 1990


@pytest.fixture
def api(monkeypatch):
    """Intercepta o HTTP do cliente real."""
    calls = []
    real = httpx.AsyncClient

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.path == "/v1/oauth2/token":
            return httpx.Response(200, json={"access_token": "tok"})
        return httpx.Response(200, json={"id": "O1", "status": "PAYER_ACTION_REQUIRED"})

    monkeypatch.setattr(paypal.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    return calls


async def test_create_order_request(api):
    await paypal.PayPal("id", "sec", sandbox=True).create_order(
        amount_cents=1990, currency="EUR", description="Assinatura", reference="ref1", return_url="https://t.me/bot"
    )
    token, order = api
    assert token.url.host == "api-m.sandbox.paypal.com" and order.url.path == "/v2/checkout/orders"
    assert order.headers["authorization"] == "Bearer tok" and order.headers["paypal-request-id"] == "order-ref1"
    body = json.loads(order.content)
    assert body["intent"] == "CAPTURE"
    assert body["purchase_units"][0]["amount"] == {"currency_code": "EUR", "value": "19.90"}
    context = body["payment_source"]["paypal"]["experience_context"]
    assert context["return_url"] == "https://t.me/bot" and context["shipping_preference"] == "NO_SHIPPING"


async def test_capture_is_idempotent_and_live_host(api):
    await paypal.PayPal("id", "sec").capture_order("O1")
    assert api[1].url.host == "api-m.paypal.com"
    assert api[1].headers["paypal-request-id"] == "capture-O1"


async def test_bad_credentials_do_not_leak_secret(monkeypatch):
    real = httpx.AsyncClient
    transport = httpx.MockTransport(lambda request: httpx.Response(401, json={"error": "invalid_client"}))
    monkeypatch.setattr(paypal.httpx, "AsyncClient", lambda **kw: real(transport=transport, **kw))
    with pytest.raises(paypal.PayPalError) as exc:
        await paypal.PayPal("id", "segredo").get_token()
    assert "segredo" not in str(exc.value)


# ---------------------------------------------------------- opções no bot


async def test_paypal_offered_only_when_configured(session):
    plan = await session.get(Plan, (TID, "mensal"))
    assert "paypal" not in available_methods(plan, await get_settings(session, TID))
    await set_settings(session, TID, CREDS)
    settings = await get_settings(session, TID)
    assert "paypal" not in available_methods(plan, settings)  # plano sem preço no PayPal
    plan.foreign_price_cents = 1990
    assert "paypal" in available_methods(plan, settings)
    await set_settings(session, TID, {"enable_paypal": "0"})
    assert "paypal" not in available_methods(plan, await get_settings(session, TID))

    await set_settings(session, TID, {"enable_paypal": "1"})
    recurring = await session.get(Plan, (TID, "mensal_recorrente"))
    recurring.foreign_price_cents = 1990
    assert "paypal" not in available_methods(recurring, await get_settings(session, TID))


def test_method_button_shows_foreign_price():
    plan = Plan(code="mensal", name="Mensal", price_cents=2990, days=30, foreign_price_cents=1990)
    assert methods_kb(plan, ["paypal"], "EUR").inline_keyboard[0][0].text == "PayPal — EUR 19.90"


class FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, reply_markup=None):
        self.sent.append((text, reply_markup))


async def test_bot_sends_paypal_link(session, fake_paypal):
    payment_id = await _setup()
    async with SessionLocal() as s:
        await s.delete(await s.get(Payment, payment_id))  # sem cobrança recente
        await s.commit()
    bot = FakeBot()
    await handlers_member._start_payment(bot, TID, 777, "mensal", "paypal", None)
    text, markup = bot.sent[0]
    assert "USD 19.90" in text and markup.inline_keyboard[0][0].url == "https://paypal.test/aprovar"
    assert fake_paypal.created["currency"] == "USD" and fake_paypal.created["amount_cents"] == 1990
    async with SessionLocal() as s:
        payment = await s.scalar(select(Payment).where(Payment.method == "paypal"))
    assert (payment.mp_id, payment.currency, payment.amount_cents, payment.status) == ("ORDER1", "USD", 1990, "pending")


# -------------------------------------------------------------- conciliação


async def test_approved_order_is_captured_and_released_once(session, fake_paypal):
    payment_id = await _setup()
    fake_paypal.order = {"status": "APPROVED"}
    fake_paypal.captured = _captured()
    await asyncio.gather(_sync(payment_id), _sync(payment_id), _sync(payment_id))
    payment, member = await _state(payment_id)
    assert payment.status == "approved" and member.status == "active"
    left = member.expires_at - utcnow()
    assert timedelta(days=29, hours=23) < left <= timedelta(days=30)  # liberou uma vez só


async def test_not_approved_by_buyer_stays_pending(session, fake_paypal):
    payment_id = await _setup()
    fake_paypal.order = {"status": "PAYER_ACTION_REQUIRED"}
    await _sync(payment_id)
    assert (await _state(payment_id))[0].status == "pending" and fake_paypal.captures == 0


@pytest.mark.parametrize("captured", [_captured("PENDING"), _captured(value="1.00"), _captured(currency="BRL")])
async def test_capture_not_released_when_pending_or_different_amount(session, fake_paypal, captured):
    payment_id = await _setup()
    fake_paypal.order = captured
    await _sync(payment_id)
    payment, member = await _state(payment_id)
    assert payment.status == "pending" and member.status != "active"


async def test_declined_capture_rejects(session, fake_paypal):
    payment_id = await _setup()
    fake_paypal.order = _captured("DECLINED")
    await _sync(payment_id)
    assert (await _state(payment_id))[0].status == "rejected"


async def test_scheduler_checks_paypal_without_mercado_pago(session, fake_paypal):
    payment_id = await _setup()
    fake_paypal.order = _captured()
    async with SessionLocal() as s:
        await payments.check_pending(s, TID)
    assert (await _state(payment_id))[0].status == "approved"


async def test_paypal_failure_does_not_stop_scheduler(session, monkeypatch):
    class Down:
        async def get_order(self, order_id):
            raise paypal.PayPalError("fora do ar")

    payment_id = await _setup()
    monkeypatch.setattr(payments, "paypal_client", lambda settings: Down())
    async with SessionLocal() as s:
        await payments.check_pending(s, TID)
    assert (await _state(payment_id))[0].status == "pending"


# ------------------------------------------------------------------ webhook


def _hook(client, key, order_id="ORDER1"):
    return client.post(f"/webhooks/paypal/{key}", json={
        "event_type": "PAYMENT.CAPTURE.COMPLETED",
        "resource": {"id": "CAP1", "supplementary_data": {"related_ids": {"order_id": order_id}}},
    })


def test_webhook_releases_after_checking_the_api(client, fake_paypal):
    payment_id = client.portal.call(_setup)
    fake_paypal.order = _captured()
    assert _hook(client, client.portal.call(_key, TID)).json() == {"ok": True}
    assert client.portal.call(_state, payment_id)[0].status == "approved"


def test_forged_webhook_does_not_release(client, fake_paypal):
    payment_id = client.portal.call(_setup)
    fake_paypal.order = {"status": "PAYER_ACTION_REQUIRED"}  # a API diz que não foi pago
    _hook(client, client.portal.call(_key, TID))
    assert client.portal.call(_state, payment_id)[0].status == "pending"


def test_webhook_of_one_area_does_not_touch_another(client, fake_paypal, other_tenant):
    payment_id = client.portal.call(_setup, other_tenant)
    fake_paypal.order = _captured()
    _hook(client, client.portal.call(_key, TID))  # chave do usuário errado
    assert client.portal.call(_state, payment_id)[0].status == "pending"
    assert _hook(client, "chave-que-nao-existe").status_code == 404


def test_webhook_completes_expired_capture(client, fake_paypal):
    payment_id = client.portal.call(partial(_setup, status="expired"))
    fake_paypal.order = _captured()
    _hook(client, client.portal.call(_key, TID))
    assert client.portal.call(_state, payment_id)[0].status == "approved"


# -------------------------------------------------------------------- painel


def _config_form(**extra):
    return {"paypal_client_id": "abc", "paypal_client_secret": "segredo-do-app", "paypal_currency": "EUR",
            "enable_paypal": "on", "paypal_sandbox": "on", "grace_hours": "24"} | extra


def test_config_saves_and_never_shows_secret(client):
    page = client.post("/config", data=_config_form()).text
    assert "Configurações salvas" in page and "segredo-do-app" not in page
    assert 'value="abc"' in page and "EUR · Euro" in page

    async def saved():
        async with SessionLocal() as s:
            return await get_settings(s, TID)

    settings = client.portal.call(saved)
    assert settings["paypal_client_secret"] == "segredo-do-app" and settings.flag("paypal_sandbox")
    assert settings["paypal_currency"] == "EUR"

    client.post("/config", data=_config_form(paypal_client_secret="", paypal_currency="XXX"))
    settings = client.portal.call(saved)
    assert settings["paypal_client_secret"] == "segredo-do-app"  # em branco mantém
    assert settings["paypal_currency"] == "EUR"  # moeda fora da lista é ignorada


def test_test_connection(client, monkeypatch):
    class Ok:
        def __init__(self, *args):
            pass

        async def get_token(self):
            return "tok"

    assert "falhou" in client.post("/config/testar-paypal", data=_config_form(paypal_client_id="")).text
    monkeypatch.setattr(payments, "PayPal", Ok)
    assert "PayPal conectado (Sandbox)" in client.post("/config/testar-paypal", data=_config_form()).text


def test_plan_foreign_price(client):
    async def plan(code):
        async with SessionLocal() as s:
            return await s.get(Plan, (TID, code))

    base = {"mensal_name": "Mensal", "mensal_price": "29,90", "mensal_days": "30"}
    client.post("/planos", data=base | {"mensal_foreign": "19.90"})
    assert client.portal.call(plan, "mensal").foreign_price_cents == 1990
    assert 'value="19.90"' in client.get("/planos").text
    client.post("/planos", data=base | {"mensal_foreign": ""})
    assert client.portal.call(plan, "mensal").foreign_price_cents is None
    assert "valor no PayPal inválido" in client.post("/planos", data=base | {"mensal_foreign": "0.50"}).text

    client.post("/planos/novo", data={"new_name": "Global", "new_price": "50", "new_days": "30", "new_foreign": "9.99"})
    assert client.portal.call(plan, "global").foreign_price_cents == 999


async def test_foreign_revenue_is_not_added_to_reais(session):
    await _setup(status="approved", paid_at=utcnow())
    async with SessionLocal() as s:
        data = await stats.compute(s, TID)
    assert data["revenue_month"] == 0 and data["foreign_month"] == {"USD": 1990}


def test_payments_page_shows_currency(client):
    client.portal.call(_setup)
    page = client.get("/pagamentos").text
    assert "USD 19.90" in page and "Verificar no PayPal" in page


async def test_sale_notice_uses_foreign_currency(session, fake_paypal, sent_messages):
    payment_id = await _setup()
    async with SessionLocal() as s:
        await set_settings(s, TID, {"admin_ids": "4242"})
    fake_paypal.order = _captured()
    await _sync(payment_id)
    assert any(chat == 4242 and "USD 19.90" in text for chat, text in sent_messages)
