"""Leitura e escrita de configurações, planos e mensagens de um usuário (tenant)."""

import html
import json

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import CustomMessage, MessageTemplate, Plan, Setting, User
from app.security import decrypt, encrypt, fingerprint
from app.services.defaults import DEFAULT_MESSAGES, DEFAULT_SETTINGS, SECRET_SETTINGS
from app.utils import matches_keyword, parse_admin_ids, parse_id_list, parse_int_list, parse_keywords, render, time_greeting


class Settings(dict):
    """dict de configurações de um tenant, com acessores tipados."""

    tenant_id: int = 0
    webhook_key: str = ""

    def flag(self, key: str) -> bool:
        return self.get(key, "0") == "1"

    def as_int(self, key: str, default: int = 0) -> int:
        try:
            return int(self.get(key) or default)
        except ValueError:
            return default

    @property
    def admin_ids(self) -> list[int]:
        return parse_admin_ids(self.get("admin_ids", ""))

    @property
    def group_chat_id(self) -> int | None:
        ids = parse_id_list(self.get("group_chat_id", ""))
        return ids[0] if ids else None

    @property
    def reminder_days(self) -> list[int]:
        return parse_int_list(self.get("reminder_days", ""))

    @property
    def detected_chats(self) -> list[dict]:
        try:
            return json.loads(self.get("detected_chats") or "[]")
        except json.JSONDecodeError:
            return []


async def get_settings(session: AsyncSession, tenant_id: int) -> Settings:
    rows = (await session.scalars(select(Setting).where(Setting.tenant_id == tenant_id))).all()
    data = dict(DEFAULT_SETTINGS)
    data.update({row.key: decrypt(row.value) if row.key in SECRET_SETTINGS else row.value for row in rows})
    settings = Settings(data)
    settings.tenant_id = tenant_id
    settings.webhook_key = await session.scalar(select(User.webhook_key).where(User.id == tenant_id)) or ""
    return settings


async def set_settings(session: AsyncSession, tenant_id: int, values: dict[str, str]) -> None:
    values = dict(values)
    if "bot_token" in values:  # permite achar o mesmo bot em outra conta sem ler tokens em claro
        values["bot_token_fp"] = fingerprint(values["bot_token"])
    for key in SECRET_SETTINGS & values.keys():
        values[key] = encrypt(values[key])
    for key, value in values.items():
        row = await session.get(Setting, (tenant_id, key))
        if row is None:
            session.add(Setting(tenant_id=tenant_id, key=key, value=value))
        else:
            row.value = value
    await session.commit()


async def get_plans(session: AsyncSession, tenant_id: int, only_active: bool = False) -> list[Plan]:
    stmt = select(Plan).where(Plan.tenant_id == tenant_id).order_by(Plan.sort_order)
    if only_active:
        stmt = stmt.where(Plan.active.is_(True))
    return list((await session.scalars(stmt)).all())


async def get_plan(session: AsyncSession, tenant_id: int, code: str | None) -> Plan | None:
    return await session.get(Plan, (tenant_id, code)) if code else None


async def get_message(session: AsyncSession, tenant_id: int, key: str, **values) -> str:
    row = await session.get(MessageTemplate, (tenant_id, key))
    text = row.text if row else DEFAULT_MESSAGES.get(key, ("", key, ""))[1]
    # O texto do admin é HTML do Telegram; os valores (nome do usuário etc.) não.
    return render(text, **{k: html.escape(str(v), quote=False) for k, v in values.items()})


def render_custom(row: CustomMessage, settings: Settings, name: str) -> str:
    values = {"nome": name, "saudacao": time_greeting(), "suporte": settings.get("support_contact") or "-"}
    return render(row.text, **{k: html.escape(str(v), quote=False) for k, v in values.items()})


async def find_custom_message(session: AsyncSession, tenant_id: int, text: str) -> CustomMessage | None:
    """Primeira mensagem personalizada ativa cuja palavra-gatilho casa com o texto."""
    rows = await session.scalars(
        select(CustomMessage)
        .where(CustomMessage.tenant_id == tenant_id, CustomMessage.active.is_(True))
        .order_by(CustomMessage.id)
    )
    for row in rows:
        if matches_keyword(text, parse_keywords(row.triggers)):
            return row
    return None
