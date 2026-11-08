"""Assinatura da plataforma: cada usuário (exceto o superadmin) paga para manter o bot ativo.

Vencida a assinatura, só o bot para (BotManager não inicia e o agendador pausa
a área). O painel continua acessível para o usuário renovar.
Os pagamentos caem no Mercado Pago da plataforma, configurado pelo superadmin.
"""

import logging
import uuid
from datetime import datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

import html

from app import config
from app.models import PlatformPayment, PlatformPlan, PlatformSetting, User
from app.security import decrypt, encrypt
from app.services import mailer, membership, notifier
from app.services.mercadopago import MercadoPago, MercadoPagoError
from app.services.store import get_settings
from app.utils import fmt_date, parse_int_list
from app.services.payments import MP_STATUS, PENDING_TTL, _parse_mp_date
from app.utils import utcnow


log = logging.getLogger(__name__)

DEFAULT_PLATFORM_SETTINGS = {
    "mp_access_token": "",
    "mp_webhook_secret": "",
    "trial_days": "7",
    "signup_enabled": "1",
    "reminder_days": "7,3,1,0",
    "smtp_host": "",
    "smtp_port": "587",
    "smtp_user": "",
    "smtp_password": "",
    "smtp_from": "",
    "smtp_tls": "1",
}
PLATFORM_SECRETS = {"mp_access_token", "mp_webhook_secret", "smtp_password"}
DEFAULT_PLATFORM_PLANS = [
    {"code": "mensal", "name": "Mensal", "price_cents": 4990, "days": 30, "sort_order": 1},
    {"code": "trimestral", "name": "Trimestral", "price_cents": 13490, "days": 90, "sort_order": 2},
    {"code": "semestral", "name": "Semestral", "price_cents": 24990, "days": 180, "sort_order": 3},
]
PLATFORM_WEBHOOK_PATH = "/webhooks/mercadopago/plataforma"
# Cobrança pendente do mesmo plano é reaproveitada dentro desta janela (o Pix expira em 30 min).
REUSE_WINDOW = timedelta(minutes=20)


# ------------------------------------------------------------ configuração


async def seed_platform(session: AsyncSession) -> None:
    keys = set((await session.scalars(select(PlatformSetting.key))).all())
    for key, value in DEFAULT_PLATFORM_SETTINGS.items():
        if key not in keys:
            session.add(PlatformSetting(key=key, value=value))
    codes = set((await session.scalars(select(PlatformPlan.code))).all())
    for plan in DEFAULT_PLATFORM_PLANS:
        if plan["code"] not in codes:
            session.add(PlatformPlan(**plan))
    await session.commit()


async def get_platform_settings(session: AsyncSession) -> dict[str, str]:
    data = dict(DEFAULT_PLATFORM_SETTINGS)
    rows = (await session.scalars(select(PlatformSetting))).all()
    data.update({r.key: decrypt(r.value) if r.key in PLATFORM_SECRETS else r.value for r in rows})
    return data


async def set_platform_settings(session: AsyncSession, values: dict[str, str]) -> None:
    values = {key: encrypt(value) if key in PLATFORM_SECRETS else value for key, value in values.items()}
    for key, value in values.items():
        row = await session.get(PlatformSetting, key)
        if row is None:
            session.add(PlatformSetting(key=key, value=value))
        else:
            row.value = value
    await session.commit()


async def get_platform_plans(session: AsyncSession, only_active: bool = False) -> list[PlatformPlan]:
    stmt = select(PlatformPlan).order_by(PlatformPlan.sort_order)
    if only_active:
        stmt = stmt.where(PlatformPlan.active.is_(True))
    return list((await session.scalars(stmt)).all())


async def trial_until(session: AsyncSession) -> datetime | None:
    """Vencimento inicial de um usuário novo (teste grátis)."""
    days = (await get_platform_settings(session)).get("trial_days", "0")
    days = int(days) if days.isdigit() else 0
    return utcnow() + timedelta(days=days) if days else None


def platform_webhook_url() -> str | None:
    return f"{config.PUBLIC_URL}{PLATFORM_WEBHOOK_PATH}" if config.PUBLIC_URL.startswith("https://") else None


# ------------------------------------------------------------- cobranças


def _client(settings: dict[str, str]) -> MercadoPago:
    return MercadoPago(settings.get("mp_access_token", ""))


