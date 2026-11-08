"""Tarefas periódicas: conciliação de pagamentos, lembretes e expiração (por usuário)."""

import asyncio
import logging
import time
from datetime import timedelta

from sqlalchemy import select

from app import runtime
from app.bot.keyboards import renew_kb
from app.bot.runner import SUBSCRIPTION_EXPIRED
from app.db import SessionLocal
from app.models import Member, User
from app.services import membership, notifier, payments, platform, reports, support
from app.services.mercadopago import MercadoPagoError
from app.services.store import get_message, get_plan, get_settings
from app.utils import fmt_date, utcnow

log = logging.getLogger(__name__)

TICK_SECONDS = 20
FULL_PENDING_EVERY = 10 * 60
LIFECYCLE_EVERY = 60
RECURRING_SYNC_EVERY = 30 * 60


async def active_tenants() -> list[int]:
    """Usuários ativos com assinatura da plataforma em dia (os demais ficam pausados)."""
    async with SessionLocal() as session:
        users = (await session.scalars(select(User).where(User.active.is_(True)))).all()
        return [u.id for u in users if u.subscription_active]


async def enforce_subscriptions(bots, reminders: bool = False, sync_recurring: bool = False) -> None:
    """Assinatura da plataforma: renovou, liga o bot; venceu, avisa e para o bot (o painel continua)."""
    async with SessionLocal() as session:
        renewed = await platform.check_pending(session)
        if sync_recurring:
            renewed += await platform.sync_all_recurring(session)
        if reminders:
            await platform.send_reminders(session)
        expired = await platform.expired_tenants(session)
    for tenant_id in renewed:
        await bots.start_tenant(tenant_id)

    for tenant_id in expired:
        async with SessionLocal() as session:
            user = await session.get(User, tenant_id)
            if runtime.get_bot(tenant_id) is not None and user.platform_preapproval_id:
                # Última chance: a cobrança automática do cartão pode ter acabado de cair.
                try:
                    if await platform.sync_recurring(session, user):
                        await bots.start_tenant(tenant_id)
                        continue
                except MercadoPagoError as exc:
                    log.warning("Falha ao sincronizar recorrência da plataforma (%s): %s", user.username, exc)
            await platform.notify_expired(session, user)  # antes de parar: o aviso sai pelo próprio bot
        if runtime.get_bot(tenant_id) is not None:
            log.info("Assinatura do usuário %s venceu: parando o bot", tenant_id)
            await bots.stop(tenant_id, reason=SUBSCRIPTION_EXPIRED)


async def process_lifecycle(tenant_id: int) -> None:
    """Envia lembretes e expira (e remove) quem passou da carência."""
    async with SessionLocal() as session:
        settings = await get_settings(session, tenant_id)
        now = utcnow()
        grace = timedelta(hours=settings.as_int("grace_hours", 24))
        reminder_days = settings.reminder_days
        members = (
            await session.scalars(select(Member).where(Member.tenant_id == tenant_id, Member.status == "active"))
        ).all()

        for member in members:
            if member.expires_at is None:
                continue
            plan = await get_plan(session, tenant_id, member.plan_code)
            values = {
                "nome": member.display_name,
                "plano": plan.name if plan else (member.plan_code or "-"),
                "vencimento": fmt_date(member.expires_at),
            }

            if member.expires_at + grace < now:
                if member.recurring_status == "authorized" and settings.get("mp_access_token"):
                    # Última chance: a cobrança do mês pode ter acabado de cair.
                    try:
                        await payments.sync_recurring(session, settings, member)
                    except MercadoPagoError as exc:
                        log.warning("Falha ao sincronizar recorrência de %s: %s", member.telegram_id, exc)
                    if member.expires_at + grace >= utcnow():
                        continue
                membership.mark_expired(session, member)
                if settings.flag("remove_on_expire"):
                    await notifier.kick(session, settings, member)
                await session.commit()
                text = await get_message(session, tenant_id, "expired", **values)
                await notifier.send(tenant_id, member.telegram_id, text, renew_kb())
                continue

            if member.recurring_status == "authorized":
                continue  # cobrança automática: sem lembretes de renovação
            offset = membership.due_reminder(member, reminder_days, now)
            if offset is None:
                continue
            key = "reminder_today" if offset == 0 else "reminder"
            text = await get_message(session, tenant_id, key, dias=notifier.remaining_days(member), **values)
            await notifier.send(tenant_id, member.telegram_id, text, renew_kb())
            membership.register_reminder(member, offset, reminder_days)
            await session.commit()


async def sync_all_recurring(tenant_id: int) -> None:
    async with SessionLocal() as session:
        settings = await get_settings(session, tenant_id)
        if not settings.get("mp_access_token"):
            return
        members = (
            await session.scalars(
                select(Member).where(
                    Member.tenant_id == tenant_id,
                    Member.mp_preapproval_id.is_not(None),
                    Member.recurring_status == "authorized",
                )
            )
        ).all()
        for member in members:
            try:
                await payments.sync_recurring(session, settings, member)
            except MercadoPagoError as exc:
                log.warning("Falha ao sincronizar recorrência de %s: %s", member.telegram_id, exc)


async def run_tenant(tenant_id: int, full: bool, life: bool, rec: bool) -> None:
    async with SessionLocal() as session:
        await payments.check_pending(session, tenant_id, recent_only=not full)
    if life:
        await process_lifecycle(tenant_id)
        async with SessionLocal() as session:
            await reports.send_weekly(session, tenant_id)
    if rec:
        await sync_all_recurring(tenant_id)


async def close_stale_tickets() -> None:
    """Pedidos de ajuda com solução sem aprovação há 48 horas são encerrados e o usuário é avisado."""
    async with SessionLocal() as session:
        closed = await support.close_stale(session)
    for ticket_id in closed:
        await support.notify_owner(ticket_id, auto_closed=True)


async def run_forever(bots) -> None:
    last = {"full": 0.0, "life": 0.0, "rec": 0.0}
    while True:
        started = time.monotonic()
        full = started - last["full"] >= FULL_PENDING_EVERY
        life = started - last["life"] >= LIFECYCLE_EVERY
        rec = started - last["rec"] >= RECURRING_SYNC_EVERY
        try:
            await enforce_subscriptions(bots, reminders=life, sync_recurring=rec)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - o agendador nunca pode morrer
            log.exception("Erro ao verificar assinaturas da plataforma")
        try:
            await close_stale_tickets()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - o agendador nunca pode morrer
            log.exception("Erro ao encerrar pedidos de ajuda")
        try:
            tenants = await active_tenants()
        except Exception:  # noqa: BLE001 - o agendador nunca pode morrer
            log.exception("Erro ao listar usuários")
            tenants = []
        for tenant_id in tenants:
            try:
                await run_tenant(tenant_id, full, life, rec)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - erro de um usuário não para os demais
                log.exception("Erro no agendador (usuário %s)", tenant_id)
        last.update({k: started for k, due in (("full", full), ("life", life), ("rec", rec)) if due})
        await asyncio.sleep(TICK_SECONDS)
