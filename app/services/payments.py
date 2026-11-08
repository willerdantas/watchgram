"""Criação de cobranças e conciliação com o Mercado Pago e o PayPal."""

import logging
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app import config, runtime
from app.models import Event, Member, Payment, Plan
from app.services import brcode, membership, notifier, paypal
from app.services.mercadopago import MercadoPago, MercadoPagoError
from app.services.paypal import PayPal, PayPalError
from app.services.store import Settings, get_plan, get_settings
from app.utils import utcnow

log = logging.getLogger(__name__)

MP_STATUS = {
    "approved": "approved",
    "rejected": "rejected",
    "cancelled": "rejected",
    "refunded": "rejected",
    "charged_back": "rejected",
}

PENDING_TTL = timedelta(hours=48)

# Limites por assinante para cobranças no Mercado Pago (evita spam no botão).
CHARGE_COOLDOWN = timedelta(seconds=30)
CHARGES_PER_HOUR = 5
MP_METHODS = ("pix", "card", "card_recurring")
GATEWAY_METHODS = (*MP_METHODS, "paypal")
GATEWAY_ERRORS = (MercadoPagoError, PayPalError)


def mp_client(settings: Settings) -> MercadoPago:
    return MercadoPago(settings.get("mp_access_token", ""))


def paypal_client(settings: Settings) -> PayPal:
    return PayPal(
        settings.get("paypal_client_id", ""), settings.get("paypal_client_secret", ""), settings.flag("paypal_sandbox")
    )


def paypal_webhook_url(webhook_key: str) -> str | None:
    if not config.PUBLIC_URL.startswith("https://") or not webhook_key:
        return None
    return f"{config.PUBLIC_URL}/webhooks/paypal/{webhook_key}"


def webhook_url(webhook_key: str) -> str | None:
    """URL do webhook de um usuário: a chave identifica de quem é a notificação."""
    if not config.PUBLIC_URL.startswith("https://") or not webhook_key:
        return None
    return f"{config.PUBLIC_URL}/webhooks/mercadopago/{webhook_key}"


def notification_url(settings: Settings) -> str | None:
    return webhook_url(settings.webhook_key)


def back_url(settings: Settings) -> str:
    username = runtime.usernames.get(settings.tenant_id)
    if username:
        return f"https://t.me/{username}"
    if config.PUBLIC_URL.startswith("https://"):
        return f"{config.PUBLIC_URL}/obrigado"
    return "https://www.mercadopago.com.br"


def _new_payment(member: Member, plan: Plan, method: str) -> Payment:
    return Payment(
        tenant_id=member.tenant_id,
        member_id=member.id,
        plan_code=plan.code,
        amount_cents=plan.price_cents,
        method=method,
        status="pending",
        external_ref=uuid.uuid4().hex,
    )


# --------------------------------------------------------------------- criação


async def charge_limit_reason(session: AsyncSession, member: Member) -> str | None:
    """Motivo para recusar uma nova cobrança no gateway, ou None se pode gerar."""
    since = utcnow() - timedelta(hours=1)
    recent = (
        await session.scalars(
            select(Payment.created_at).where(
                Payment.tenant_id == member.tenant_id,
                Payment.member_id == member.id,
                Payment.method.in_(GATEWAY_METHODS),
                Payment.created_at >= since,
            )
        )
    ).all()
    if any(utcnow() - created < CHARGE_COOLDOWN for created in recent):
        return "Aguarde alguns segundos antes de gerar outra cobrança."
    if len(recent) >= CHARGES_PER_HOUR:
        return "Você gerou muitas cobranças na última hora. Use uma das que já recebeu ou tente mais tarde."
    return None


async def reusable_pix(session: AsyncSession, member: Member, plan: Plan) -> Payment | None:
    """Pix pendente do mesmo plano ainda válido (o QR expira em 30 min)."""
    return await session.scalar(
        select(Payment)
        .where(
            Payment.tenant_id == member.tenant_id,
            Payment.member_id == member.id,
            Payment.plan_code == plan.code,
            Payment.method == "pix",
            Payment.status == "pending",
            Payment.pix_code.is_not(None),
            Payment.created_at >= utcnow() - timedelta(minutes=20),
        )
        .order_by(Payment.created_at.desc())
    )


async def start_pix(session: AsyncSession, settings: Settings, member: Member, plan: Plan) -> tuple[Payment, str | None]:
    payment = _new_payment(member, plan, "pix")
    session.add(payment)
    await session.flush()
    data = await mp_client(settings).create_pix(
        amount_cents=plan.price_cents,
        description=f"Assinatura {plan.name}",
        payer_email=member.email or "",
        external_reference=payment.external_ref,
        notification_url=notification_url(settings),
    )
    tx = (data.get("point_of_interaction") or {}).get("transaction_data") or {}
    payment.mp_id = str(data["id"])
    payment.pix_code = tx.get("qr_code")
    await session.commit()
    return payment, tx.get("qr_code_base64")


