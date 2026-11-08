"""Checklist de segurança: segredos, sessão, login, origem, cabeçalhos, HTTPS, escape e isolamento no banco."""

import re

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from sqlalchemy import select, text

from app import config, runtime
from app.db import IS_SQLITE, SessionLocal, bind_tenant
from app.main import app
from app.models import Member, PlatformSetting, Setting
from app.security import PREFIX, decrypt, encrypt, fingerprint
from app.services import platform
from app.services.seed import encrypt_stored_secrets
from app.services.store import get_settings, set_settings
from app.web import auth
from tests.tenant import TID

A_LOGIN = {"username": "admin", "password": "senha-teste"}
TOKEN = "123456789:" + "B" * 35
PUBLIC_GET = {"/", "/login", "/precos", "/cadastro", "/obrigado", "/health"}


@pytest.fixture(autouse=True)
def _clear_limits():
    auth._failures.clear()
    yield
    auth._failures.clear()


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


def _login(client, creds=A_LOGIN):
    client.cookies.clear()
    return client.post("/login", data=creds)


# ------------------------------------------------ 1, 2, 5: segredos


async def test_secrets_are_encrypted_at_rest(session):
    await set_settings(session, TID, {"bot_token": TOKEN, "mp_access_token": "APP_USR-segredo"})
    raw = {r.key: r.value for r in (await session.scalars(select(Setting).where(Setting.tenant_id == TID))).all()}
    assert raw["bot_token"].startswith(PREFIX) and TOKEN not in raw["bot_token"]
    assert raw["mp_access_token"].startswith(PREFIX)
    assert raw["bot_token_fp"] == fingerprint(TOKEN)
    settings = await get_settings(session, TID)
    assert settings["bot_token"] == TOKEN and settings["mp_access_token"] == "APP_USR-segredo"

    await platform.set_platform_settings(session, {"smtp_password": "senha-smtp", "trial_days": "7"})
    stored = await session.get(PlatformSetting, "smtp_password")
    await session.refresh(stored)
    assert stored.value.startswith(PREFIX)
    assert (await platform.get_platform_settings(session))["smtp_password"] == "senha-smtp"


async def test_plaintext_secrets_from_old_versions_are_migrated(session):
    (await session.get(Setting, (TID, "bot_token"))).value = TOKEN  # como uma versão antiga gravava
    (await session.get(PlatformSetting, "mp_webhook_secret")).value = "antigo"
    await session.commit()
    await encrypt_stored_secrets(session)
    row = await session.get(Setting, (TID, "bot_token"))
    await session.refresh(row)
    assert row.value.startswith(PREFIX) and decrypt(row.value) == TOKEN
    assert (await session.get(Setting, (TID, "bot_token_fp"))).value == fingerprint(TOKEN)
    secret = await session.get(PlatformSetting, "mp_webhook_secret")
    await session.refresh(secret)
    assert secret.value.startswith(PREFIX) and decrypt(secret.value) == "antigo"


def test_unreadable_secret_does_not_crash():
    assert decrypt(PREFIX + "lixo") == "" and decrypt("") == "" and encrypt("") == ""


def test_same_bot_in_two_accounts_is_still_blocked(client, other_tenant):
    async def give_token():
        async with SessionLocal() as s:
            await set_settings(s, other_tenant, {"bot_token": TOKEN})

    client.portal.call(give_token)
    _login(client)
    assert "já está cadastrado em outra conta" in client.post("/config", data={"bot_token": TOKEN, "grace_hours": "24"}).text


def test_secret_never_rendered(client):
    _login(client)
    client.post("/config", data={"bot_token": TOKEN, "grace_hours": "24"})
    assert TOKEN not in client.get("/config").text


# ------------------------------------------------ 6, 7: autenticação no servidor


def _all_routes(routes):
    for route in routes:
        router = getattr(route, "original_router", None)  # rotas de include_router
        if router is not None:
            yield from _all_routes(router.routes)
        else:
            yield route


