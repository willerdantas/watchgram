"""Pedidos de ajuda (conversa, aprovação, encerramento em 48 h) e ganhos da plataforma nas estatísticas."""

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from app import scheduler
from app.db import SessionLocal
from app.main import app
from app.models import PlatformPayment, SupportTicket, User
from app.services import stats
from app.services.passwords import hash_password
from app.services.store import set_settings
from app.utils import utcnow
from tests.tenant import TID

A_LOGIN = {"username": "admin", "password": "senha-teste"}
B_LOGIN = {"username": "outro", "password": "senha-do-outro"}


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


def _login(client, creds):
    client.cookies.clear()
    client.post("/login", data=creds)


async def _ticket(ticket_id) -> SupportTicket:
    async with SessionLocal() as s:
        ticket = await s.get(SupportTicket, ticket_id)
        await s.refresh(ticket, ["messages"])
        return ticket


async def _admins(tenant_id, ids):
    async with SessionLocal() as s:
        await set_settings(s, tenant_id, {"admin_ids": ids})


async def _answered_ago(ticket_id, hours):
    async with SessionLocal() as s:
        ticket = await s.get(SupportTicket, ticket_id)
        ticket.answered_at = utcnow() - timedelta(hours=hours)
        await s.commit()


def _open_ticket(client, subject="Bot não envia convite", text="Desde ontem o convite não chega.") -> int:
    r = client.post("/ajuda", data={"subject": subject, "text": text}, follow_redirects=False)
    assert r.status_code == 303
    return int(r.headers["location"].rsplit("/", 1)[1])


# ------------------------------------------------------------------- ajuda


def test_full_flow_reply_approve(client, other_tenant, sent_messages):
    client.portal.call(_admins, TID, "1001 @suporte")  # Telegram do superadmin
    client.portal.call(_admins, other_tenant, "2002 @cliente")  # Telegram do usuário
    _login(client, B_LOGIN)
    tid = _open_ticket(client)
    page = client.get(f"/ajuda/{tid}").text
    assert "Desde ontem o convite não chega." in page and "Aguardando o suporte" in page
    assert any(chat == 1001 and "novo pedido" in text for chat, text in sent_messages)

    _login(client, A_LOGIN)
    assert "Bot não envia convite" in client.get("/ajuda").text
    page = client.post(f"/ajuda/{tid}/mensagem", data={"text": "Dê permissão de convidar ao bot."}).text
    assert "48 horas" in page
    assert any(chat == 2002 and "Resposta ao seu pedido" in text for chat, text in sent_messages)
    assert client.post(f"/ajuda/{tid}/aprovar").status_code == 404  # só quem pediu aprova

    _login(client, B_LOGIN)
    page = client.get(f"/ajuda/{tid}").text
    assert "/aprovar" in page and "Aguardando sua aprovação" in page
    assert "Pedido encerrado" in client.post(f"/ajuda/{tid}/aprovar").text
    ticket = client.portal.call(_ticket, tid)
    assert ticket.status == "resolved" and ticket.closed_at
    assert [m.kind for m in ticket.messages] == ["message", "message", "approved"]
    assert [m.is_staff for m in ticket.messages][:2] == [False, True]

    # Encerrado: o histórico continua, mas não recebe mensagens
    assert "está encerrado" in client.post(f"/ajuda/{tid}/mensagem", data={"text": "mais uma"}).text
    assert len(client.portal.call(_ticket, tid).messages) == 3
    assert "Bot não envia convite" in client.get("/ajuda?filtro=encerrados").text
    assert "Bot não envia convite" not in client.get("/ajuda?filtro=ativos").text


def test_reject_reopens_with_comment(client, other_tenant):
    _login(client, B_LOGIN)
    tid = _open_ticket(client)
    assert "Não há solução" in client.post(f"/ajuda/{tid}/recusar").text  # ainda sem resposta
    _login(client, A_LOGIN)
    client.post(f"/ajuda/{tid}/mensagem", data={"text": "Tente reiniciar."})
    _login(client, B_LOGIN)
    client.post(f"/ajuda/{tid}/recusar", data={"text": "Reiniciei e nada."})
    ticket = client.portal.call(_ticket, tid)
    assert ticket.status == "open"
    assert (ticket.messages[-1].kind, ticket.messages[-1].text) == ("rejected", "Reiniciei e nada.")


def test_each_request_is_a_new_conversation(client, other_tenant):
    _login(client, B_LOGIN)
    first, second = _open_ticket(client, "Primeiro"), _open_ticket(client, "Segundo")
    assert first != second
    assert len(client.portal.call(_ticket, first).messages) == 1


def test_users_cannot_see_each_other_tickets(client, other_tenant):
    async def third_user():
        async with SessionLocal() as s:
            s.add(User(username="terceiro", password_hash=hash_password("senha-do-terceiro")))
            await s.commit()

    client.portal.call(third_user)
    _login(client, B_LOGIN)
    tid = _open_ticket(client, "Segredo do outro")
    _login(client, {"username": "terceiro", "password": "senha-do-terceiro"})
    assert "Segredo do outro" not in client.get("/ajuda?filtro=todos").text
    assert client.get(f"/ajuda/{tid}").status_code == 404
    for action in ("mensagem", "aprovar", "recusar"):
        assert client.post(f"/ajuda/{tid}/{action}", data={"text": "x"}).status_code == 404


