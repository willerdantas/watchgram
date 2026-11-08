"""Criar/excluir planos, mensagens personalizadas (painel e bot) e a ajuda (i) de cada página."""

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.bot import handlers_member
from app.db import SessionLocal
from app.main import app
from app.models import CustomMessage, Member, Plan
from app.services.seed import seed_tenant
from app.services.store import get_plans, set_settings
from app.utils import slugify, utcnow
from tests.tenant import TID

A_LOGIN = {"username": "admin", "password": "senha-teste"}
B_LOGIN = {"username": "outro", "password": "senha-do-outro"}


@pytest.fixture
def client():
    with TestClient(app) as c:
        c.post("/login", data=A_LOGIN)
        yield c


async def _plans(tenant_id=TID):
    async with SessionLocal() as s:
        return {p.code: p for p in await get_plans(s, tenant_id)}


async def _customs(tenant_id=TID):
    async with SessionLocal() as s:
        return (await s.scalars(select(CustomMessage).where(CustomMessage.tenant_id == tenant_id))).all()


async def _set_admins():
    async with SessionLocal() as s:
        await set_settings(s, TID, {"admin_ids": "4242 @dono"})


# ------------------------------------------------------------------ planos


def test_slugify():
    assert slugify("Plano Anual VIP 2000") == "plano_anual_vip_2000"
    assert slugify("Ação & Promoção!") == "acao_promocao"


def test_create_plan_and_unique_code(client):
    data = {"new_name": "Anual VIP", "new_price": "199,90", "new_days": "365", "new_active": "on",
            "new_description": "1 ano"}
    assert "Plano &#34;Anual VIP&#34; criado" in client.post("/planos/novo", data=data).text
    client.post("/planos/novo", data=data)
    plans = client.portal.call(_plans)
    assert {"anual_vip", "anual_vip_2"} <= set(plans)
    plan = plans["anual_vip"]
    assert (plan.price_cents, plan.days, plan.active, plan.recurring) == (19990, 365, True, False)
    assert plan.sort_order > max(p.sort_order for c, p in plans.items() if not c.startswith("anual"))


def test_create_recurring_plan_forces_30_days(client):
    client.post("/planos/novo", data={"new_name": "Mensal Premium", "new_price": "49,90", "new_days": "90",
                                      "new_recurring": "on", "new_active": "on"})
    plan = client.portal.call(_plans)["mensal_premium"]
    assert plan.recurring and plan.days == 30


def test_create_plan_validation(client):
    before = set(client.portal.call(_plans))
    assert "valor inválido" in client.post("/planos/novo", data={"new_name": "X", "new_price": "0,50", "new_days": "30"}).text
    assert "duração inválida" in client.post("/planos/novo", data={"new_name": "X", "new_price": "10", "new_days": "0"}).text
    assert "informe o nome" in client.post("/planos/novo", data={"new_name": " ", "new_price": "10", "new_days": "5"}).text
    assert set(client.portal.call(_plans)) == before


def test_delete_unused_plan_but_not_used_one(client):
    async def use_plan():
        async with SessionLocal() as s:
            s.add(Member(tenant_id=TID, telegram_id=55, status="active", plan_code="mensal", expires_at=utcnow()))
            await s.commit()

    client.portal.call(use_plan)
    assert "não pode ser excluído" in client.post("/planos/mensal/excluir").text
    assert "excluído" in client.post("/planos/semestral/excluir").text
    plans = client.portal.call(_plans)
    assert "mensal" in plans and "semestral" not in plans
    assert client.post("/planos/nao-existe/excluir").status_code == 404


async def test_deleted_default_plan_does_not_come_back(session):
    async with SessionLocal() as s:
        await s.delete(await s.get(Plan, (TID, "semestral")))
        await s.commit()
        await seed_tenant(s, TID)  # roda a cada inicialização
    assert "semestral" not in await _plans()


def test_save_plan_order(client):
    plans = client.portal.call(_plans)
    form = {}
    for code, p in plans.items():
        form |= {f"{code}_name": p.name, f"{code}_price": f"{p.price_cents / 100:.2f}".replace(".", ","),
                 f"{code}_days": str(p.days), f"{code}_order": str(p.sort_order), f"{code}_active": "on"}
    form["semestral_order"] = "-1"
    assert "Planos salvos" in client.post("/planos", data=form).text
    assert next(iter(client.portal.call(_plans))) == "semestral"


def test_cannot_delete_other_tenant_plan(client, other_tenant):
    client.cookies.clear()
    client.post("/login", data=B_LOGIN)
    client.post("/planos/semestral/excluir")  # exclui o do próprio usuário B
    assert "semestral" in client.portal.call(_plans, TID)
    assert "semestral" not in client.portal.call(_plans, other_tenant)


# ----------------------------------------------------- mensagens personalizadas


