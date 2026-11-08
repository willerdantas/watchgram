"""Isenção de assinatura pelo superadmin e preenchimento dos admins com os administradores do grupo."""

from datetime import timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app import runtime
from app.db import SessionLocal
from app.main import app
from app.models import User
from app.services import platform
from app.services.store import get_settings, set_settings
from app.utils import admin_entry, parse_admin_ids, utcnow

A_LOGIN = {"username": "admin", "password": "senha-teste"}
B_LOGIN = {"username": "outro", "password": "senha-do-outro"}


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


def _login(client, creds):
    client.cookies.clear()
    client.post("/login", data=creds)


async def _user(user_id) -> User:
    async with SessionLocal() as s:
        return await s.get(User, user_id)


# ---------------------------------------------------------------- isenção


def test_superadmin_exempts_and_removes_exemption(client, other_tenant):
    _login(client, A_LOGIN)
    assert not client.portal.call(_user, other_tenant).subscription_active  # sem assinatura

    page = client.post(f"/admin/usuarios/{other_tenant}/isento").text
    assert "isento da assinatura" in page
    user = client.portal.call(_user, other_tenant)
    assert user.exempt and user.subscription_active and user.subscription_days_left is None

    _login(client, B_LOGIN)
    home = client.get("/").text
    assert "bot está parado" not in home
    assert "está <b>isenta</b>" in client.get("/assinatura").text
    assert client.post("/assinatura/pagar", data={"plan_code": "mensal", "method": "pix"}).status_code == 404

    _login(client, A_LOGIN)
    assert "Isenção de outro removida" in client.post(f"/admin/usuarios/{other_tenant}/isento").text
    assert not client.portal.call(_user, other_tenant).subscription_active


def test_only_superadmin_can_exempt(client, other_tenant):
    _login(client, B_LOGIN)
    assert client.post(f"/admin/usuarios/{other_tenant}/isento").status_code == 404
    _login(client, A_LOGIN)
    assert client.post("/admin/usuarios/1/isento").status_code == 400  # a própria conta


async def test_exempt_user_gets_no_reminders(session, other_tenant, sent_messages):
    async with SessionLocal() as s:
        await set_settings(s, other_tenant, {"admin_ids": "4242"})
        user = await s.get(User, other_tenant)
        user.subscription_until = utcnow() + timedelta(days=1)
        user.subscription_exempt = True
        await s.commit()
        await platform.send_reminders(s)
    assert sent_messages == []


# ------------------------------------------------------ admins do grupo


def test_admin_ids_ignore_labels():
    assert parse_admin_ids("123 @fulano\n456 (João 2, da Silva)\n789; 123") == [123, 456, 789]
    assert parse_admin_ids("111, 222") == [111, 222]  # formato antigo continua valendo
    assert admin_entry(5, "ciclano") == "5 @ciclano"
    assert admin_entry(6, None, "Ana, (3)") == "6 (Ana   3)"
    assert parse_admin_ids(admin_entry(6, None, "Ana, (3)")) == [6]


class FakeBot:
    def __init__(self, admins):
        self.admins = admins
        self.asked = None

    async def get_chat_administrators(self, chat_id):
        self.asked = chat_id
        return [SimpleNamespace(user=u) for u in self.admins]


def _tg(user_id, username=None, name="", is_bot=False):
    return SimpleNamespace(id=user_id, username=username, full_name=name, is_bot=is_bot)


def test_fill_admins_from_group(client, other_tenant):
    async def configure():
        async with SessionLocal() as s:
            await set_settings(s, other_tenant, {"group_chat_id": "-100777", "admin_ids": "999 @manual\n111"})

    async def admin_ids():
        async with SessionLocal() as s:
            settings = await get_settings(s, other_tenant)
            return settings["admin_ids"], settings.admin_ids

    client.portal.call(configure)
    bot = FakeBot([_tg(111, "dono"), _tg(222, None, "Maria Souza"), _tg(333, "meu_bot", is_bot=True)])
    runtime.bots[other_tenant] = bot
    _login(client, B_LOGIN)

    page = client.post("/config/admins-do-grupo").text
    assert "2 administrador(es) do grupo" in page
    assert bot.asked == -100777
    raw, ids = client.portal.call(admin_ids)
    assert raw == "111 @dono\n222 (Maria Souza)\n999 @manual"
    assert ids == [111, 222, 999]
    assert "222 (Maria Souza)" in client.get("/config").text


def test_fill_admins_requires_bot_and_group(client, other_tenant):
    _login(client, B_LOGIN)
    assert "Conecte o bot e configure o grupo" in client.post("/config/admins-do-grupo").text