def test_superadmin_does_not_open_tickets_and_validation(client, other_tenant):
    _login(client, A_LOGIN)
    assert client.post("/ajuda", data={"subject": "x", "text": "y"}).status_code == 404
    _login(client, B_LOGIN)
    assert "Informe o assunto" in client.post("/ajuda", data={"subject": " ", "text": "y"}).text


def test_auto_close_after_48_hours(client, other_tenant, sent_messages):
    client.portal.call(_admins, other_tenant, "2002")
    _login(client, B_LOGIN)
    tid = _open_ticket(client)
    _login(client, A_LOGIN)
    client.post(f"/ajuda/{tid}/mensagem", data={"text": "Resolvido do nosso lado."})

    client.portal.call(_answered_ago, tid, 47)
    client.portal.call(scheduler.close_stale_tickets)
    assert client.portal.call(_ticket, tid).status == "answered"  # ainda no prazo

    client.portal.call(_answered_ago, tid, 49)
    client.portal.call(scheduler.close_stale_tickets)
    ticket = client.portal.call(_ticket, tid)
    assert ticket.status == "closed" and ticket.messages[-1].kind == "auto_closed"
    assert any(chat == 2002 and "encerrado" in text for chat, text in sent_messages)

    _login(client, B_LOGIN)
    page = client.get(f"/ajuda/{tid}").text
    assert "Encerrado sem resposta" in page and "/aprovar" not in page
    assert "Não há solução" in client.post(f"/ajuda/{tid}/aprovar").text


def test_page_closes_overdue_ticket_even_before_scheduler(client, other_tenant):
    _login(client, B_LOGIN)
    tid = _open_ticket(client)
    _login(client, A_LOGIN)
    client.post(f"/ajuda/{tid}/mensagem", data={"text": "Feito."})
    client.portal.call(_answered_ago, tid, 50)
    _login(client, B_LOGIN)
    client.post(f"/ajuda/{tid}/aprovar")  # tarde demais
    assert client.portal.call(_ticket, tid).status == "closed"


# ------------------------------------------------- estatísticas da plataforma


async def _platform_data(other_tenant):
    now = utcnow()
    async with SessionLocal() as s:
        user = await s.get(User, other_tenant)
        user.subscription_until = now + timedelta(days=20)
        s.add_all([
            PlatformPayment(user_id=other_tenant, plan_code="mensal", days=30, amount_cents=4990, method="pix",
                            status="approved", external_ref="p1", paid_at=now),
            PlatformPayment(user_id=other_tenant, plan_code="mensal", days=30, amount_cents=4990, method="pix",
                            status="approved", external_ref="p0", paid_at=now - timedelta(days=40)),
            PlatformPayment(user_id=other_tenant, plan_code="manual", days=30, amount_cents=0, method="manual",
                            status="approved", external_ref="pm", paid_at=now),
            PlatformPayment(user_id=other_tenant, plan_code="mensal", days=30, amount_cents=4990, method="pix",
                            status="pending", external_ref="pp"),
            User(username="trial", password_hash="x", subscription_until=now + timedelta(days=3)),
            User(username="vencido", password_hash="x", subscription_until=now - timedelta(days=1)),
            User(username="isento", password_hash="x", subscription_exempt=True),
        ])
        await s.commit()


async def test_platform_stats(session, other_tenant):
    await _platform_data(other_tenant)
    async with SessionLocal() as s:
        p = await stats.compute_platform(s)
    assert p["revenue_month"] == 4990 and p["sales_month"] == 1
    assert p["revenue_total"] == 9980  # o manual (dias dados) não conta
    assert (p["paying"], p["trial"], p["expired"], p["exempt"]) == (1, 1, 1, 1)
    assert p["mrr_cents"] == 4990
    assert [u.username for u in p["expiring"]] == ["trial"]
    assert p["chart_monthly"]["values"][-1] == 49.9


def test_dashboard_shows_platform_earnings_only_for_superadmin(client, other_tenant):
    client.portal.call(_platform_data, other_tenant)
    _login(client, A_LOGIN)
    page = client.get("/").text
    assert "Com a plataforma" in page and "Assinaturas da plataforma" in page and "chartTotal" in page
    assert "R$ 49,90" in page
    _login(client, B_LOGIN)
    page = client.get("/").text
    assert "Com a plataforma" not in page and "chartTotal" not in page


def test_dashboard_warns_open_tickets(client, other_tenant):
    _login(client, B_LOGIN)
    _open_ticket(client)
    _login(client, A_LOGIN)
    assert "pedido(s) de ajuda aguardando" in client.get("/").text


def test_help_page_has_own_help_popup(client, other_tenant):
    _login(client, B_LOGIN)
    page = client.get("/ajuda").text
    assert "Como funciona: Ajuda" in page and page.count('class="help-btn"') == 1
    assert 'href="/ajuda"' in client.get("/membros").text  # link no menu