def test_every_private_route_requires_login(client):
    checked = 0
    for route in _all_routes(app.routes):
        if not isinstance(route, APIRoute) or "GET" not in route.methods or "{" in route.path:
            continue
        if route.path in PUBLIC_GET:
            continue
        r = client.get(route.path, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/login", route.path
        checked += 1
    assert checked >= 13


def test_private_posts_require_login(client):
    for path in ("/config", "/planos/novo", "/alertas", "/membros/adicionar", "/conta/senha", "/admin/usuarios"):
        r = client.post(path, data={}, follow_redirects=False)
        assert r.status_code in (303, 422), path
        if r.status_code == 303:
            assert r.headers["location"] == "/login", path


@pytest.mark.skipif(IS_SQLITE, reason="segurança em nível de linha é do PostgreSQL")
async def test_row_level_security_hides_other_tenants(session, other_tenant):
    async with SessionLocal() as s:
        s.add(Member(tenant_id=other_tenant, telegram_id=1, first_name="Outro"))
        await s.commit()
    async with SessionLocal() as s:
        await bind_tenant(s, TID)
        assert (await s.scalars(select(Member))).all() == []  # mesmo sem filtro no código
        s.add(Member(tenant_id=other_tenant, telegram_id=2))
        with pytest.raises(Exception, match="row-level security"):
            await s.commit()
    async with SessionLocal() as s:  # sem área ligada (bot, agendador) segue vendo tudo
        assert len((await s.scalars(select(Member))).all()) == 1
        assert await s.scalar(text("select relforcerowsecurity from pg_class where relname = 'members'"))


# ------------------------------------------------ 8: campos adulterados / origem


def test_cross_site_post_is_blocked(client):
    _login(client)
    r = client.post("/alertas", data={"alert_joined": "on"}, headers={"origin": "https://site-malicioso.com"})
    assert r.status_code == 403
    r = client.post("/alertas", data={"alert_joined": "on"}, headers={"origin": "http://testserver"})
    assert r.status_code == 200
    r = client.post("/alertas", data={}, headers={"referer": "https://site-malicioso.com/x"})
    assert r.status_code == 403


def test_webhooks_accept_other_origins(client):
    r = client.post("/webhooks/paypal/inexistente", json={}, headers={"origin": "https://paypal.com"})
    assert r.status_code == 404  # chegou à rota (não foi barrado como 403)


def test_unknown_fields_are_ignored(client):
    _login(client)
    client.post("/config", data={"grace_hours": "24", "detected_chats": "[{\"id\": 1}]", "bot_token_fp": "x"})

    async def read():
        async with SessionLocal() as s:
            return await get_settings(s, TID)

    settings = client.portal.call(read)
    assert settings.detected_chats == [] and settings.get("bot_token_fp", "") == ""


def test_oversized_request_is_rejected(client):
    _login(client)
    r = client.post("/mensagens", content=b"x" * (1024 * 1024 + 1),
                    headers={"content-type": "application/x-www-form-urlencoded"})
    assert r.status_code == 413


def test_oversized_field_is_not_saved(client):
    _login(client)
    page = client.post("/config", data={"grace_hours": "24", "support_contact": "x" * 5000}).text
    assert "longo demais" in page


# ------------------------------------------------ 9, 10, 11: sessão, senha, login


def test_session_cookie_flags(client):
    r = client.post("/login", data=A_LOGIN, follow_redirects=False)
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=lax" in cookie and "max-age=43200" in cookie
    assert cookie.startswith("sessao=")  # sem domínio https; com ele vira __Host-sessao e Secure


def test_passwords_are_hashed_with_scrypt(client):
    async def stored():
        from app.models import User

        async with SessionLocal() as s:
            return (await s.get(User, TID)).password_hash

    value = client.portal.call(stored)
    assert value.startswith("scrypt$") and "senha-teste" not in value


def test_login_is_limited_per_ip_and_per_account(client):
    for _ in range(5):
        client.post("/login", data={"username": "admin", "password": "errada"})
    assert "Muitas tentativas" in client.post("/login", data=A_LOGIN).text

    auth._failures.clear()
    for i in range(10):  # tentativas espalhadas por vários IPs contra a mesma conta
        auth.register_failure(f"10.0.0.{i}")
        auth.register_failure("conta:admin")
    assert auth.is_locked("10.9.9.9", "admin") and not auth.is_locked("10.9.9.9", "outra")


def test_password_change_is_limited(client):
    _login(client)
    for _ in range(5):
        client.post("/conta/senha", data={"current_password": "errada", "new_password": "nova-senha-123"})
    page = client.post("/conta/senha", data={"current_password": "senha-teste", "new_password": "nova-senha-123"}).text
    assert "Muitas tentativas" in page


# ------------------------------------------------ 15: escape do conteúdo do usuário


def test_user_content_is_escaped_and_never_inside_scripts(client):
    _login(client)
    evil = "x');alert(1);('<script>alert(2)</script>"
    client.post("/planos/novo", data={"new_name": evil, "new_price": "10", "new_days": "30"})
    page = client.get("/planos").text
    assert "<script>alert(2)" not in page and "&lt;script&gt;alert(2)" in page
    assert "confirm(" not in page
    assert not re.search(r"\son[a-z]+=", page)  # nenhum onclick/onsubmit: JS só em arquivo e script com nonce


# ------------------------------------------------ 16: upload


def test_proof_size_is_limited(client, monkeypatch):
    class File:
        file_size = 50 * 1024 * 1024
        file_path = "documents/grande.pdf"

    class Bot:
        async def get_file(self, file_id):
            return File()

    async def payment():
        from datetime import timedelta

        from app.models import Payment
        from app.utils import utcnow

        async with SessionLocal() as s:
            m = Member(tenant_id=TID, telegram_id=77, status="active", expires_at=utcnow() + timedelta(days=1))
            s.add(m)
            await s.flush()
            p = Payment(tenant_id=TID, member_id=m.id, plan_code="mensal", amount_cents=100, method="pix_manual",
                        external_ref="up-1", proof_file_id="doc:x")
            s.add(p)
            await s.commit()
            return p.id

    monkeypatch.setitem(runtime.bots, TID, Bot())
    pid = client.portal.call(payment)
    _login(client)
    assert client.get(f"/pagamentos/{pid}/comprovante").status_code == 413


# ------------------------------------------------ 17, 18, 19: respostas, cabeçalhos, HTTPS


def test_lean_responses(client):
    assert client.get("/health").json() == {"ok": True}
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(path).status_code == 404


def test_security_headers_and_nonce(client):
    _login(client)
    r = client.get("/")
    csp = r.headers["content-security-policy"]
    nonce = re.search(r"'nonce-([^']+)'", csp).group(1)
    assert f'<script nonce="{nonce}">' in r.text
    script_src = re.search(r"script-src ([^;]+)", csp).group(1)
    assert "unsafe-inline" not in script_src and "unsafe-eval" not in script_src
    assert "frame-ancestors 'none'" in csp and "object-src 'none'" in csp
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["x-frame-options"] == "DENY"
    assert r.headers["referrer-policy"] == "strict-origin-when-cross-origin"
    assert r.headers["cache-control"] == "no-store"
    assert "strict-transport-security" not in r.headers  # só em https
    assert client.get("/").headers["content-security-policy"] != csp  # nonce novo a cada página


def test_https_is_forced_with_public_domain(monkeypatch):
    monkeypatch.setattr(config, "FORCE_HTTPS", True)
    with TestClient(app, base_url="http://bot.exemplo.com.br") as c:
        r = c.get("/precos", follow_redirects=False)
        assert r.status_code == 308 and r.headers["location"] == "https://bot.exemplo.com.br/precos"
    with TestClient(app, base_url="https://bot.exemplo.com.br") as c:
        r = c.get("/precos")
        assert r.status_code == 200 and "max-age=31536000" in r.headers["strict-transport-security"]
