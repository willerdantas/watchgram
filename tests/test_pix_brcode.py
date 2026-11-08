"""Pix manual com QR Code: BR Code estático com o valor do plano, só quando o dono liga a opção."""

import pytest
from fastapi.testclient import TestClient

from app.bot import handlers_member
from app.db import SessionLocal
from app.main import app
from app.models import Member, Plan
from app.services import brcode, payments
from app.services.store import get_settings, set_settings
from tests.tenant import TID

PIX = {"pix_key": "fulano@exemplo.com", "pix_holder": "José da Conceição", "pix_city": "São Paulo"}


def test_matches_bcb_manual_example():
    payload = brcode.build("123e4567-e12b-12d1-a456-426655440000", "Fulano de Tal", "BRASILIA")
    assert payload == (
        "00020126580014br.gov.bcb.pix0136123e4567-e12b-12d1-a456-4266554400005204000053039865802BR"
        "5913Fulano de Tal6008BRASILIA62070503***63041D3D"
    )


def test_amount_txid_and_normalization():
    payload = brcode.build("+55 (11) 99999-8888", "José da Conceição Ltda e Filhos", "São Paulo", 2990, "VIP-42")
    assert "0114+5511999998888" in payload
    assert "54052990" not in payload and "540529.90" in payload
    assert "5925Jose da Conceicao Ltda e " not in payload  # corta em 25 sem deixar espaço no fim
    assert "5924Jose da Conceicao Ltda e" in payload
    assert "6009Sao Paulo" in payload and "62090505VIP42" in payload
    assert payload[-4:] == brcode.crc16(payload[:-4])
    assert brcode.normalize_key("123.456.789-09") == "12345678909"
    assert brcode.normalize_key(" Fulano@Exemplo.com ") == "fulano@exemplo.com"


def test_requires_key_holder_and_city():
    with pytest.raises(ValueError):
        brcode.build("chave", "Titular", "")
    with pytest.raises(ValueError):
        brcode.build("x" * 78, "Titular", "Cidade")
    assert brcode.missing_fields({"pix_key": "k", "pix_holder": " ", "pix_city": ""}) == ["nome do titular", "cidade"]


def test_qr_is_png():
    assert brcode.qr_png(brcode.build(**{"key": "a@b.com", "name": "A", "city": "B"}, amount_cents=100))[:8] == b"\x89PNG\r\n\x1a\n"


async def _member_and_plan(session):
    member = Member(tenant_id=TID, telegram_id=555, first_name="Ana")
    session.add(member)
    await session.commit()
    return member, await session.get(Plan, (TID, "mensal"))


async def test_manual_pix_code_only_when_enabled(session):
    member, plan = await _member_and_plan(session)
    await set_settings(session, TID, PIX)
    off = await payments.start_manual_pix(session, member, plan, await get_settings(session, TID))
    assert off.method == "pix_manual" and off.pix_code is None

    await set_settings(session, TID, {"pix_manual_qr": "1"})
    on = await payments.start_manual_pix(session, member, plan, await get_settings(session, TID))
    assert on.method == "pix_manual" and on.status == "pending"  # continua dependendo do comprovante
    assert f"54{len(f'{plan.price_cents / 100:.2f}'):02d}{plan.price_cents / 100:.2f}" in on.pix_code
    assert f"VIP{on.id}" in on.pix_code and "0118fulano@exemplo.com" in on.pix_code


class FakeBot:
    def __init__(self):
        self.sent, self.photos = [], []

    async def send_message(self, chat_id, text, reply_markup=None):
        self.sent.append(text)

    async def send_photo(self, chat_id, photo):
        self.photos.append(photo)


class FakeState:
    def __init__(self):
        self.state, self.data = None, {}

    async def set_state(self, state):
        self.state = state

    async def update_data(self, **values):
        self.data.update(values)


@pytest.mark.parametrize("enabled", [False, True])
async def test_bot_sends_qr_only_when_enabled(session, enabled):
    member, plan = await _member_and_plan(session)
    await set_settings(session, TID, {**PIX, "enable_pix_manual": "1", "pix_manual_qr": "1" if enabled else "0"})
    bot, state = FakeBot(), FakeState()
    await handlers_member._start_payment(bot, TID, 555, plan.code, "pix_manual", state)

    assert state.state == handlers_member.Flow.proof and state.data["payment_id"]
    assert "comprovante" in bot.sent[-1]
    async with SessionLocal() as s:
        payment = await s.get(payments.Payment, state.data["payment_id"])
    if enabled:
        assert len(bot.photos) == 1 and payment.pix_code in bot.sent[-1]
    else:
        assert bot.photos == [] and payment.pix_code is None and "fulano@exemplo.com" in bot.sent[-1]


@pytest.fixture
def client():
    with TestClient(app) as c:
        c.post("/login", data={"username": "admin", "password": "senha-teste"})
        yield c


def _saved(client):
    async def read():
        async with SessionLocal() as s:
            return await get_settings(s, TID)

    return client.portal.call(read)


def test_config_turns_qr_on_only_with_complete_data(client):
    form = {"grace_hours": "24", "enable_pix_manual": "on", "pix_manual_qr": "on", "pix_key": "k@x.com", "pix_holder": "Ana"}
    page = client.post("/config", data=form).text
    assert "QR Code Pix não foi ligado: falta cidade" in page
    assert _saved(client).flag("pix_manual_qr") is False

    client.post("/config", data={**form, "pix_city": "Recife"})
    settings = _saved(client)
    assert settings.flag("pix_manual_qr") and settings.get("pix_city") == "Recife"
    assert 'name="pix_manual_qr" checked' in client.get("/config").text