async def start_card(session: AsyncSession, settings: Settings, member: Member, plan: Plan) -> Payment:
    payment = _new_payment(member, plan, "card")
    session.add(payment)
    await session.flush()
    data = await mp_client(settings).create_preference(
        title=f"Assinatura {plan.name}",
        amount_cents=plan.price_cents,
        payer_email=member.email or "",
        external_reference=payment.external_ref,
        notification_url=notification_url(settings),
        back_url=back_url(settings),
    )
    payment.checkout_url = data["init_point"]
    await session.commit()
    return payment


async def start_paypal(session: AsyncSession, settings: Settings, member: Member, plan: Plan) -> Payment:
    payment = _new_payment(member, plan, "paypal")
    payment.amount_cents = plan.foreign_price_cents
    payment.currency = settings.get("paypal_currency", "USD")
    session.add(payment)
    await session.flush()
    order = await paypal_client(settings).create_order(
        amount_cents=payment.amount_cents,
        currency=payment.currency,
        description=f"Assinatura {plan.name}",
        reference=payment.external_ref,
        return_url=back_url(settings),
    )
    link = paypal.approval_link(order)
    if not link:
        raise PayPalError("PayPal não devolveu o link de aprovação.")
    payment.mp_id = str(order["id"])
    payment.checkout_url = link
    await session.commit()
    return payment


async def start_recurring(session: AsyncSession, settings: Settings, member: Member, plan: Plan) -> Payment:
    payment = _new_payment(member, plan, "card_recurring")
    session.add(payment)
    await session.flush()
    data = await mp_client(settings).create_preapproval(
        reason=f"Assinatura {plan.name}",
        amount_cents=plan.price_cents,
        payer_email=member.email or "",
        external_reference=payment.external_ref,
        back_url=back_url(settings),
    )
    payment.mp_id = str(data["id"])
    payment.checkout_url = data["init_point"]
    member.mp_preapproval_id = str(data["id"])
    member.recurring_status = data.get("status", "pending")
    await session.commit()
    return payment


async def start_manual_pix(session: AsyncSession, member: Member, plan: Plan, settings: Settings | None = None) -> Payment:
    """Pix na chave do dono. Com o QR Code ligado, grava também o copia e cola com o valor do plano."""
    payment = _new_payment(member, plan, "pix_manual")
    session.add(payment)
    await session.flush()
    if settings is not None and settings.flag("pix_manual_qr") and not brcode.missing_fields(settings):
        payment.pix_code = brcode.build(
            settings.get("pix_key", ""),
            settings.get("pix_holder", ""),
            settings.get("pix_city", ""),
            plan.price_cents,
            txid=f"VIP{payment.id}",  # aparece no extrato e ajuda a conferir o comprovante
        )
    await session.commit()
    return payment


# ----------------------------------------------------------------- conciliação


async def approve(session: AsyncSession, payment: Payment, paid_at: datetime | None = None) -> bool:
    """Aprova e libera o acesso. Idempotente: retorna False se já estava aprovado.

    Webhook, agendador e botões podem aprovar o mesmo pagamento ao mesmo tempo:
    o UPDATE condicional garante que só um deles libera os dias.
    """
    if payment.status == "approved":
        return False
    paid_at = paid_at or utcnow()
    claimed = await session.execute(
        update(Payment)
        .where(Payment.id == payment.id, Payment.status != "approved")
        .values(status="approved", paid_at=paid_at)
        .execution_options(synchronize_session=False)
    )
    if claimed.rowcount == 0:
        await session.commit()
        await session.refresh(payment)
        return False
    payment.status = "approved"
    payment.paid_at = paid_at
    # Trava o membro: dois pagamentos dele aprovados juntos não podem ler o mesmo vencimento.
    member = await session.get(Member, payment.member_id, with_for_update=True, populate_existing=True)
    plan = await get_plan(session, payment.tenant_id, payment.plan_code)
    if plan is not None:
        membership.extend(session, member, plan)
    else:
        membership.add_days(session, member, 30, detail=payment.plan_code)
    await session.commit()
    await notifier.payment_approved(session, payment, member, plan)
    await session.commit()  # in_group pode mudar no envio
    return True


async def reject(session: AsyncSession, payment: Payment, notify: bool = True) -> None:
    if payment.status != "pending":
        return
    payment.status = "rejected"
    await session.commit()
    if notify:
        member = await session.get(Member, payment.member_id)
        await notifier.payment_rejected(session, payment, member)


