"""Regressões da revisão: aprovação concorrente, HTML nas mensagens e comprovante."""

import asyncio
from datetime import timedelta
from types import SimpleNamespace

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app import runtime
from app.db import SessionLocal
from app.main import app
from app.models import Member, Payment, Plan
from app.services import payments
from app.services.store import get_message, set_settings
from app.utils import utcnow
from tests.tenant import TID


async def _member_with_pending(session, method="pix_manual"):
    await set_settings(session, TID, {"admin_ids": "999", "mp_access_token": "TEST-token"})
    member = Member(tenant_id=TID, telegram_id=222, first_name="Bia")
    session.add(member)
    await session.flush()
    payment = Payment(tenant_id=TID, member_id=member.id, plan_code="mensal", amount_cents=2990, method=method, external_ref="r-1")
    session.add(payment)
    await session.commit()
    return member, payment


async def _approve_in_new_session(payment_id):
    async with SessionLocal() as s:
        return await payments.approve(s, await s.get(Payment, payment_id))


async def test_concurrent_approve_extends_once(session):
    member, payment = await _member_with_pending(session)
    results = await asyncio.gather(*[_approve_in_new_session(payment.id) for _ in range(3)])
    assert sorted(results) == [False, False, True]
    async with SessionLocal() as s:
        fresh = await s.get(Member, member.id)
        assert abs((fresh.expires_at - utcnow()) - timedelta(days=30)) < timedelta(minutes=1)


async def test_concurrent_recurring_sync_registers_charge_once(session, monkeypatch):
    member, _ = await _member_with_pending(session)
    member.mp_preapproval_id, member.recurring_status = "pre-9", "authorized"
    await session.commit()
    pre = {"status": "authorized", "summarized": {"last_charged_date": "2026-10-01T12:00:00.000-03:00", "last_charged_amount": 24.9}}

    class FakeMP:
        async def get_preapproval(self, _id):
            await asyncio.sleep(0)  # deixa a outra sincronização intercalar
            return pre

    monkeypatch.setattr(payments, "mp_client", lambda settings: FakeMP())

    async def sync():
        async with SessionLocal() as s:
            try:
                await payments.sync_recurring(s, None, await s.get(Member, member.id))
            except Exception:  # noqa: BLE001 - no SQLite a 2ª pode bater na unique
                await s.rollback()

    await asyncio.gather(sync(), sync())
    async with SessionLocal() as s:
        approved = await s.scalar(
            select(func.count()).select_from(Payment).where(Payment.method == "card_recurring", Payment.status == "approved")
        )
    assert approved == 1


async def test_message_values_are_html_escaped(session):
    text = await get_message(session, TID, "welcome", nome="Tom & <Jerry>", saudacao="Oi")
    assert "Tom &amp; &lt;Jerry&gt;" in text
    assert "<" not in text.replace("&lt;", "")  # o texto padrão não tem tags; nenhum < cru sobrou


class _FakeBot:
    def __init__(self, path, content):
        self.path, self.content = path, content

    async def get_file(self, file_id):
        return SimpleNamespace(file_path=self.path)

    async def download(self, file):
        import io

        return io.BytesIO(self.content)


def _proof_response(path, monkeypatch):
    async def prepare():
        async with SessionLocal() as s:
            _, payment = await _member_with_pending(s)
            payment.proof_file_id = "doc:abc"
            await s.commit()
            return payment.id

    monkeypatch.setitem(runtime.bots, TID, _FakeBot(path, b"<script>alert(1)</script>"))
    with TestClient(app) as client:
        payment_id = client.portal.call(prepare)
        client.post("/login", data={"username": "admin", "password": "senha-teste"})
        return client.get(f"/pagamentos/{payment_id}/comprovante")


def test_proof_html_is_never_rendered(monkeypatch):
    r = _proof_response("documents/file_1.html", monkeypatch)
    assert r.headers["content-type"] == "application/octet-stream"
    assert r.headers["content-disposition"].startswith("attachment")
    assert r.headers["x-content-type-options"] == "nosniff"


def test_proof_pdf_is_inline(monkeypatch):
    r = _proof_response("documents/file_2.pdf", monkeypatch)
    assert r.headers["content-type"] == "application/pdf"
    assert "content-disposition" not in r.headers


def test_plan_days_not_numeric_does_not_crash():
    with TestClient(app) as client:
        client.post("/login", data={"username": "admin", "password": "senha-teste"})
        r = client.post("/planos", data={"mensal_price": "29,90", "mensal_days": "trinta", "mensal_active": "on"})
        assert r.status_code == 200 and "duração inválida" in r.text


# ------------------------------------------------- limites e validações


async def test_charge_limit_cooldown_and_hourly(session):
    member, _ = await _member_with_pending(session)
    assert await payments.charge_limit_reason(session, member) is None  # pix_manual não conta

    session.add(Payment(tenant_id=TID, member_id=member.id, plan_code="mensal", amount_cents=1, method="pix", external_ref="c-0"))
    await session.commit()
    assert "Aguarde" in await payments.charge_limit_reason(session, member)

    old = utcnow() - timedelta(minutes=5)
    for i in range(1, 5):
        session.add(Payment(tenant_id=TID, member_id=member.id, plan_code="mensal", amount_cents=1, method="card",
                            external_ref=f"c-{i}", created_at=old))
    first = await session.scalar(select(Payment).where(Payment.external_ref == "c-0"))
    first.created_at = old
    await session.commit()
    assert "muitas cobranças" in await payments.charge_limit_reason(session, member)


async def test_reusable_pix_only_recent_same_plan(session):
    member, _ = await _member_with_pending(session)
    plan = await session.get(Plan, (TID, "mensal"))
    pix = Payment(tenant_id=TID, member_id=member.id, plan_code="mensal", amount_cents=2990, method="pix",
                  external_ref="p-1", pix_code="000201...")
    session.add(pix)
    await session.commit()
    assert (await payments.reusable_pix(session, member, plan)).id == pix.id
    assert await payments.reusable_pix(session, member, await session.get(Plan, (TID, "trimestral"))) is None
    pix.created_at = utcnow() - timedelta(minutes=25)
    await session.commit()
    assert await payments.reusable_pix(session, member, plan) is None


def test_member_days_out_of_range_rejected():
    with TestClient(app) as client:
        client.post("/login", data={"username": "admin", "password": "senha-teste"})
        for days in ("0", "-5", "99999"):
            r = client.post("/membros/adicionar", data={"telegram_id": "888", "days": days})
            assert "Informe de 1 a 3650 dias" in r.text
        assert "888" not in client.get("/membros?status=all").text


async def test_login_failures_are_pruned(session, monkeypatch):
    from app.web import auth

    monkeypatch.setattr(auth, "MAX_TRACKED_IPS", 3)
    monkeypatch.setattr(auth, "_failures", {})
    for i in range(10):
        assert await auth.authenticate(session, f"10.0.0.{i}", "x", "y") is None
    assert len(auth._failures) <= 3
    assert "10.0.0.9" in auth._failures  # o mais recente continua sendo contado


def test_cookie_secure_flag(monkeypatch):
    from starlette.middleware.sessions import SessionMiddleware

    session_mw = next(m for m in app.user_middleware if m.cls is SessionMiddleware)
    assert session_mw.kwargs["https_only"] is False  # padrão: funciona em http://localhost
