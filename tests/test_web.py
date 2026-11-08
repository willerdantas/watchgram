import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


def _login(client):
    r = client.post("/login", data={"username": "admin", "password": "senha-teste"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/"


def test_requires_login(client):
    # Visitante vê a página inicial; a área do usuário continua exigindo login.
    assert "Começar grátis" in client.get("/").text
    for path in ("/membros", "/relatorios", "/alertas", "/primeiros-passos"):
        r = client.get(path, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/login", path


def test_wrong_password(client):
    r = client.post("/login", data={"username": "admin", "password": "x"})
    assert "inválidos" in r.text


@pytest.mark.parametrize("path", ["/", "/membros", "/membros?status=all", "/pagamentos", "/planos", "/mensagens", "/config"])
def test_pages_render(client, path):
    _login(client)
    r = client.get(path)
    assert r.status_code == 200, r.text[:500]


def test_save_plans(client):
    _login(client)
    form = {}
    for code, price, days in [("mensal", "39,90", "30"), ("mensal_recorrente", "34,90", "30"),
                              ("trimestral", "99,90", "90"), ("semestral", "179,90", "180")]:
        form.update({f"{code}_name": code.title(), f"{code}_price": price, f"{code}_days": days,
                     f"{code}_description": "desc", f"{code}_active": "on"})
    client.post("/planos", data=form)
    page = client.get("/planos").text
    assert "39,90" in page and "179,90" in page


def test_save_config_keeps_secret_when_blank(client):
    _login(client)
    client.post("/config", data={"mp_access_token": "TEST-123", "admin_ids": "42", "grace_hours": "12",
                                 "reminder_days": "3,1", "enable_card": "on"})
    client.post("/config", data={"mp_access_token": "", "admin_ids": "42, 43", "grace_hours": "12",
                                 "reminder_days": "3,1"})
    page = client.get("/config").text
    assert "salvo — deixe em branco para manter" in page  # token continua salvo
    assert "TEST-123" not in page  # e nunca é exibido
    assert "42, 43" in page


def test_save_message(client):
    _login(client)
    page = client.get("/mensagens").text
    assert "Lembrete de vencimento" in page
    client.post("/mensagens", data={"welcome": "Oi {nome}, bem-vindo!"})
    assert "Oi {nome}, bem-vindo!" in client.get("/mensagens").text


def test_save_start_keywords(client):
    _login(client)
    assert "Palavras de entrada" in client.get("/mensagens").text
    client.post("/mensagens/palavras-de-entrada", data={"start_keywords": "Oi\nbom dia, salve\noi"})
    page = client.get("/mensagens").text
    assert "Oi\nbom dia\nsalve</textarea>" in page
    assert 'name="reply_unknown" value="1" >' in page  # desmarcado


def test_manual_member_and_stats(client):
    _login(client)
    client.post("/membros/adicionar", data={"telegram_id": "777", "days": "30", "plan_code": "mensal"})
    assert "777" in client.get("/membros").text
    assert client.get("/").status_code == 200


def test_webhook_ignores_empty(client):
    key = client.portal.call(_webhook_key)
    assert client.post(f"/webhooks/mercadopago/{key}", json={}).json() == {"ok": True}
    assert client.post("/webhooks/mercadopago/chave-inexistente", json={}).status_code == 404


async def _webhook_key():
    from app.db import SessionLocal
    from app.models import User

    async with SessionLocal() as s:
        return (await s.get(User, 1)).webhook_key
