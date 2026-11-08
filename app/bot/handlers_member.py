"""Conversa com o assinante no privado: planos, pagamento, status e cancelamento.

Todo handler recebe tenant_id (o dono do bot que recebeu a mensagem; ver
runner.py) e só lê ou grava dados desse usuário.
"""

import base64
import html
import logging

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import BufferedInputFile, CallbackQuery, Message

from app.bot.keyboards import (
    available_methods,
    manual_review_kb,
    menu_kb,
    methods_kb,
    pix_check_kb,
    plans_kb,
    url_kb,
)
from app.db import SessionLocal
from app.models import Member, Payment
from app.services import brcode, membership, notifier, payments
from app.services.mercadopago import MercadoPagoError
from app.services.store import find_custom_message, get_message, get_plan, get_plans, get_settings, render_custom
from app.utils import EMAIL_RE, brl, fmt_date, matches_keyword, money, parse_keywords, time_greeting

log = logging.getLogger(__name__)
router = Router()
router.message.filter(F.chat.type == "private")

NEEDS_EMAIL = {"pix", "card", "card_recurring"}


class Flow(StatesGroup):
    email = State()
    proof = State()


async def _send_plans(bot: Bot, tenant_id: int, chat_id: int, member: Member) -> None:
    async with SessionLocal() as session:
        plans = await get_plans(session, tenant_id, only_active=True)
        text = await get_message(session, tenant_id, "welcome", nome=member.display_name, saudacao=time_greeting())
    if not plans:
        await bot.send_message(chat_id, "Nenhum plano disponível no momento.")
        return
    await bot.send_message(chat_id, text, reply_markup=plans_kb(plans))


@router.message(CommandStart())
@router.message(Command("planos"))
async def cmd_start(message: Message, state: FSMContext, tenant_id: int) -> None:
    await state.clear()
    async with SessionLocal() as session:
        member = await membership.get_or_create_member(session, tenant_id, message.from_user)
        await session.commit()
    await _send_plans(message.bot, tenant_id, message.chat.id, member)


@router.callback_query(F.data == "plans")
async def cb_plans(callback: CallbackQuery, state: FSMContext, tenant_id: int) -> None:
    await state.clear()
    async with SessionLocal() as session:
        member = await membership.get_or_create_member(session, tenant_id, callback.from_user)
        await session.commit()
    await callback.answer()
    await _send_plans(callback.bot, tenant_id, callback.from_user.id, member)


@router.callback_query(F.data.startswith("plan:"))
async def cb_plan(callback: CallbackQuery, state: FSMContext, tenant_id: int) -> None:
    await state.clear()
    code = callback.data.split(":", 1)[1]
    async with SessionLocal() as session:
        plan = await get_plan(session, tenant_id, code)
        settings = await get_settings(session, tenant_id)
        if plan is None or not plan.active:
            await callback.answer("Plano indisponível.", show_alert=True)
            return
        methods = available_methods(plan, settings)
        text = await get_message(
            session,
            tenant_id,
            "choose_method",
            nome=callback.from_user.first_name,
            plano=plan.name,
            valor=brl(plan.price_cents),
            descricao=plan.description,
        )
    await callback.answer()
    if not methods:
        await callback.message.answer("Nenhuma forma de pagamento está disponível para este plano no momento.")
        return
    await callback.message.answer(text, reply_markup=methods_kb(plan, methods, settings.get("paypal_currency", "USD")))


@router.callback_query(F.data.startswith("pay:"))
async def cb_pay(callback: CallbackQuery, state: FSMContext, tenant_id: int) -> None:
    _, code, method = callback.data.split(":", 2)
    await callback.answer()
    async with SessionLocal() as session:
        member = await membership.get_or_create_member(session, tenant_id, callback.from_user)
        await session.commit()
        if method in NEEDS_EMAIL and not member.email:
            await state.set_state(Flow.email)
            await state.update_data(plan=code, method=method)
            await callback.message.answer(await get_message(session, tenant_id, "ask_email", nome=member.display_name))
            return
    await _start_payment(callback.bot, tenant_id, callback.from_user.id, code, method, state)


