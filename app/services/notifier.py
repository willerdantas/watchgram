"""Tudo o que fala com o Telegram fora dos handlers: avisos, convites e remoções.

Cada usuário (tenant) tem o próprio bot: toda função recebe o tenant, direto ou
via Settings/Member, e usa só o bot dele.
"""

import html
import logging

from aiogram.exceptions import TelegramAPIError
from aiogram.types import InlineKeyboardMarkup
from sqlalchemy.ext.asyncio import AsyncSession

from app import runtime
from app.bot.keyboards import METHOD_LABELS, plans_kb, url_kb
from app.models import Member, Payment, Plan
from app.services import membership
from app.services.store import Settings, get_message, get_plans, get_settings
from app.utils import fmt_date, money, utcnow

log = logging.getLogger(__name__)


async def send(tenant_id: int, chat_id: int, text: str, reply_markup: InlineKeyboardMarkup | None = None) -> bool:
    bot = runtime.get_bot(tenant_id)
    if bot is None:
        log.warning("Bot do usuário %s não configurado; mensagem para %s descartada", tenant_id, chat_id)
        return False
    try:
        await bot.send_message(chat_id, text, reply_markup=reply_markup)
        return True
    except TelegramAPIError as exc:
        log.warning("Falha ao enviar mensagem para %s (usuário %s): %s", chat_id, tenant_id, exc)
        return False


async def plans_markup(session: AsyncSession, tenant_id: int) -> InlineKeyboardMarkup | None:
    """Botões com os planos ativos e seus valores (o mesmo menu do /start)."""
    plans = await get_plans(session, tenant_id, only_active=True)
    return plans_kb(plans) if plans else None


async def notify_admins(settings: Settings, text: str, reply_markup: InlineKeyboardMarkup | None = None) -> None:
    for admin_id in settings.admin_ids:
        await send(settings.tenant_id, admin_id, text, reply_markup)


async def alert(
    session: AsyncSession, settings: Settings, key: str, subject: str, text: str, telegram_text: str | None = None
) -> None:
    """Alerta do dono da área, conforme a página Alertas (texto puro; telegram_text já vem em HTML)."""
    from app.services import mailer
    from app.services.platform import get_platform_settings

    if not settings.flag(key):
        return
    if settings.flag("alert_telegram"):
        await notify_admins(settings, telegram_text or f"<b>{html.escape(subject)}</b>\n{html.escape(text)}")
    if settings.flag("alert_email") and settings.get("alert_email_to"):
        await mailer.send_email(await get_platform_settings(session), settings["alert_email_to"], subject, text)


async def create_invite(settings: Settings, member: Member) -> str | None:
    """Link com pedido de entrada: só o próprio assinante é aprovado (ver handlers_group)."""
    bot = runtime.get_bot(settings.tenant_id)
    chat_id = settings.group_chat_id
    if bot is None or chat_id is None:
        return None
    try:
        link = await bot.create_chat_invite_link(
            chat_id, name=f"assinante {member.telegram_id}"[:32], creates_join_request=True
        )
        return link.invite_link
    except TelegramAPIError as exc:
        log.error("Falha ao criar convite (usuário %s): %s", settings.tenant_id, exc)
        return None


async def kick(session: AsyncSession, settings: Settings, member: Member) -> bool:
    bot = runtime.get_bot(settings.tenant_id)
    chat_id = settings.group_chat_id
    if bot is None or chat_id is None:
        return False
    try:
        await bot.ban_chat_member(chat_id, member.telegram_id)
        # Desbanir em seguida: remove sem impedir que volte após pagar.
        await bot.unban_chat_member(chat_id, member.telegram_id, only_if_banned=True)
        membership.set_in_group(session, member, False, detail="removido")
        return True
    except TelegramAPIError as exc:
        log.warning("Falha ao remover %s do grupo: %s", member.telegram_id, exc)
        return False


async def send_access(session: AsyncSession, member: Member, plan_name: str) -> None:
    """Primeiro acesso: envia convite. Já no grupo: só confirma a renovação."""
    tenant_id = member.tenant_id
    settings = await get_settings(session, tenant_id)
    values = {"nome": member.display_name, "plano": plan_name, "vencimento": fmt_date(member.expires_at)}
    if member.in_group:
        await send(tenant_id, member.telegram_id, await get_message(session, tenant_id, "renewed", **values))
        return
    text = await get_message(session, tenant_id, "payment_approved", **values)
    link = await create_invite(settings, member)
    if link:
        await send(tenant_id, member.telegram_id, text, url_kb("👉 Entrar no grupo", link))
    else:
        await send(tenant_id, member.telegram_id, text + "\n\n(O link será enviado em instantes pelo administrador.)")
        await notify_admins(
            settings,
            f"⚠️ Não consegui gerar o convite para {html.escape(member.display_name)} ({member.telegram_id}). "
            "Confira o grupo em Configurações.",
        )


async def payment_approved(session: AsyncSession, payment: Payment, member: Member, plan: Plan | None) -> None:
    settings = await get_settings(session, member.tenant_id)
    plan_name = plan.name if plan else payment.plan_code
    await send_access(session, member, plan_name)
    value = money(payment.amount_cents, payment.currency)
    await alert(
        session,
        settings,
        "alert_sale",
        f"Nova venda: {plan_name}",
        f"{member.display_name} comprou o plano {plan_name} por {value}.",
        await get_message(
            session,
            member.tenant_id,
            "admin_new_sale",
            nome=member.display_name,
            telegram_id=member.telegram_id,
            plano=plan_name,
            valor=value,
            metodo=METHOD_LABELS.get(payment.method, payment.method),
        ),
    )


async def payment_rejected(session: AsyncSession, payment: Payment, member: Member) -> None:
    settings = await get_settings(session, member.tenant_id)
    text = await get_message(
        session,
        member.tenant_id,
        "payment_rejected",
        nome=member.display_name,
        plano=payment.plan_code,
        valor=money(payment.amount_cents, payment.currency),
        suporte=settings.get("support_contact") or "-",
    )
    await send(member.tenant_id, member.telegram_id, text, await plans_markup(session, member.tenant_id))


def remaining_days(member: Member) -> int:
    if member.expires_at is None:
        return 0
    return max(0, (member.expires_at - utcnow()).days)
