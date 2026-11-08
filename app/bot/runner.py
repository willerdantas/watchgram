"""Um bot por usuário (tenant), todos no mesmo processo.

Um único Dispatcher atende todos os bots. Cada bot tem o próprio loop de
getUpdates, e cada update chega aos handlers com o tenant_id do dono do bot,
então um handler nunca enxerga dados de outro usuário.
"""

import asyncio
import contextlib
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError, TelegramConflictError, TelegramUnauthorizedError
from aiogram.methods import GetUpdates
from aiogram.types import BotCommand
from aiogram.utils.token import TokenValidationError
from sqlalchemy import select

from app import runtime
from app.bot import handlers_admin, handlers_group, handlers_member
from app.db import SessionLocal
from app.models import User
from app.services.store import get_settings

log = logging.getLogger(__name__)

COMMANDS = [
    BotCommand(command="start", description="Ver planos e assinar"),
    BotCommand(command="status", description="Minha assinatura"),
    BotCommand(command="cancelar", description="Cancelar cobrança recorrente"),
]
POLLING_TIMEOUT = 25
SUBSCRIPTION_EXPIRED = "Assinatura da plataforma vencida: o bot está parado. Renove em Assinatura."


class BotManager:
    def __init__(self) -> None:
        self.dp = Dispatcher()
        self.dp.include_routers(handlers_admin.router, handlers_group.router, handlers_member.router)
        self._tasks: dict[int, asyncio.Task] = {}
        self._handling: set[asyncio.Task] = set()
        self._locks: dict[int, asyncio.Lock] = {}

    def _lock(self, tenant_id: int) -> asyncio.Lock:
        return self._locks.setdefault(tenant_id, asyncio.Lock())

    async def start_all(self) -> None:
        async with SessionLocal() as session:
            tenants = (await session.scalars(select(User.id).where(User.active.is_(True)))).all()
            tokens = {tid: (await get_settings(session, tid)).get("bot_token", "") for tid in tenants}
        for tenant_id, token in tokens.items():
            await self.start(tenant_id, token)

    async def start_tenant(self, tenant_id: int) -> None:
        async with SessionLocal() as session:
            token = (await get_settings(session, tenant_id)).get("bot_token", "")
        await self.start(tenant_id, token)

    async def start(self, tenant_id: int, token: str) -> None:
        async with self._lock(tenant_id):
            await self._stop(tenant_id)
            runtime.errors.pop(tenant_id, None)
            async with SessionLocal() as session:
                user = await session.get(User, tenant_id)
            if user is None or not user.active:
                return
            if not user.subscription_active:
                runtime.errors[tenant_id] = SUBSCRIPTION_EXPIRED
                return
            if not token:
                runtime.errors[tenant_id] = "Token do bot não configurado."
                return
            try:
                bot = Bot(token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
            except TokenValidationError:
                runtime.errors[tenant_id] = "Token inválido. Copie o token completo do @BotFather (formato 123456:ABC...)."
                return
            try:
                me = await bot.get_me()
                await bot.delete_webhook(drop_pending_updates=False)
                await bot.set_my_commands(COMMANDS)
            except TelegramAPIError as exc:
                runtime.errors[tenant_id] = f"Não foi possível conectar ao Telegram: {exc}"
                log.error("Usuário %s: %s", tenant_id, runtime.errors[tenant_id])
                await bot.session.close()
                return
            runtime.bots[tenant_id] = bot
            runtime.usernames[tenant_id] = me.username
            self._tasks[tenant_id] = asyncio.create_task(self._poll(tenant_id, bot), name=f"bot-{tenant_id}")
            log.info("Bot @%s iniciado (usuário %s)", me.username, tenant_id)

    async def stop(self, tenant_id: int, reason: str | None = None) -> None:
        async with self._lock(tenant_id):
            await self._stop(tenant_id)
            if reason:
                runtime.errors[tenant_id] = reason

    async def stop_all(self) -> None:
        for tenant_id in list(self._tasks):
            await self.stop(tenant_id)

    async def _stop(self, tenant_id: int) -> None:
        task = self._tasks.pop(tenant_id, None)
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        bot = runtime.bots.pop(tenant_id, None)
        runtime.usernames.pop(tenant_id, None)
        if bot is not None:
            await bot.session.close()

    async def _poll(self, tenant_id: int, bot: Bot) -> None:
        method = GetUpdates(timeout=POLLING_TIMEOUT, allowed_updates=self.dp.resolve_used_update_types())
        request_timeout = int((bot.session.timeout or 60) + POLLING_TIMEOUT)
        delay = 1.0
        while True:
            try:
                updates = await bot(method, request_timeout=request_timeout)
                delay = 1.0
            except asyncio.CancelledError:
                raise
            except TelegramUnauthorizedError:
                runtime.errors[tenant_id] = "O Telegram recusou o token (revogado?). Atualize em Configurações."
                log.error("Usuário %s: token recusado; polling parado", tenant_id)
                runtime.bots.pop(tenant_id, None)
                runtime.usernames.pop(tenant_id, None)
                return
            except TelegramConflictError:
                runtime.errors[tenant_id] = "Este bot está rodando em outro lugar (getUpdates em conflito)."
                log.warning("Usuário %s: conflito de polling", tenant_id)
                await asyncio.sleep(10)
                continue
            except Exception as exc:  # noqa: BLE001 - rede instável: tenta de novo com espera crescente
                log.warning("Usuário %s: falha no getUpdates: %s", tenant_id, exc)
                await asyncio.sleep(delay)
                delay = min(delay * 2, 30)
                continue
            runtime.errors.pop(tenant_id, None)
            for update in updates:
                method.offset = update.update_id + 1
                task = asyncio.create_task(self._handle(tenant_id, bot, update))
                self._handling.add(task)
                task.add_done_callback(self._handling.discard)

    async def _handle(self, tenant_id: int, bot: Bot, update) -> None:
        try:
            await self.dp.feed_update(bot, update, tenant_id=tenant_id)
        except Exception:  # noqa: BLE001 - um update com erro não derruba o polling
            log.exception("Erro ao processar update %s (usuário %s)", update.update_id, tenant_id)