@router.message(StateFilter(Flow.email), F.text, ~F.text.startswith("/"))
async def msg_email(message: Message, state: FSMContext, tenant_id: int) -> None:
    email = message.text.strip().lower()
    if not EMAIL_RE.match(email):
        await message.answer("E-mail inválido. Envie novamente, por exemplo: nome@gmail.com")
        return
    data = await state.get_data()
    async with SessionLocal() as session:
        member = await membership.get_or_create_member(session, tenant_id, message.from_user)
        member.email = email
        await session.commit()
    await state.clear()
    await _start_payment(message.bot, tenant_id, message.chat.id, data["plan"], data["method"], state)


async def _start_payment(bot: Bot, tenant_id: int, chat_id: int, code: str, method: str, state: FSMContext) -> None:
    async with SessionLocal() as session:
        settings = await get_settings(session, tenant_id)
        plan = await get_plan(session, tenant_id, code)
        member = await membership.get_member_by_tg(session, tenant_id, chat_id)
        if plan is None or not plan.active or method not in available_methods(plan, settings):
            await bot.send_message(
                chat_id,
                "Opção indisponível. Escolha um dos planos abaixo:",
                reply_markup=await notifier.plans_markup(session, tenant_id),
            )
            return
        values = {"nome": member.display_name, "plano": plan.name, "valor": brl(plan.price_cents)}
        if method == "pix" and (pending := await payments.reusable_pix(session, member, plan)):
            # Reenvia o Pix ainda válido em vez de gerar outro.
            text = await get_message(session, tenant_id, "pix_created", pix_codigo=pending.pix_code, **values)
            await bot.send_message(chat_id, text, reply_markup=pix_check_kb(pending.id))
            return
        if method in payments.GATEWAY_METHODS and (reason := await payments.charge_limit_reason(session, member)):
            await bot.send_message(chat_id, reason)
            return
        try:
            if method == "pix":
                payment, qr_b64 = await payments.start_pix(session, settings, member, plan)
                if qr_b64:
                    await bot.send_photo(chat_id, BufferedInputFile(base64.b64decode(qr_b64), "pix.png"))
                text = await get_message(session, tenant_id, "pix_created", pix_codigo=payment.pix_code or "", **values)
                await bot.send_message(chat_id, text, reply_markup=pix_check_kb(payment.id))
            elif method == "card":
                payment = await payments.start_card(session, settings, member, plan)
                text = await get_message(session, tenant_id, "card_link", **values)
                await bot.send_message(chat_id, text, reply_markup=url_kb("💳 Pagar com cartão", payment.checkout_url))
            elif method == "paypal":
                payment = await payments.start_paypal(session, settings, member, plan)
                values["valor"] = money(payment.amount_cents, payment.currency)
                text = await get_message(session, tenant_id, "paypal_link", **values)
                await bot.send_message(chat_id, text, reply_markup=url_kb("🌎 Pagar com PayPal", payment.checkout_url))
            elif method == "card_recurring":
                if member.recurring_status == "authorized" and membership.is_active(member):
                    await bot.send_message(chat_id, "Você já tem uma assinatura recorrente ativa. Veja em /status.")
                    return
                payment = await payments.start_recurring(session, settings, member, plan)
                text = await get_message(session, tenant_id, "recurring_link", **values)
                await bot.send_message(chat_id, text, reply_markup=url_kb("💳 Assinar no cartão", payment.checkout_url))
            elif method == "pix_manual":
                payment = await payments.start_manual_pix(session, member, plan, settings)
                pix = {"pix_chave": settings.get("pix_key", ""), "pix_titular": settings.get("pix_holder", "")}
                if payment.pix_code:  # QR Code ligado em Configurações; a conferência continua pelo comprovante
                    await bot.send_photo(chat_id, BufferedInputFile(brcode.qr_png(payment.pix_code), "pix.png"))
                    text = await get_message(session, tenant_id, "pix_manual_qr", pix_codigo=payment.pix_code, **pix, **values)
                else:
                    text = await get_message(session, tenant_id, "pix_manual", **pix, **values)
                await state.set_state(Flow.proof)
                await state.update_data(payment_id=payment.id)
                await bot.send_message(chat_id, text)
        except payments.GATEWAY_ERRORS as exc:
            gateway = "PayPal" if method == "paypal" else "Mercado Pago"
            log.error("Erro no %s (%s, usuário %s): %s", gateway, method, tenant_id, exc)
            await bot.send_message(chat_id, "Não foi possível gerar a cobrança agora. Tente novamente em instantes.")
            await notifier.notify_admins(settings, f"⚠️ Erro no {gateway} ao gerar {method}: {html.escape(str(exc)[:300])}")