def _new_payment(user: User, plan: PlatformPlan, method: str) -> PlatformPayment:
    return PlatformPayment(
        user_id=user.id,
        plan_code=plan.code,
        days=plan.days,
        amount_cents=plan.price_cents,
        method=method,
        status="pending",
        external_ref="plat-" + uuid.uuid4().hex,
    )


async def start_pix(session: AsyncSession, user: User, plan: PlatformPlan) -> PlatformPayment:
    settings = await get_platform_settings(session)
    payment = _new_payment(user, plan, "pix")
    session.add(payment)
    await session.flush()
    data = await _client(settings).create_pix(
        amount_cents=plan.price_cents,
        description=f"Assinatura da plataforma - {plan.name}",
        payer_email=user.email or "",
        external_reference=payment.external_ref,
        notification_url=platform_webhook_url(),
    )
    tx = (data.get("point_of_interaction") or {}).get("transaction_data") or {}
    payment.mp_id = str(data["id"])
    payment.pix_code = tx.get("qr_code")
    payment.pix_qr_base64 = tx.get("qr_code_base64")
    await session.commit()
    return payment


async def start_card(session: AsyncSession, user: User, plan: PlatformPlan) -> PlatformPayment:
    settings = await get_platform_settings(session)
    payment = _new_payment(user, plan, "card")
    session.add(payment)
    await session.flush()
    back = f"{config.PUBLIC_URL}/assinatura" if config.PUBLIC_URL.startswith("https://") else None
    data = await _client(settings).create_preference(
        title=f"Assinatura da plataforma - {plan.name}",
        amount_cents=plan.price_cents,
        payer_email=user.email or "",
        external_reference=payment.external_ref,
        notification_url=platform_webhook_url(),
        back_url=back,
    )
    payment.checkout_url = data["init_point"]
    await session.commit()
    return payment


def _extend(user: User, days: int) -> None:
    base = user.subscription_until if user.subscription_until and user.subscription_until > utcnow() else utcnow()
    user.subscription_until = base + timedelta(days=days)
    user.platform_reminders_sent = ""  # novo vencimento: lembretes recomeçam


async def approve(session: AsyncSession, payment: PlatformPayment, paid_at: datetime | None = None) -> bool:
    """Aprova e estende a assinatura. Idempotente e seguro contra aprovação concorrente."""
    paid_at = paid_at or utcnow()
    claimed = await session.execute(
        update(PlatformPayment)
        .where(PlatformPayment.id == payment.id, PlatformPayment.status != "approved")
        .values(status="approved", paid_at=paid_at)
        .execution_options(synchronize_session=False)
    )
    if claimed.rowcount == 0:
        await session.commit()
        return False
    payment.status, payment.paid_at = "approved", paid_at
    user = await session.get(User, payment.user_id, with_for_update=True, populate_existing=True)
    _extend(user, payment.days)
    await session.commit()
    log.info("Assinatura de %s renovada até %s", user.username, user.subscription_until)
    return True


async def grant_days(session: AsyncSession, user: User, days: int) -> None:
    """Dias dados pelo superadmin (cortesia ou pagamento por fora)."""
    payment = PlatformPayment(
        user_id=user.id, plan_code="manual", days=days, amount_cents=0, method="manual",
        status="pending", external_ref="plat-manual-" + uuid.uuid4().hex,
    )
    session.add(payment)
    await session.flush()
    await approve(session, payment)


async def refresh(session: AsyncSession, payment: PlatformPayment) -> bool:
    """Consulta o Mercado Pago. Retorna True se o pagamento foi aprovado agora."""
    if payment.status != "pending":
        return False
    client = _client(await get_platform_settings(session))
    if payment.method == "pix" and payment.mp_id:
        mp = await client.get_payment(payment.mp_id)
    else:
        found = [r for r in await client.search_payments(payment.external_ref) if r.get("status") == "approved"]
        if not found:
            return False
        mp = found[0]
    status = MP_STATUS.get(mp.get("status", ""), "pending")
    if status == "approved":
        return await approve(session, payment, _parse_mp_date(mp.get("date_approved")))
    if status == "rejected" and payment.method == "pix":
        payment.status = "rejected"
        await session.commit()
    return False


