"""Elementos do Watchgram: página inicial, cadastro, primeiros passos, movimento do grupo, alertas e relatórios."""

import io
import json
import re
from datetime import timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from openpyxl import load_workbook
from sqlalchemy import func, select

from app.bot import handlers_group
from app.db import SessionLocal
from app.main import app
from app.models import Event, Member, Payment, User
from app.services import notifier, platform, reports
from app.services.store import get_settings, set_settings
from app.utils import utcnow
from app.web import auth, public
from tests.tenant import TID

A_LOGIN = {"username": "admin", "password": "senha-teste"}
B_LOGIN = {"username": "outro", "password": "senha-do-outro"}
TOKEN = "123456789:" + "A" * 35


@pytest.fixture
def client(monkeypatch):
    auth._signups.clear()
    monkeypatch.setattr(public, "MIN_FILL_SECONDS", 0)
    with TestClient(app) as c:
        yield c


def _signup(client, **data):
    """Como o navegador: abre o formulário (que traz o carimbo de tempo) e envia."""
    client.cookies.clear()
    stamp = re.search(r'name="stamp" value="([^"]+)"', client.get("/cadastro").text).group(1)
    return client.post("/cadastro", data={"stamp": stamp, **data}, follow_redirects=False)


def _login(client, creds=A_LOGIN):
    client.cookies.clear()
    return client.post("/login", data=creds)


async def _settings(tenant_id, values):
    async with SessionLocal() as s:
        await set_settings(s, tenant_id, values)


async def _get(tenant_id=TID):
    async with SessionLocal() as s:
        return await get_settings(s, tenant_id)


# ------------------------------------------------------- site e cadastro


def test_landing_and_pricing_are_public(client):
    page = client.get("/").text
    assert "Venda acesso ao seu grupo VIP" in page and 'href="/cadastro"' in page and 'class="sidebar"' not in page
    page = client.get("/precos").text
    assert "Mensal" in page and "R$ 49,90" in page and "Compare os planos" in page


def test_signup_creates_area_with_trial_and_logs_in(client):
    r = _signup(client, name="VIP da Ana", email="Ana@Exemplo.com", password="senha-forte-1")
    assert r.headers["location"] == "/primeiros-passos"
    assert "Conecte o seu bot" in client.get("/primeiros-passos").text

    async def created():
        async with SessionLocal() as s:
            return await s.scalar(select(User).where(User.email == "ana@exemplo.com"))

    user = client.portal.call(created)
    assert (user.username, user.name, user.is_superadmin) == ("ana", "VIP da Ana", False)
    left = user.subscription_until - utcnow()
    assert timedelta(days=6, hours=23) < left <= timedelta(days=7)

    # Entra com o e-mail; o mesmo e-mail não abre outra conta.
    client.cookies.clear()
    assert client.post("/login", data={"username": "ana@exemplo.com", "password": "senha-forte-1"},
                       follow_redirects=False).headers["location"] == "/"
    r = _signup(client, email="ana@exemplo.com", password="outra-senha")
    assert r.headers["location"] == "/login" and "Já existe uma conta" in client.get("/login").text


def test_signup_validation_honeypot_and_rate_limit(client):
    _signup(client, email="x", password="senha-forte-1")
    assert "e-mail válido" in client.get("/cadastro").text
    _signup(client, email="a@b.co", password="curta")
    assert "ao menos 8" in client.get("/cadastro").text
    _signup(client, email="robo@b.co", password="senha-forte-1", website="spam")  # armadilha preenchida
    client.cookies.clear()
    client.post("/cadastro", data={"email": "semcarimbo@b.co", "password": "senha-forte-1"})  # sem o formulário
    client.post("/cadastro", data={"email": "falso@b.co", "password": "senha-forte-1", "stamp": "ok.forjado.x"})
    for i in range(5):
        _signup(client, email=f"p{i}@b.co", password="senha-forte-1")
    _signup(client, email="p9@b.co", password="senha-forte-1")
    assert "Muitos cadastros" in client.get("/cadastro").text

    async def count():
        async with SessionLocal() as s:
            return await s.scalar(select(func.count(User.id)))

    assert client.portal.call(count) == 1 + 5  # admin + 5 cadastros; robô e excedente não


def test_signup_can_be_turned_off(client):
    async def off():
        async with SessionLocal() as s:
            await platform.set_platform_settings(s, {"signup_enabled": "0"})

    client.portal.call(off)
    assert client.get("/cadastro").status_code == 404
    assert client.post("/cadastro", data={"email": "a@b.co", "password": "senha-forte-1"}).status_code == 404
    assert 'href="/cadastro"' not in client.get("/").text