async def _apply_mp_payment(session: AsyncSession, payment: Payment, mp: dict) -> None:
    status = MP_STATUS.get(mp.get("status", ""), "pending")
    if not payment.mp_id:
        payment.mp_id = str(mp.get("id"))
    if status == "approved":
        await approve(session, payment, _parse_mp_date(mp.get("date_approved")))
    elif status == "rejected" and payment.method == "pix":
        await reject(session, payment)
    elif status == "rejected":
        # No cartão o cliente pode tentar de novo no mesmo link; só avisa.
        member = await session.get(Member, payment.member_id)
        await notifier.payment_rejected(session, payment, member)


async def sync_paypal(session: AsyncSession, settings: Settings, payment: Payment) -> None:
    """Captura o pedido aprovado e só libera com a captura concluída, no valor e na moeda cobrados."""
    client = paypal_client(settings)
    order = await client.get_order(payment.mp_id)
    if order.get("status") == "APPROVED":
        order = await client.capture_order(payment.mp_id)
    capture = paypal.first_capture(order)
    if capture is None:
        if order.get("status") == "VOIDED":
            await reject(session, payment)
        return
    status = capture.get("status")
    if status == "COMPLETED":
        amount = capture.get("amount") or {}
        expected = (payment.amount_cents, payment.currency)
        if (paypal.to_cents(amount.get("value")), amount.get("currency_code")) != expected:
            log.error("PayPal: captura do pagamento %s diverge do valor cobrado: %s", payment.id, amount)
            return
        await approve(session, payment, _parse_mp_date(capture.get("create_time")))
    elif status in ("DECLINED", "FAILED"):
        await reject(session, payment)
    else:
        # Ex.: PENDING, quando a conta PayPal ainda não aceitou a moeda ou o pagamento está em análise.
        log.info("PayPal: captura do pagamento %s com status %s", payment.id, status)


async def handle_paypal_webhook(session: AsyncSession, tenant_id: int, order_id: str) -> None:
    """A notificação só diz qual pedido olhar: o status vem da API, com as credenciais do próprio usuário."""
    payment = await session.scalar(
        select(Payment).where(
            Payment.tenant_id == tenant_id,
            Payment.method == "paypal",
            Payment.mp_id == order_id,
            Payment.status.in_(("pending", "expired")),  # captura em análise pode concluir depois do prazo
        )
    )
    if payment is not None:
        await sync_paypal(session, await get_settings(session, tenant_id), payment)


async def refresh_payment(session: AsyncSession, settings: Settings, payment: Payment) -> None:
    """Consulta o gateway (usado pelo agendador e pelo botão "Já paguei")."""
    if payment.status != "pending":
        return
    if payment.method == "paypal":
        await sync_paypal(session, settings, payment)
        return
    client = mp_client(settings)
    if payment.method == "pix" and payment.mp_id:
        await _apply_mp_payment(session, payment, await client.get_payment(payment.mp_id))
    elif payment.method == "card":
        results = await client.search_payments(payment.external_ref)
        approved = [r for r in results if r.get("status") == "approved"]
        if approved:
            await _apply_mp_payment(session, payment, approved[0])
    elif payment.method == "card_recurring":
        member = await session.get(Member, payment.member_id)
        if member and member.mp_preapproval_id:
            await sync_recurring(session, settings, member)


async def sync_recurring(session: AsyncSession, settings: Settings, member: Member) -> None:
    """Lê a assinatura no Mercado Pago e registra cobranças novas."""
    if not member.mp_preapproval_id:
        return
    pre = await mp_client(settings).get_preapproval(member.mp_preapproval_id)
    # Serializa sincronizações do mesmo membro e relê a última cobrança já registrada.
    await session.refresh(member, with_for_update=True)
    status = pre.get("status")
    if status == "cancelled" and member.recurring_status != "cancelled":
        session.add(
            Event(tenant_id=member.tenant_id, member_id=member.id, kind="recurring_canceled", detail=member.mp_preapproval_id)
        )
    member.recurring_status = status

    summary = pre.get("summarized") or {}
    last_charge = _parse_mp_date(summary.get("last_charged_date"))
    if last_charge and (
        member.last_recurring_charge_at is None or last_charge > member.last_recurring_charge_at + timedelta(minutes=1)
    ):
        member.last_recurring_charge_at = last_charge
        payment = await session.scalar(
            select(Payment)
            .where(
                Payment.tenant_id == member.tenant_id,
                Payment.member_id == member.id,
                Payment.method == "card_recurring",
                Payment.status == "pending",
            )
            .order_by(Payment.created_at.desc())
        )
        # O plano recorrente do membro; sem ele, o primeiro recorrente da área.
        plan = await get_plan(session, member.tenant_id, member.plan_code or "")
        if plan is None or not plan.recurring:
            plan = await session.scalar(
                select(Plan).where(Plan.tenant_id == member.tenant_id, Plan.recurring.is_(True)).order_by(Plan.sort_order)
            )
        if payment is None:
            payment = Payment(
                tenant_id=member.tenant_id,
                member_id=member.id,
                plan_code=plan.code if plan else "mensal_recorrente",
                amount_cents=plan.price_cents if plan else 0,
                method="card_recurring",
                status="pending",
                # Determinístico: a mesma cobrança nunca vira dois pagamentos (unique).
                external_ref=f"rec-{member.mp_preapproval_id}-{last_charge:%Y%m%d%H%M}"[:64],
                mp_id=member.mp_preapproval_id,
            )
            session.add(payment)
            await session.flush()
        amount = summary.get("last_charged_amount")
        if amount:
            payment.amount_cents = round(float(amount) * 100)
        await approve(session, payment, last_charge)
    else:
        await session.commit()