def test_create_edit_delete_custom_message(client):
    data = {"title": "Chave Pix", "triggers": "pix\nchave pix", "text": "{saudacao}, {nome}! Chave: abc",
            "show_plans": "on", "active": "on"}
    assert "criada" in client.post("/mensagens/personalizadas", data=data).text
    row = client.portal.call(_customs)[0]
    assert (row.triggers, row.show_plans, row.active) == ("pix\nchave pix", True, True)

    page = client.post(f"/mensagens/personalizadas/{row.id}", data=data | {"text": "Nova chave", "active": ""}).text
    assert "salva" in page and "Nova chave" in page
    row = client.portal.call(_customs)[0]
    assert row.text == "Nova chave" and not row.active

    assert "excluída" in client.post(f"/mensagens/personalizadas/{row.id}/excluir").text
    assert client.portal.call(_customs) == []


def test_custom_message_validation(client):
    assert "palavra-gatilho" in client.post("/mensagens/personalizadas", data={"title": "A", "triggers": " ", "text": "x"}).text
    assert "texto é obrigatório" in client.post("/mensagens/personalizadas", data={"title": "A", "triggers": "a", "text": ""}).text
    assert client.portal.call(_customs) == []


def test_custom_message_is_isolated(client, other_tenant):
    client.post("/mensagens/personalizadas", data={"title": "Do admin", "triggers": "x", "text": "segredo", "active": "on"})
    row_id = client.portal.call(_customs)[0].id
    client.cookies.clear()
    client.post("/login", data=B_LOGIN)
    assert "segredo" not in client.get("/mensagens").text
    for path in ("", "/excluir", "/testar"):
        assert client.post(f"/mensagens/personalizadas/{row_id}{path}",
                           data={"title": "B", "triggers": "x", "text": "y"}).status_code == 404
    assert client.portal.call(_customs)[0].text == "segredo"


def test_custom_message_test_sends_to_admins(client, sent_messages):
    client.portal.call(_set_admins)
    client.post("/mensagens/personalizadas", data={"title": "T", "triggers": "x", "text": "Olá {nome}", "active": "on"})
    row_id = client.portal.call(_customs)[0].id
    assert "enviada aos admins" in client.post(f"/mensagens/personalizadas/{row_id}/testar").text
    assert sent_messages == [(4242, "🧪 Teste\n\nOlá Fulano")]


class FakeMessage:
    def __init__(self, text, first_name="Ana <b>"):
        self.text = text
        self.from_user = SimpleNamespace(id=777, username=None, first_name=first_name, last_name=None,
                                         full_name=first_name, is_bot=False)
        self.chat = SimpleNamespace(id=777)
        self.bot = None
        self.answers = []

    async def answer(self, text, reply_markup=None):
        self.answers.append((text, reply_markup))


async def test_bot_answers_custom_trigger_before_greetings(session):
    async with SessionLocal() as s:
        s.add(CustomMessage(tenant_id=TID, title="Pix", triggers="pix", text="Oi {nome}, chave: abc", show_plans=True))
        s.add(CustomMessage(tenant_id=TID, title="Off", triggers="suporte", text="não deve sair", active=False))
        await s.commit()

    msg = FakeMessage("Pix por favor")
    await handlers_member.msg_free_text(msg, tenant_id=TID)
    text, markup = msg.answers[0]
    assert text == "Oi Ana &lt;b&gt;, chave: abc"  # nome escapado no HTML do Telegram
    assert markup.inline_keyboard[0][0].callback_data == "plans"

    msg = FakeMessage("suporte")  # inativa: cai na resposta padrão
    await handlers_member.msg_free_text(msg, tenant_id=TID)
    assert all("não deve sair" not in t for t, _ in msg.answers)


async def test_other_tenant_custom_message_not_used(session, other_tenant):
    async with SessionLocal() as s:
        s.add(CustomMessage(tenant_id=other_tenant, title="B", triggers="pix", text="do outro"))
        await s.commit()
    msg = FakeMessage("pix")
    await handlers_member.msg_free_text(msg, tenant_id=TID)
    assert all("do outro" not in t for t, _ in msg.answers)


# -------------------------------------------------------------------- ajuda


HELP_PAGES = {
    "/": "Como funciona: Estatísticas",
    "/membros": "Como funciona: Membros",
    "/pagamentos": "Como funciona: Pagamentos",
    "/planos": "Como funciona: Planos",
    "/mensagens": "Como funciona: Mensagens",
    "/config": "Como funciona: Configurações",
    "/conta": "Como funciona: Minha conta",
    "/admin/usuarios": "Como funciona: Usuários",
    "/admin/plataforma": "Como funciona: Plataforma",
}


def test_each_page_has_only_its_own_help(client):
    for path, title in HELP_PAGES.items():
        page = client.get(path).text
        assert page.count('class="help-btn"') == 1, path
        assert page.count('<dialog id="help"') == 1, path
        assert title in page, path
        assert not any(t in page for t in HELP_PAGES.values() if t != title), path


def test_subscription_page_help(client, other_tenant):
    client.cookies.clear()
    client.post("/login", data=B_LOGIN)
    page = client.get("/assinatura").text
    assert "Como funciona: Assinatura" in page and 'class="help-btn"' in page


def test_login_page_has_no_help(client):
    client.cookies.clear()
    page = client.get("/login").text
    assert "help-btn" not in page and "<dialog" not in page
