"""Layout do painel: menu lateral, primeiros passos, estados vazios e tela de login."""

import pytest
from fastapi.testclient import TestClient

from app import runtime
from app.db import SessionLocal
from app.main import app
from app.services.store import set_settings
from tests.tenant import TID

A_LOGIN = {"username": "admin", "password": "senha-teste"}
B_LOGIN = {"username": "outro", "password": "senha-do-outro"}
READY = {"group_chat_id": "-100123", "admin_ids": "4242 @dono", "mp_access_token": "APP_USR-x", "enable_pix_auto": "1"}


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


def _login(client, creds=A_LOGIN):
    client.cookies.clear()
    client.post("/login", data=creds)


async def _settings(values):
    async with SessionLocal() as s:
        await set_settings(s, TID, values)


def test_login_page_has_no_menu_and_shows_errors(client):
    page = client.get("/login").text
    assert "Watchgram" in page and 'class="sidebar"' not in page
    page = client.post("/login", data={"username": "admin", "password": "errada"}).text
    assert 'class="flash' in page


def test_menu_marks_current_page(client):
    _login(client)
    page = client.get("/membros").text
    assert 'class="sidebar"' in page
    assert '<a href="/membros" class="on" aria-current="page">' in page
    assert '<a href="/planos" class="on"' not in page


def test_menu_shows_each_kind_of_user_its_own_links(client, other_tenant):
    _login(client)
    page = client.get("/").text
    assert 'href="/admin/usuarios"' in page and 'href="/assinatura"' not in page
    _login(client, B_LOGIN)
    page = client.get("/").text
    assert 'href="/assinatura"' in page and 'href="/admin/usuarios"' not in page


def test_first_steps_follow_the_real_configuration(client):
    _login(client)
    page = client.get("/").text
    assert "card setup" in page and "0 de 4 concluídos" in page and "Conectar o bot" in page

    runtime.usernames[TID] = "meu_bot"
    client.portal.call(_settings, {"group_chat_id": "-100123"})
    page = client.get("/").text
    assert "2 de 4 concluídos" in page and "Configurar pagamentos" in page

    client.portal.call(_settings, READY)
    assert "card setup" not in client.get("/").text


def test_empty_states(client, other_tenant):
    _login(client, B_LOGIN)
    assert "Quando alguém conversar com o seu bot" in client.get("/membros?status=all").text
    assert "Tente buscar por outro nome" in client.get("/membros?status=all&q=ninguem").text
    assert "Nenhum pagamento por aqui" in client.get("/pagamentos").text
    page = client.get("/ajuda").text
    assert "Nenhum pedido em andamento" in page and "Abrir um pedido" in page


def test_custom_accent_color_is_applied(client):
    _login(client)
    client.post("/conta", data={"name": "", "accent_color": "#123456"})
    assert "--accent: #123456" in client.get("/").text