async def cancel_recurring(session: AsyncSession, settings: Settings, member: Member) -> None:
    if not member.mp_preapproval_id:
        return
    await mp_client(settings).cancel_preapproval(member.mp_preapproval_id)
    if member.recurring_status != "cancelled":
        session.add(
            Event(tenant_id=member.tenant_id, member_id=member.id, kind="recurring_canceled", detail=member.mp_preapproval_id)
        )
    member.recurring_status = "cancelled"
    await session.commit()


async def handle_webhook(session: AsyncSession, tenant_id: int, topic: str, data_id: str) -> None:
    """Consulta o Mercado Pago com o token do próprio usuário e só mexe nos dados dele."""
    settings = await get_settings(session, tenant_id)
    client = mp_client(settings)
    if topic == "payment":
        mp = await client.get_payment(data_id)
        payment = await session.scalar(
            select(Payment).where(
                Payment.tenant_id == tenant_id, Payment.external_ref == (mp.get("external_reference") or "")
            )
        )
        if payment is not None:
            if payment.method == "card_recurring":
                member = await session.get(Member, payment.member_id)
                await sync_recurring(session, settings, member)
            elif payment.status == "pending":
                await _apply_mp_payment(session, payment, mp)
            return
        preapproval_id = (mp.get("metadata") or {}).get("preapproval_id") or (
            ((mp.get("point_of_interaction") or {}).get("transaction_data") or {}).get("subscription_id")
        )
        if preapproval_id:
            await _sync_by_preapproval(session, settings, str(preapproval_id))
    elif topic in ("subscription_preapproval", "preapproval"):
        await _sync_by_preapproval(session, settings, data_id)
    elif topic in ("subscription_authorized_payment", "authorized_payment"):
        ap = await client.get_authorized_payment(data_id)
        if ap.get("preapproval_id"):
            await _sync_by_preapproval(session, settings, str(ap["preapproval_id"]))


async def _sync_by_preapproval(session: AsyncSession, settings: Settings, preapproval_id: str) -> None:
    member = await session.scalar(
        select(Member).where(Member.tenant_id == settings.tenant_id, Member.mp_preapproval_id == preapproval_id)
    )
    if member is None:
        log.info("Assinatura %s sem membro associado", preapproval_id)
        return
    await sync_recurring(session, settings, member)


async def check_pending(session: AsyncSession, tenant_id: int, recent_only: bool = True) -> None:
    """Fallback do webhook: funciona mesmo sem domínio público.

    recent_only consulta só cobranças das últimas 2 h (rodada rápida);
    a rodada completa roda com menos frequência.
    """
    settings = await get_settings(session, tenant_id)
    now = utcnow()
    pending = (
        await session.scalars(
            select(Payment).where(
                Payment.tenant_id == tenant_id, Payment.status == "pending", Payment.method.in_(GATEWAY_METHODS)
            )
        )
    ).all()
    for payment in pending:
        age = now - payment.created_at
        if age > PENDING_TTL:
            payment.status = "expired"
            await session.commit()
            continue
        if recent_only and age > timedelta(hours=2):
            continue
        has_credentials = (
            settings.get("paypal_client_id") and settings.get("paypal_client_secret")
            if payment.method == "paypal"
            else settings.get("mp_access_token")
        )
        if not has_credentials:
            continue
        try:
            await refresh_payment(session, settings, payment)
        except GATEWAY_ERRORS as exc:
            log.warning("Falha ao consultar pagamento %s: %s", payment.id, exc)


def _parse_mp_date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(UTC).replace(tzinfo=None)
    return dt
