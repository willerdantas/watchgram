"""Ponto de entrada: painel web + bots de todos os usuários + agendador no mesmo processo.

    uvicorn app.main:app --host 0.0.0.0 --port 8000
"""

import asyncio
import contextlib
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from app import config, scheduler
from app.bot.runner import BotManager
from app.db import init_db
from app.security import security_middleware
from app.web import admin, insights, public, routes, support
from app.web.auth import LoginRequired, login_required_handler

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

if "troque" in config.SECRET_KEY or len(config.SECRET_KEY) < 32:
    raise RuntimeError(
        "SECRET_KEY precisa ser um valor aleatório próprio de 32+ caracteres (não o do exemplo). Gere com:\n"
        "  python -c \"import secrets; print(secrets.token_urlsafe(48))\""
    )
if not config.SECRET_KEY:
    raise RuntimeError("Defina SECRET_KEY no arquivo .env (veja .env.example).")


bots = BotManager()  # único: os routers do aiogram só podem ser anexados uma vez


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    app.state.bots = bots
    scheduler_task = None
    if not config.DISABLE_BACKGROUND:
        await bots.start_all()
        scheduler_task = asyncio.create_task(scheduler.run_forever(bots))
    yield
    if scheduler_task:
        scheduler_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await scheduler_task
    await bots.stop_all()


app = FastAPI(title="Bot de Assinaturas", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.middleware("http")(security_middleware)
app.add_middleware(
    SessionMiddleware,
    secret_key=config.SECRET_KEY,
    same_site="lax",
    max_age=60 * 60 * 12,
    https_only=config.COOKIE_SECURE,
    # __Host-: só vale no próprio domínio, por HTTPS e para todo o site (não pode ser plantado por subdomínio).
    session_cookie="__Host-sessao" if config.COOKIE_SECURE else "sessao",
)
app.add_exception_handler(LoginRequired, login_required_handler)
app.mount("/static", StaticFiles(directory=config.BASE_DIR / "app" / "web" / "static"), name="static")
app.include_router(routes.router)
app.include_router(admin.router)
app.include_router(support.router)
app.include_router(public.router)
app.include_router(insights.router)