# ------------------------------------------------------- primeiros passos


def test_wizard_walks_the_three_steps(client):
    _login(client)
    assert "Código do seu bot" in client.get("/primeiros-passos").text
    assert "não parece o do @BotFather" in client.post("/primeiros-passos/bot", data={"bot_token": "abc"}).text
    page = client.post("/primeiros-passos/bot", data={"bot_token": TOKEN}).text
    assert "Seu bot foi conectado" in page and "Nenhum grupo encontrado ainda" in page

    client.portal.call(_settings, TID, {"detected_chats": json.dumps([{"id": -100777, "title": "Meu VIP", "type": "supergroup", "is_admin": True}])})
    assert "Meu VIP" in client.get("/primeiros-passos?etapa=2").text
    assert client.post("/primeiros-passos/grupo/-100999").status_code == 404  # grupo que o bot não viu
    page = client.post("/primeiros-passos/grupo/-100777").text
    assert "Como você quer receber?" in page

    assert "pelo menos uma forma" in client.post("/primeiros-passos/receber", data={}).text
    page = client.post("/primeiros-passos/receber", data={"pix_key": "chave@pix.com", "pix_holder": "Ana", "admin_ids": "4242"}).text
    assert "Tudo pronto!" in page
    settings = client.portal.call(_get)
    assert settings.group_chat_id == -100777 and settings.flag("enable_pix_manual") and settings.admin_ids == [4242]


# ---------------------------------------------- entradas e saídas do grupo


def _event(user_id, name="Ana"):
    return SimpleNamespace(chat=SimpleNamespace(id=-100777),
                           new_chat_member=SimpleNamespace(user=SimpleNamespace(id=user_id, is_bot=False, first_name=name, username=None)))


async def _member(telegram_id, status="active", in_group=False):
    async with SessionLocal() as s:
        member = Member(tenant_id=TID, telegram_id=telegram_id, first_name="Ana", status=status, in_group=in_group,
                        expires_at=utcnow() + timedelta(days=5))
        s.add(member)
        await s.commit()
        return member.id


async def _kinds():
    async with SessionLocal() as s:
        return (await s.scalars(select(Event.kind).where(Event.tenant_id == TID).order_by(Event.id))).all()


async def test_join_and_leave_are_recorded_once_and_alerted(session, sent_messages):
    await _settings(TID, {"group_chat_id": "-100777", "admin_ids": "4242", "alert_joined": "1", "alert_left": "1"})
    await _member(555)
    await handlers_group.on_join(_event(555), tenant_id=TID)
    await handlers_group.on_join(_event(555), tenant_id=TID)  # atualização repetida do Telegram
    await handlers_group.on_leave(_event(555), tenant_id=TID)
    await handlers_group.on_leave(_event(555), tenant_id=TID)
    assert await _kinds() == ["joined", "left"]
    texts = [text for chat, text in sent_messages if chat == 4242]
    assert len(texts) == 2 and "Ana entrou no grupo" in texts[0] and "Ana saiu do grupo" in texts[1]


async def test_kick_records_left_and_leave_update_does_not_repeat(session, monkeypatch):
    await _settings(TID, {"group_chat_id": "-100777"})
    member_id = await _member(556, in_group=True)

    class Bot:
        async def ban_chat_member(self, *a):
            pass

        async def unban_chat_member(self, *a, **k):
            pass

    from app import runtime

    runtime.bots[TID] = Bot()
    monkeypatch.undo()  # usa o kick de verdade (o conftest o substitui)
    async with SessionLocal() as s:
        member = await s.get(Member, member_id)
        assert await notifier.kick(s, await get_settings(s, TID), member)
        await s.commit()
    await handlers_group.on_leave(_event(556), tenant_id=TID)
    assert await _kinds() == ["left"]


async def test_many_exits_alert_once_a_day(session, sent_messages):
    await _settings(TID, {"group_chat_id": "-100777", "admin_ids": "4242", "alert_exits": "1", "alert_exits_min": "2"})
    for tg in (601, 602, 603):
        await _member(tg, in_group=True)
        await handlers_group.on_leave(_event(tg), tenant_id=TID)
    alerts = [t for c, t in sent_messages if "Muitas saídas" in t]
    assert len(alerts) == 1 and "2 pessoas" in alerts[0]