async def _own_payment(session, tenant_id: int, telegram_id: int, payment_id) -> Payment | None:
    """Pagamento do próprio assinante, neste bot; None para qualquer outro."""
    payment = await session.get(Payment, payment_id) if payment_id else None
    member = await membership.get_member_by_tg(session, tenant_id, telegram_id)
    if payment is None or member is None or payment.tenant_id != tenant_id or payment.member_id != member.id:
        return None
    return payment


@router.callback_query(F.data.startswith("check:"))
async def cb_check(callback: CallbackQuery, tenant_id: int) -> None:
    payment_id = int(callback.data.split(":", 1)[1])
    async with SessionLocal() as session:
        payment = await _own_payment(session, tenant_id, callback.from_user.id, payment_id)
        if payment is None:
            await callback.answer("Pagamento não encontrado.", show_alert=True)
            return
        if payment.status == "pending":
            try:
                await payments.refresh_payment(session, await get_settings(session, tenant_id), payment)
            except payments.GATEWAY_ERRORS as exc:
                log.warning("Falha ao verificar pagamento %s: %s", payment.id, exc)
        if payment.status == "approved":
            await callback.answer("Pagamento confirmado! ✅")
        else:
            await callback.answer("Ainda não identificamos o pagamento. Aguarde alguns segundos.", show_alert=True)


PROOF_DOC_TYPES = F.document.mime_type.startswith("image/") | (F.document.mime_type == "application/pdf")


@router.message(StateFilter(Flow.proof), F.photo | PROOF_DOC_TYPES)
async def msg_proof(message: Message, state: FSMContext, tenant_id: int) -> None:
    data = await state.get_data()
    is_photo = bool(message.photo)
    file_id = message.photo[-1].file_id if is_photo else message.document.file_id
    async with SessionLocal() as session:
        payment = await _own_payment(session, tenant_id, message.from_user.id, data.get("payment_id"))
        if payment is None or payment.status != "pending":
            await state.clear()
            await message.answer(
                "Essa cobrança não está mais pendente. Escolha um plano abaixo para gerar outra:",
                reply_markup=await notifier.plans_markup(session, tenant_id),
            )
            return
        payment.proof_file_id = f"{'photo' if is_photo else 'doc'}:{file_id}"
        await session.commit()
        settings = await get_settings(session, tenant_id)
        member = await session.get(Member, payment.member_id)
        plan = await get_plan(session, tenant_id, payment.plan_code)
        await message.answer(
            await get_message(
                session,
                tenant_id,
                "proof_received",
                nome=member.display_name,
                plano=plan.name if plan else payment.plan_code,
                valor=brl(payment.amount_cents),
            )
        )
    await state.clear()
    caption = (
        f"🧾 Comprovante Pix #{payment.id}\n{html.escape(member.display_name)} (id {member.telegram_id})\n"
        f"Plano: {html.escape(plan.name if plan else payment.plan_code)} — {brl(payment.amount_cents)}"
    )
    for admin_id in settings.admin_ids:
        try:
            if is_photo:
                await message.bot.send_photo(admin_id, file_id, caption=caption, reply_markup=manual_review_kb(payment.id))
            else:
                await message.bot.send_document(admin_id, file_id, caption=caption, reply_markup=manual_review_kb(payment.id))
        except Exception as exc:  # noqa: BLE001 - um admin inacessível não deve travar os demais
            log.warning("Falha ao enviar comprovante ao admin %s: %s", admin_id, exc)


