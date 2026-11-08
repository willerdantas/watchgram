from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import config
from app.models import MessageTemplate, Plan, PlatformSetting, Setting, User
from app.security import PREFIX, encrypt, fingerprint
from app.services.defaults import DEFAULT_MESSAGES, DEFAULT_PLANS, DEFAULT_SETTINGS, SECRET_SETTINGS
from app.services.passwords import hash_password

# Padrões antigos: se o admin nunca editou, troca pelo padrão atual.
LEGACY_MESSAGES = {
    "welcome": "Olá, {nome}! 👋\n\nBem-vindo(a) ao nosso grupo VIP. Escolha um plano abaixo:",
    "status_none": "Você não tem uma assinatura ativa. Use /start para assinar.",
    "payment_rejected": "❌ Não conseguimos confirmar seu pagamento. Tente novamente com /start ou fale com o suporte: {suporte}",
    "join_denied": "Seu pedido para entrar no grupo foi recusado porque não encontramos uma assinatura ativa. "
    "Use /start para assinar.",
}


async def ensure_superadmin(session: AsyncSession) -> None:
    """Primeira execução: cria o superadmin com ADMIN_USER/ADMIN_PASSWORD do .env."""
    if await session.scalar(select(User.id).limit(1)) is not None:
        return
    if not config.ADMIN_PASSWORD:
        raise RuntimeError("Defina ADMIN_PASSWORD no .env para criar o primeiro usuário (superadmin).")
    session.add(
        User(username=config.ADMIN_USER.strip().lower(), password_hash=hash_password(config.ADMIN_PASSWORD), is_superadmin=True)
    )
    await session.commit()


async def seed_tenant(session: AsyncSession, tenant_id: int) -> None:
    """Cria o que estiver faltando na área do usuário, sem sobrescrever o que ele editou."""
    existing = set((await session.scalars(select(Setting.key).where(Setting.tenant_id == tenant_id))).all())
    for key, value in DEFAULT_SETTINGS.items():
        if key not in existing:
            session.add(Setting(tenant_id=tenant_id, key=key, value=value))

    # Planos padrão só numa área nova: os que o usuário excluiu não voltam.
    if not await session.scalar(select(Plan.code).where(Plan.tenant_id == tenant_id).limit(1)):
        for plan in DEFAULT_PLANS:
            session.add(Plan(tenant_id=tenant_id, **plan))

    messages = {
        m.key: m
        for m in (await session.scalars(select(MessageTemplate).where(MessageTemplate.tenant_id == tenant_id))).all()
    }
    for key, (_title, text, _vars) in DEFAULT_MESSAGES.items():
        if key not in messages:
            session.add(MessageTemplate(tenant_id=tenant_id, key=key, text=text))
        elif messages[key].text == LEGACY_MESSAGES.get(key):
            messages[key].text = text

    await session.commit()


async def encrypt_stored_secrets(session: AsyncSession) -> None:
    """Migração: segredos gravados em claro por versões anteriores passam a ser criptografados."""
    from app.services.platform import PLATFORM_SECRETS

    rows = (await session.scalars(select(Setting).where(Setting.key.in_(SECRET_SETTINGS)))).all()
    for row in rows:
        if row.value and not row.value.startswith(PREFIX):
            if row.key == "bot_token":
                fp = await session.get(Setting, (row.tenant_id, "bot_token_fp"))
                if fp is None:
                    session.add(Setting(tenant_id=row.tenant_id, key="bot_token_fp", value=fingerprint(row.value)))
                else:
                    fp.value = fingerprint(row.value)
            row.value = encrypt(row.value)
    for row in (await session.scalars(select(PlatformSetting).where(PlatformSetting.key.in_(PLATFORM_SECRETS)))).all():
        if row.value and not row.value.startswith(PREFIX):
            row.value = encrypt(row.value)
    await session.commit()


async def seed_defaults(session: AsyncSession) -> None:
    from app.services.platform import seed_platform

    await ensure_superadmin(session)
    await encrypt_stored_secrets(session)
    await seed_platform(session)
    for tenant_id in (await session.scalars(select(User.id))).all():
        await seed_tenant(session, tenant_id)
