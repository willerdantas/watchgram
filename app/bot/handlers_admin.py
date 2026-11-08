"""Ações dos administradores (do dono do bot) dentro do Telegram."""

import html

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from app.db import SessionLocal
from app.models import Payment
from app.services import payments, stats
from app.services.store import get_settings
from app.utils import brl

router = Router()


async def _is_admin(tenant_id: int, user_id: int) -> bool:
    async with SessionLocal() as session:
        return user_id in (await get_settings(session, tenant_id)).admin_ids


@router.callback_query(F.data.startswith("manual:"))
async def cb_manual(callback: CallbackQuery, tenant_id: int) -> None:
    if not await _is_admin(tenant_id, callback.from_user.id):
        await callback.answer("Apenas administradores.", show_alert=True)
        return
    _, action, payment_id = callback.data.split(":", 2)
    admin_name = html.escape(callback.from_user.first_name)
    async with SessionLocal() as session:
        payment = await session.get(Payment, int(payment_id))
        if payment is None or payment.tenant_id != tenant_id or payment.status != "pending":
            await callback.answer("Este pagamento já foi processado.", show_alert=True)
            return
        if action == "approve":
            await payments.approve(session, payment)
            result = f"✅ Aprovado por {admin_name}"
        else:
            await payments.reject(session, payment)
            result = f"❌ Recusado por {admin_name}"
    await callback.answer(html.unescape(result))
    caption = html.escape(callback.message.caption or "") + f"\n\n{result}"
    await callback.message.edit_caption(caption=caption, reply_markup=None)


@router.message(Command("id"))
async def cmd_id(message: Message) -> None:
    """Mostra o id do chat atual (útil para configurar o grupo e os admins)."""
    await message.reply(f"Chat id: <code>{message.chat.id}</code>\nSeu id: <code>{message.from_user.id}</code>")


@router.message(Command("stats"), F.chat.type == "private")
async def cmd_stats(message: Message, tenant_id: int) -> None:
    if not await _is_admin(tenant_id, message.from_user.id):
        return
    async with SessionLocal() as session:
        s = await stats.compute(session, tenant_id)
    await message.answer(
        "📊 <b>Resumo</b>\n"
        f"Assinantes ativos: <b>{s['active']}</b> (recorrentes: {s['recurring_active']})\n"
        f"MRR estimado: <b>{brl(s['mrr_cents'])}</b>\n"
        f"Receita no mês: <b>{brl(s['revenue_month'])}</b> ({s['sales_month']} vendas)\n"
        f"Novos no mês: {s['new_month']} · Saídas: {s['churned_month']} · Churn: {s['churn_rate']:.1f}%\n"
        f"Vencendo em 7 dias: {len(s['upcoming'])}\n"
        f"Comprovantes pendentes: {s['pending_manual']}"
    )