async def handle_webhook(session: AsyncSession, topic: str, data_id: str) -> int | None:
    """Notificação do Mercado Pago da plataforma. Retorna o id do usuário renovado, se houver."""
    client = _client(await get_platform_settings(session))
    preapproval_id = None
    if topic == "payment":
        mp = await client.get_payment(data_id)
        payment = await session.scalar(
            select(PlatformPayment).where(PlatformPayment.external_ref == (mp.get("external_reference") or ""))
        )
        if payment is not None:
            if payment.status == "pending" and MP_STATUS.get(mp.get("status", "")) == "approved":
                if await approve(session, payment, _parse_mp_date(mp.get("date_approved"))):
                    return payment.user_id
            return None
        preapproval_id = (mp.get("metadata") or {}).get("preapproval_id") or (
            ((mp.get("point_of_interaction") or {}).get("transaction_data") or {}).get("subscription_id")
        )
    elif topic in ("subscription_preapproval", "preapproval"):
        preapproval_id = data_id
    elif topic in ("subscription_authorized_payment", "authorized_payment"):
        preapproval_id = (await client.get_authorized_payment(data_id)).get("preapproval_id")
    if not preapproval_id:
        return None
    user = await session.scalar(select(User).where(User.platform_preapproval_id == str(preapproval_id)))
    if user is not None and await sync_recurring(session, user):
        return user.id
    return None


async def check_pending(session: AsyncSession) -> list[int]:
    """Fallback do webhook (roda no agendador). Retorna os usuários renovados."""
    settings = await get_platform_settings(session)
    pending = (
        await session.scalars(
            select(PlatformPayment).where(PlatformPayment.status == "pending", PlatformPayment.method != "manual")
        )
    ).all()
    renewed = []
    for payment in pending:
        if utcnow() - payment.created_at > PENDING_TTL:
            payment.status = "expired"
            await session.commit()
            continue
        if not settings.get("mp_access_token"):
            continue
        try:
            if await refresh(session, payment):
                renewed.append(payment.user_id)
        except MercadoPagoError as exc:
            log.warning("Falha ao consultar pagamento da plataforma %s: %s", payment.id, exc)
    return renewed


async def expired_tenants(session: AsyncSession) -> list[int]:
    """Usuários ativos cuja assinatura venceu (o bot deve ficar parado)."""
    users = (await session.scalars(select(User).where(User.active.is_(True)))).all()
    return [u.id for u in users if not u.subscription_active]


# ------------------------------------------------- renovação automática (cartão)


