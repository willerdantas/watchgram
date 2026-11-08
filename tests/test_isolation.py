"""Isolamento entre usuários (tenants): nenhum usuário vê ou altera a área de outro."""

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.bot import handlers_admin, handlers_member
from app.db import SessionLocal
from app.main import app
from app.models import Member, Payment, Plan, Setting, User
from app.services import membership, payments, stats
from app.services.store import get_settings, set_settings
from app.utils import utcnow
from tests.tenant import TID

A_LOGIN = {"username": "admin", "password": "senha-teste"}
B_LOGIN = {"username": "outro", "password": "senha-do-outro"}


async def _data_for(tenant_id: int, telegram_id: int, name: str) -> tuple[int, int]:
    """Cria um membro ativo e um pagamento pendente com comprovante na área do usuário."""
    async with SessionLocal() as s:
        member = Member(tenant_id=tenant_id, telegram_id=telegram_id, first_name=name, status="active",
                        expires_at=utcnow(), plan_code="mensal")
        s.add(member)
        await s.flush()
        payment = Payment(tenant_id=tenant_id, member_id=member.id, plan_code="mensal", amount_cents=2990,
                          method="pix_manual", external_ref=f"ref-{tenant_id}-{telegram_id}", proof_file_id="doc:x")
        s.add(payment)
        await s.commit()
        return member.id, payment.id


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