@router.message(StateFilter(Flow.proof))
async def msg_proof_invalid(message: Message, tenant_id: int) -> None:
    async with SessionLocal() as session:
        markup = await notifier.plans_markup(session, tenant_id)
    await message.answer(
        "Envie a <b>foto</b> ou o <b>PDF</b> do comprovante, ou escolha outro plano abaixo:", reply_markup=markup
    )


@router.message(Command("status"))
async def cmd_status(message: Message, tenant_id: int) -> None:
    async with SessionLocal() as session:
        member = await membership.get_member_by_tg(session, tenant_id, message.from_user.id)
        if not membership.is_active(member):
            text = await get_message(session, tenant_id, "status_none", nome=message.from_user.first_name)
            await message.answer(text, reply_markup=await notifier.plans_markup(session, tenant_id))
            return
        plan = await get_plan(session, tenant_id, member.plan_code)
        recorrencia = {
            "authorized": "ativa (cobrança automática)",
            "cancelled": "cancelada",
            "paused": "pausada",
        }.get(member.recurring_status or "", "não")
        await message.answer(
            await get_message(
                session,
                tenant_id,
                "status_active",
                nome=member.display_name,
                plano=plan.name if plan else (member.plan_code or "-"),
                vencimento=fmt_date(member.expires_at),
                recorrencia=recorrencia,
            )
        )


@router.message(Command("cancelar"))
async def cmd_cancel(message: Message, tenant_id: int) -> None:
    async with SessionLocal() as session:
        member = await membership.get_member_by_tg(session, tenant_id, message.from_user.id)
        if member is None or not member.mp_preapproval_id or member.recurring_status not in ("authorized", "pending", "paused"):
            await message.answer("Você não tem cobrança recorrente ativa.")
            return
        settings = await get_settings(session, tenant_id)
        try:
            await payments.cancel_recurring(session, settings, member)
        except MercadoPagoError as exc:
            log.error("Falha ao cancelar recorrência de %s: %s", member.telegram_id, exc)
            await message.answer("Não foi possível cancelar agora. Tente novamente em instantes.")
            return
        await message.answer(
            await get_message(
                session, tenant_id, "recurring_canceled", nome=member.display_name, vencimento=fmt_date(member.expires_at)
            )
        )


@router.message(StateFilter(None), F.text, ~F.text.startswith("/"))
async def msg_free_text(message: Message, tenant_id: int) -> None:
    """Texto livre: mensagens personalizadas, depois saudações ("oi"...) abrem o menu; o resto recebe uma ajuda."""
    async with SessionLocal() as session:
        settings = await get_settings(session, tenant_id)
        member = await membership.get_or_create_member(session, tenant_id, message.from_user)
        await session.commit()
        custom = await find_custom_message(session, tenant_id, message.text)
        if custom is not None:
            text = render_custom(custom, settings, member.display_name)
            await message.answer(text, reply_markup=menu_kb() if custom.show_plans else None)
            return
        if not matches_keyword(message.text, parse_keywords(settings.get("start_keywords", ""))):
            if settings.flag("reply_unknown"):
                text = await get_message(
                    session, tenant_id, "unknown_text", nome=member.display_name, saudacao=time_greeting()
                )
                await message.answer(text, reply_markup=menu_kb())
            return
    await _send_plans(message.bot, tenant_id, message.chat.id, member)