def recurrence(days: int) -> tuple[int, str]:
    """Periodicidade da cobrança no Mercado Pago para um plano de N dias."""
    return (days // 30, "months") if days % 30 == 0 else (days, "days")


async def start_recurring(session: AsyncSession, user: User, plan: PlatformPlan) -> str:
    """Cria a assinatura no cartão e retorna o link para o usuário autorizar."""
    settings = await get_platform_settings(session)
    frequency, frequency_type = recurrence(plan.days)
    back = f"{config.PUBLIC_URL}/assinatura" if config.PUBLIC_URL.startswith("https://") else "https://www.mercadopago.com.br"
    data = await _client(settings).create_preapproval(
        reason=f"Assinatura da plataforma - {plan.name} (renovação automática)",
        amount_cents=plan.price_cents,
        payer_email=user.email or "",
        external_reference=f"plat-pre-{user.id}-{uuid.uuid4().hex[:12]}",
        back_url=back,
        frequency=frequency,
        frequency_type=frequency_type,
    )
    user.platform_preapproval_id = str(data["id"])
    user.platform_recurring_status = data.get("status", "pending")
    user.platform_recurring_plan = plan.code
    await session.commit()
    return data["init_point"]


async def sync_recurring(session: AsyncSession, user: User) -> bool:
    """Lê a assinatura no Mercado Pago e registra cobranças novas. True se renovou agora."""
    if not user.platform_preapproval_id:
        return False
    pre = await _client(await get_platform_settings(session)).get_preapproval(user.platform_preapproval_id)
    # Serializa sincronizações do mesmo usuário e relê a última cobrança já registrada.
    await session.refresh(user, with_for_update=True)
    user.platform_recurring_status = pre.get("status")
    summary = pre.get("summarized") or {}
    last_charge = _parse_mp_date(summary.get("last_charged_date"))
    if not last_charge or (
        user.platform_last_charge_at is not None and last_charge <= user.platform_last_charge_at + timedelta(minutes=1)
    ):
        await session.commit()
        return False
    user.platform_last_charge_at = last_charge
    plan = await session.get(PlatformPlan, user.platform_recurring_plan or "")
    amount = summary.get("last_charged_amount")
    payment = PlatformPayment(
        user_id=user.id,
        plan_code=plan.code if plan else (user.platform_recurring_plan or "recorrente"),
        days=plan.days if plan else 30,
        amount_cents=round(float(amount) * 100) if amount else (plan.price_cents if plan else 0),
        method="card_recurring",
        status="pending",
        # Determinístico: a mesma cobrança nunca vira dois pagamentos (unique).
        external_ref=f"plat-rec-{user.platform_preapproval_id}-{last_charge:%Y%m%d%H%M}"[:64],
        mp_id=user.platform_preapproval_id,
    )
    session.add(payment)
    await session.flush()
    return await approve(session, payment, last_charge)


async def cancel_recurring(session: AsyncSession, user: User) -> None:
    if not user.platform_preapproval_id:
        return
    await _client(await get_platform_settings(session)).cancel_preapproval(user.platform_preapproval_id)
    user.platform_recurring_status = "cancelled"
    await session.commit()


async def sync_all_recurring(session: AsyncSession) -> list[int]:
    settings = await get_platform_settings(session)
    if not settings.get("mp_access_token"):
        return []
    users = (
        await session.scalars(
            select(User).where(User.platform_preapproval_id.is_not(None), User.platform_recurring_status == "authorized")
        )
    ).all()
    renewed = []
    for user in users:
        try:
            if await sync_recurring(session, user):
                renewed.append(user.id)
        except MercadoPagoError as exc:
            log.warning("Falha ao sincronizar recorrência da plataforma (%s): %s", user.username, exc)
    return renewed


# ------------------------------------------------------------- lembretes


def _renew_hint() -> str:
    if config.PUBLIC_URL.startswith("https://"):
        return f"Renove em: {config.PUBLIC_URL}/assinatura"
    return "Renove no painel, na página Assinatura."


async def notify_user(session: AsyncSession, user: User, subject: str, text: str) -> bool:
    """Avisa o usuário por e-mail e pelo Telegram (o próprio bot, para os ids de admin dele)."""
    settings = await get_platform_settings(session)
    sent = await mailer.send_email(settings, user.email or "", subject, text)
    tenant = await get_settings(session, user.id)
    telegram_text = f"<b>{html.escape(subject)}</b>\n\n{html.escape(text)}"
    for admin_id in tenant.admin_ids:
        sent = await notifier.send(user.id, admin_id, telegram_text) or sent
    return sent


def _reminder_text(user: User, offset: int) -> tuple[str, str]:
    when = "hoje" if offset == 0 else f"em {offset} dia(s)"
    subject = f"Sua assinatura vence {when}"
    text = (
        f"Olá, {user.area_name}!\n\n"
        f"A assinatura da sua área vence {when} ({fmt_date(user.subscription_until)}). "
        "Quando vencer, o seu bot para de funcionar; o painel e os seus dados continuam disponíveis.\n\n"
        f"{_renew_hint()}"
    )
    return subject, text


async def send_reminders(session: AsyncSession) -> None:
    """Lembretes antes do vencimento (quem tem renovação automática não recebe)."""
    reminder_days = parse_int_list((await get_platform_settings(session)).get("reminder_days", ""))
    users = (
        await session.scalars(
            select(User).where(
                User.active.is_(True), User.is_superadmin.is_(False), User.subscription_until.is_not(None)
            )
        )
    ).all()
    for user in users:
        if user.exempt or user.recurring_active:
            continue
        offset = membership.due_offset(user.subscription_until, user.platform_reminders_sent or "", reminder_days)
        if offset is None:
            continue
        subject, text = _reminder_text(user, offset)
        await notify_user(session, user, subject, text)
        user.platform_reminders_sent = membership.mark_offsets(user.platform_reminders_sent or "", offset, reminder_days)
        await session.commit()


async def notify_expired(session: AsyncSession, user: User) -> None:
    """Aviso único de assinatura vencida (enviado antes de o bot parar)."""
    sent = (user.platform_reminders_sent or "").split(",")
    if "x" in sent:
        return
    text = (
        f"Olá, {user.area_name}!\n\n"
        f"A assinatura da sua área venceu em {fmt_date(user.subscription_until)} e o seu bot foi parado. "
        "O painel e os seus dados continuam disponíveis, e o bot volta assim que você renovar.\n\n"
        f"{_renew_hint()}"
    )
    await notify_user(session, user, "Sua assinatura venceu: o bot foi parado", text)
    user.platform_reminders_sent = ",".join([x for x in sent if x] + ["x"])
    await session.commit()