def _login(client, creds):
    client.cookies.clear()
    r = client.post("/login", data=creds, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/", r.text


# ------------------------------------------------------------------ painel


def test_lists_show_only_own_data(client, other_tenant):
    client.portal.call(_data_for, TID, 1001, "Alice-do-A")
    client.portal.call(_data_for, other_tenant, 2002, "Bruno-do-B")

    _login(client, B_LOGIN)
    for path in ("/membros?status=all", "/pagamentos", "/"):
        page = client.get(path).text
        assert "Alice-do-A" not in page and "1001" not in page, path
    assert "Bruno-do-B" in client.get("/membros?status=all").text

    _login(client, A_LOGIN)
    assert "Bruno-do-B" not in client.get("/membros?status=all").text
    assert "Bruno-do-B" not in client.get("/pagamentos").text


def test_actions_on_other_tenant_records_return_404(client, other_tenant):
    member_a, payment_a = client.portal.call(_data_for, TID, 1001, "Alice-do-A")
    _login(client, B_LOGIN)
    attempts = [
        ("post", f"/membros/{member_a}/estender", {"days": "30"}),
        ("post", f"/membros/{member_a}/remover", None),
        ("post", f"/membros/{member_a}/convite", None),
        ("post", f"/membros/{member_a}/cancelar-recorrencia", None),
        ("post", f"/pagamentos/{payment_a}/aprovar", None),
        ("post", f"/pagamentos/{payment_a}/recusar", None),
        ("post", f"/pagamentos/{payment_a}/verificar", None),
        ("get", f"/pagamentos/{payment_a}/comprovante", None),
    ]
    for method, path, data in attempts:
        kwargs = {"follow_redirects": False, **({"data": data} if data else {})}
        r = getattr(client, method)(path, **kwargs)
        assert r.status_code == 404, (path, r.status_code)

    async def unchanged():
        async with SessionLocal() as s:
            member, payment = await s.get(Member, member_a), await s.get(Payment, payment_a)
            return member.status, payment.status

    assert client.portal.call(unchanged) == ("active", "pending")


def test_settings_plans_and_messages_are_per_tenant(client, other_tenant):
    _login(client, B_LOGIN)
    client.post("/config", data={"admin_ids": "777", "pix_key": "chave-do-B", "grace_hours": "5", "reminder_days": "3"})
    client.post("/planos", data={"mensal_price": "99,00", "mensal_days": "30", "mensal_name": "Plano do B",
                                 "mensal_active": "on"})
    client.post("/mensagens", data={"welcome": "Boas-vindas do B"})

    _login(client, A_LOGIN)
    assert "chave-do-B" not in client.get("/config").text
    assert "Plano do B" not in client.get("/planos").text
    assert "Boas-vindas do B" not in client.get("/mensagens").text

    async def values():
        async with SessionLocal() as s:
            a, b = await get_settings(s, TID), await get_settings(s, other_tenant)
            plan_a, plan_b = await s.get(Plan, (TID, "mensal")), await s.get(Plan, (other_tenant, "mensal"))
            return a.get("pix_key"), b.get("pix_key"), plan_a.price_cents, plan_b.price_cents

    assert client.portal.call(values) == ("", "chave-do-B", 2990, 9900)


def test_same_bot_token_cannot_be_used_by_two_tenants(client, other_tenant):
    async def set_token():
        async with SessionLocal() as s:
            await set_settings(s, TID, {"bot_token": "123:ABC"})

    client.portal.call(set_token)
    _login(client, B_LOGIN)
    page = client.post("/config", data={"bot_token": "123:ABC", "grace_hours": "24"}).text
    assert "já está cadastrado em outra conta" in page

    async def token_b():
        async with SessionLocal() as s:
            return (await get_settings(s, other_tenant)).get("bot_token")

    assert client.portal.call(token_b) == ""


def test_webhook_key_is_per_tenant(client, other_tenant):
    async def keys():
        async with SessionLocal() as s:
            return [u.webhook_key for u in (await s.scalars(select(User).order_by(User.id))).all()]

    key_a, key_b = client.portal.call(keys)
    assert key_a != key_b and len(key_a) >= 24
    _login(client, A_LOGIN)
    assert key_b not in client.get("/config").text


# ------------------------------------------------------- gestão de usuários


def test_only_superadmin_manages_users(client, other_tenant):
    _login(client, B_LOGIN)
    assert client.get("/admin/usuarios").status_code == 404
    assert client.post("/admin/usuarios", data={"username": "invasor"}).status_code == 404
    assert client.post(f"/admin/usuarios/{TID}/senha").status_code == 404
    assert "/admin/usuarios" not in client.get("/").text  # nem aparece no menu


def test_superadmin_creates_user_with_own_area(client):
    _login(client, A_LOGIN)
    page = client.post("/admin/usuarios", data={"username": "Maria.Silva", "name": "VIP da Maria",
                                                "password": "senha-da-maria"}).text
    assert "Usuário maria.silva criado" in page

    _login(client, {"username": "maria.silva", "password": "senha-da-maria"})
    assert "VIP da Maria" in client.get("/").text
    assert "R$ 29,90" in client.get("/planos").text  # planos padrão próprios
    assert client.get("/admin/usuarios").status_code == 404


def test_password_reset_invalidates_old_sessions(client, other_tenant):
    _login(client, B_LOGIN)
    b_cookies = dict(client.cookies)

    _login(client, A_LOGIN)
    page = client.post(f"/admin/usuarios/{other_tenant}/senha").text
    new_password = page.split("Nova senha de outro: ")[1].split(" ")[0]

    client.cookies.clear()
    client.cookies.update(b_cookies)
    assert client.get("/", follow_redirects=False).headers["location"] == "/login"  # sessão antiga caiu
    _login(client, {"username": "outro", "password": new_password})


def test_deactivated_user_cannot_use_panel(client, other_tenant):
    _login(client, B_LOGIN)
    b_cookies = dict(client.cookies)
    _login(client, A_LOGIN)
    client.post(f"/admin/usuarios/{other_tenant}/ativo")

    client.cookies.clear()
    client.cookies.update(b_cookies)
    assert client.get("/", follow_redirects=False).headers["location"] == "/login"
    r = client.post("/login", data=B_LOGIN, follow_redirects=False)
    assert r.headers["location"] == "/login"


def test_superadmin_cannot_deactivate_self(client):
    _login(client, A_LOGIN)
    assert client.post(f"/admin/usuarios/{TID}/ativo").status_code == 400


# --------------------------------------------------------------- bot e serviços


async def test_same_telegram_user_is_independent_per_tenant(session, other_tenant):
    await _data_for(TID, 5555, "Mesma Pessoa")
    async with SessionLocal() as s:
        assert (await membership.get_member_by_tg(s, TID, 5555)).status == "active"
        assert await membership.get_member_by_tg(s, other_tenant, 5555) is None


async def test_bot_status_only_sees_own_tenant(session, other_tenant):
    await _data_for(TID, 5555, "Assinante do A")
    answers = []

    async def answer(text, reply_markup=None):
        answers.append(text)

    user = SimpleNamespace(id=5555, username=None, first_name="Assinante")
    await handlers_member.cmd_status(SimpleNamespace(from_user=user, answer=answer), tenant_id=other_tenant)
    assert "não tem uma assinatura ativa" in answers[0]  # no bot do B ele não é assinante


async def test_bot_callbacks_reject_other_tenant_payment(session, other_tenant):
    _, payment_a = await _data_for(TID, 5555, "Assinante do A")
    alerts = []

    async def cb_answer(text=None, show_alert=False):
        alerts.append(text)

    # Mesmo usuário do Telegram, mas falando com o bot do B, tenta verificar o pagamento do A.
    callback = SimpleNamespace(data=f"check:{payment_a}", from_user=SimpleNamespace(id=5555), answer=cb_answer)
    await handlers_member.cb_check(callback, tenant_id=other_tenant)
    assert alerts == ["Pagamento não encontrado."]

    # Admin do B tenta aprovar o comprovante do A pelo botão.
    async with SessionLocal() as s:
        await set_settings(s, other_tenant, {"admin_ids": "42"})
    callback = SimpleNamespace(
        data=f"manual:approve:{payment_a}", from_user=SimpleNamespace(id=42, first_name="Admin B"), answer=cb_answer
    )
    await handlers_admin.cb_manual(callback, tenant_id=other_tenant)
    assert alerts[-1] == "Este pagamento já foi processado."
    async with SessionLocal() as s:
        assert (await s.get(Payment, payment_a)).status == "pending"


async def test_webhook_processing_ignores_other_tenant_payment(session, other_tenant, monkeypatch):
    _, payment_a = await _data_for(TID, 5555, "Assinante do A")

    class FakeMP:
        async def get_payment(self, _id):
            return {"id": 1, "status": "approved", "external_reference": f"ref-{TID}-5555"}

    monkeypatch.setattr(payments, "mp_client", lambda settings: FakeMP())
    async with SessionLocal() as s:
        await payments.handle_webhook(s, other_tenant, "payment", "1")  # chegou pela URL do B
    async with SessionLocal() as s:
        assert (await s.get(Payment, payment_a)).status == "pending"


async def test_stats_are_per_tenant(session, other_tenant):
    await _data_for(TID, 1, "A1")
    await _data_for(TID, 2, "A2")
    await _data_for(other_tenant, 3, "B1")
    async with SessionLocal() as s:
        assert (await stats.compute(s, TID))["active"] == 2
        assert (await stats.compute(s, other_tenant))["active"] == 1


async def test_new_tenant_gets_default_rows(session, other_tenant):
    async with SessionLocal() as s:
        settings_b = (await s.scalars(select(Setting).where(Setting.tenant_id == other_tenant))).all()
        plans_b = (await s.scalars(select(Plan).where(Plan.tenant_id == other_tenant))).all()
    assert len(settings_b) > 10 and len(plans_b) == 4
