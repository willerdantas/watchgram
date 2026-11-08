"""Porteiro do grupo de cada usuário: aprova quem pagou e remove quem entrou sem assinatura."""

import json
import logging

from aiogram import Router
from aiogram.filters import IS_MEMBER, IS_NOT_MEMBER, JOIN_TRANSITION, LEAVE_TRANSITION, ChatMemberUpdatedFilter
from aiogram.types import ChatJoinRequest, ChatMemberUpdated

from app.db import SessionLocal
from app.services import membership, notifier, reports
from app.services.store import get_message, get_settings, set_settings

log = logging.getLogger(__name__)
router = Router()


@router.chat_join_request()
async def on_join_request(request: ChatJoinRequest, tenant_id: int) -> None:
    async with SessionLocal() as session:
        settings = await get_settings(session, tenant_id)
        if request.chat.id != settings.group_chat_id:
            return
        member = await membership.get_member_by_tg(session, tenant_id, request.from_user.id)
        if membership.is_active(member):
            await request.approve()
            changed = membership.set_in_group(session, member, True)
            await session.commit()
            if changed:
                await reports.alert_joined(session, settings, member)
            return
        await request.decline()
        await notifier.send(
            tenant_id,
            request.from_user.id,
            await get_message(session, tenant_id, "join_denied", nome=request.from_user.first_name),
            await notifier.plans_markup(session, tenant_id),
        )


@router.chat_member(ChatMemberUpdatedFilter(member_status_changed=JOIN_TRANSITION))
async def on_join(event: ChatMemberUpdated, tenant_id: int) -> None:
    user = event.new_chat_member.user
    async with SessionLocal() as session:
        settings = await get_settings(session, tenant_id)
        if event.chat.id != settings.group_chat_id or user.is_bot or user.id in settings.admin_ids:
            return
        member = await membership.get_member_by_tg(session, tenant_id, user.id)
        if membership.is_active(member):
            changed = membership.set_in_group(session, member, True)
            await session.commit()
            if changed:
                await reports.alert_joined(session, settings, member)
            return
        log.info("Removendo %s: entrou sem assinatura ativa", user.id)
        if member is None:
            member = await membership.get_or_create_member(session, tenant_id, user)
        await notifier.kick(session, settings, member)
        await session.commit()


@router.chat_member(ChatMemberUpdatedFilter(member_status_changed=LEAVE_TRANSITION))
async def on_leave(event: ChatMemberUpdated, tenant_id: int) -> None:
    async with SessionLocal() as session:
        settings = await get_settings(session, tenant_id)
        if event.chat.id != settings.group_chat_id:
            return
        member = await membership.get_member_by_tg(session, tenant_id, event.new_chat_member.user.id)
        if member is not None and membership.set_in_group(session, member, False):
            await session.commit()
            await reports.alert_left(session, settings, member)


@router.my_chat_member(ChatMemberUpdatedFilter(member_status_changed=IS_NOT_MEMBER >> IS_MEMBER))
@router.my_chat_member(ChatMemberUpdatedFilter(member_status_changed=IS_MEMBER >> IS_MEMBER))
async def on_bot_added(event: ChatMemberUpdated, tenant_id: int) -> None:
    """Registra grupos/canais onde o bot foi adicionado, para facilitar a configuração."""
    if event.chat.type == "private":
        return
    async with SessionLocal() as session:
        settings = await get_settings(session, tenant_id)
        chats = [c for c in settings.detected_chats if c.get("id") != event.chat.id]
        chats.insert(
            0,
            {
                "id": event.chat.id,
                "title": event.chat.title or "",
                "type": event.chat.type,
                "is_admin": event.new_chat_member.status == "administrator",
            },
        )
        await set_settings(session, tenant_id, {"detected_chats": json.dumps(chats[:10], ensure_ascii=False)})