async def test_sale_alert_can_be_turned_off(session, sent_messages):
    from app.services import payments

    member_id = await _member(700, status="none")
    async with SessionLocal() as s:
        await set_settings(s, TID, {"admin_ids": "4242", "alert_sale": "0"})
        payment = Payment(tenant_id=TID, member_id=member_id, plan_code="mensal", amount_cents=2990, method="pix",
                          external_ref="venda-1")
        s.add(payment)
        await s.commit()
        await payments.approve(s, payment)
    assert not any(chat == 4242 for chat, _ in sent_messages)


async def test_overview_numbers(session):
    now = utcnow()
    async with SessionLocal() as s:
        ids = []
        for tg in range(10):
            m = Member(tenant_id=TID, telegram_id=900 + tg, first_name=f"M{tg}", status="active", in_group=tg < 8)
            s.add(m)
            await s.flush()
            ids.append(m.id)
        s.add_all([
            Event(tenant_id=TID, member_id=ids[0], kind="joined", created_at=now - timedelta(minutes=5)),
            Event(tenant_id=TID, member_id=ids[1], kind="joined", created_at=now - timedelta(minutes=6)),
            Event(tenant_id=TID, member_id=ids[8], kind="left", created_at=now - timedelta(days=3)),
            Event(tenant_id=TID, member_id=ids[9], kind="left", created_at=now - timedelta(days=4)),
        ])
        await s.commit()
        data = await reports.overview(s, TID)
        feed = await reports.activity(s, TID)
    assert (data["total"], data["joined_today"], data["left_today"]) == (8, 2, 0)
    assert round(data["retention"], 1) == 75.0  # havia 8 há 30 dias (8 - 2 + 2), saíram 2
    assert data["total_change"] == 0.0
    assert feed[0]["action"] == "entrou no grupo" and feed[0]["when"] == "há 5 minutos"


def test_dashboard_shows_group_section(client):
    _login(client)
    page = client.get("/").text
    assert "Seu grupo" in page and "Membros no grupo" in page and "chartMembers" in page
    assert "Nada aconteceu por aqui ainda" in page


# ------------------------------------------------------- alertas e relatórios


def test_alerts_page_saves(client):
    _login(client)
    assert "Quando avisar" in client.get("/alertas").text
    assert "e-mail válido" in client.post("/alertas", data={"alert_email": "on", "alert_email_to": "x"}).text
    page = client.post("/alertas", data={"alert_joined": "on", "alert_email": "on", "alert_email_to": "eu@ex.com",
                                         "alert_exits_min": "9"}).text
    assert "preferências de alerta foram salvas" in page
    settings = client.portal.call(_get)
    assert settings.flag("alert_joined") and not settings.flag("alert_sale") and settings["alert_exits_min"] == "9"


async def _report_data():
    member_id = await _member(800)
    async with SessionLocal() as s:
        member = await s.get(Member, member_id)
        member.first_name = "=HYPERLINK(\"http://x\")"
        s.add(Event(tenant_id=TID, member_id=member_id, kind="joined"))
        s.add(Payment(tenant_id=TID, member_id=member_id, plan_code="mensal", amount_cents=2990, method="pix",
                      status="approved", external_ref="r1", paid_at=utcnow()))
        await s.commit()


def test_reports_page_and_exports(client):
    client.portal.call(_report_data)
    _login(client)
    page = client.get("/relatorios?dias=7").text
    assert "Últimos 7 dias" in page and "R$ 29,90" in page and "Baixar PDF" in page

    pdf = client.get("/relatorios/exportar.pdf?dias=30")
    assert pdf.headers["content-type"] == "application/pdf" and pdf.content.startswith(b"%PDF")
    assert "attachment" in pdf.headers["content-disposition"]

    xlsx = client.get("/relatorios/exportar.xlsx?dias=30")
    book = load_workbook(io.BytesIO(xlsx.content))
    assert book.sheetnames == ["Resumo", "Por dia", "Atividade", "Pagamentos"]
    assert book["Resumo"]["B3"].value == 1  # entraram
    name = book["Atividade"]["B2"].value
    assert name.startswith("'=")  # nome do Telegram nunca vira fórmula
    assert client.get("/relatorios/exportar.exe").status_code == 404


def test_reports_are_isolated(client, other_tenant):
    client.portal.call(_report_data)
    _login(client, B_LOGIN)
    assert "R$ 29,90" not in client.get("/relatorios").text
    book = load_workbook(io.BytesIO(client.get("/relatorios/exportar.xlsx").content))
    assert book["Atividade"].max_row == 1
