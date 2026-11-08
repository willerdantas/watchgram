"""Estado compartilhado em tempo de execução: os bots ativos de cada usuário (tenant)."""

from aiogram import Bot

bots: dict[int, Bot] = {}
usernames: dict[int, str] = {}
errors: dict[int, str] = {}


def get_bot(tenant_id: int) -> Bot | None:
    return bots.get(tenant_id)
