"""Login do painel e isolamento por usuário.

Toda rota da área do usuário recebe `user = Depends(current_user)` e filtra
os dados por user.id. Rotas de gestão de usuários exigem `require_superadmin`.
"""

import time

from fastapi import Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import SessionLocal, bind_tenant
from app.models import User
from app.services.passwords import hash_password, verify_password

MAX_ATTEMPTS = 5
# Por conta: segura tentativas espalhadas por muitos IPs contra o mesmo usuário.
MAX_ACCOUNT_ATTEMPTS = 10
LOCK_SECONDS = 300
MAX_TRACKED_IPS = 10_000
_failures: dict[str, list[float]] = {}
MAX_SIGNUPS = 5
SIGNUP_WINDOW = 3600
_signups: dict[str, list[float]] = {}
# Compara com um hash qualquer quando o usuário não existe: mesmo tempo de resposta.
_DUMMY_HASH = hash_password("senha-inexistente")


class LoginRequired(Exception):
    pass


async def login_required_handler(request: Request, exc: LoginRequired) -> RedirectResponse:
    return RedirectResponse("/login", status_code=303)


async def get_session():
    async with SessionLocal() as session:
        yield session


async def current_user(request: Request, session: AsyncSession = Depends(get_session)) -> User:
    user_id = request.session.get("uid")
    user = await session.get(User, user_id) if isinstance(user_id, int) else None
    if user is None or not user.active or request.session.get("ver") != user.session_version:
        request.session.clear()
        raise LoginRequired()
    await bind_tenant(session, user.id)  # no Postgres, o banco também esconde as outras áreas
    return user


async def require_superadmin(user: User = Depends(current_user)) -> User:
    if not user.is_superadmin:
        raise HTTPException(status_code=404)
    return user


def login_session(request: Request, user: User) -> None:
    request.session.clear()
    request.session["uid"] = user.id
    request.session["ver"] = user.session_version


def signup_blocked(ip: str) -> bool:
    """Limite de cadastros públicos por IP; registra a tentativa quando libera."""
    now = time.time()
    recent = [t for t in _signups.get(ip, []) if now - t < SIGNUP_WINDOW]
    if len(recent) >= MAX_SIGNUPS:
        _signups[ip] = recent
        return True
    if len(_signups) >= MAX_TRACKED_IPS:
        _signups.clear()
    _signups[ip] = [*recent, now]
    return False


def is_locked(ip: str, account: str | None = None) -> bool:
    now = time.time()
    recent = [t for t in _failures.get(ip, []) if now - t < LOCK_SECONDS]
    _failures[ip] = recent
    if len(recent) >= MAX_ATTEMPTS:
        return True
    if account:
        key = "conta:" + account.strip().lower()
        tries = [t for t in _failures.get(key, []) if now - t < LOCK_SECONDS]
        _failures[key] = tries
        return len(tries) >= MAX_ACCOUNT_ATTEMPTS
    return False


def register_failure(key: str) -> None:
    if len(_failures) >= MAX_TRACKED_IPS:
        _prune()
    _failures.setdefault(key, []).append(time.time())


async def authenticate(session: AsyncSession, ip: str, username: str, password: str) -> User | None:
    login = username.strip().lower()
    user = await session.scalar(select(User).where(User.username == login))
    if user is None and "@" in login:  # quem se cadastrou sozinho entra com o e-mail
        user = await session.scalar(select(User).where(func.lower(User.email) == login).order_by(User.id).limit(1))
    ok = verify_password(password, user.password_hash if user else _DUMMY_HASH) and user is not None and user.active
    if not ok:
        register_failure(ip)
        register_failure("conta:" + login)
        return None
    _failures.pop(ip, None)
    _failures.pop("conta:" + login, None)
    return user


def _prune() -> None:
    """Descarta IPs sem falhas recentes; se ainda estiver cheio, os mais antigos."""
    now = time.time()
    for ip in [ip for ip, times in _failures.items() if not times or now - times[-1] >= LOCK_SECONDS]:
        del _failures[ip]
    while len(_failures) >= MAX_TRACKED_IPS:
        del _failures[next(iter(_failures))]
