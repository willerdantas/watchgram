"""Páginas públicas: preços e cadastro pelo site (a página inicial fica em routes.dashboard)."""

import re
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from itsdangerous import BadSignature, TimestampSigner
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import config
from app.models import User
from app.services import platform
from app.services.passwords import hash_password
from app.services.seed import seed_tenant
from app.utils import EMAIL_RE
from app.web.admin import USERNAME_RE
from app.web.auth import get_session, login_session, signup_blocked
from app.web.routes import MIN_PASSWORD, back, flash, page

router = APIRouter()

# Robôs enviam o formulário na hora: exige um carimbo assinado, emitido há pelo menos alguns segundos.
_signer = TimestampSigner(config.SECRET_KEY, salt="cadastro")
MIN_FILL_SECONDS = 3
FORM_MAX_AGE = 24 * 3600


def _human_timing(stamp: str) -> bool:
    try:
        _, issued = _signer.unsign(stamp, max_age=FORM_MAX_AGE, return_timestamp=True)
    except BadSignature:
        return False
    return (datetime.now(UTC) - issued).total_seconds() >= MIN_FILL_SECONDS


async def _signup_open(session: AsyncSession) -> bool:
    return (await platform.get_platform_settings(session)).get("signup_enabled") == "1"


@router.get("/precos", response_class=HTMLResponse)
async def pricing(request: Request, session: AsyncSession = Depends(get_session)):
    settings = await platform.get_platform_settings(session)
    return page(
        request,
        None,
        "pricing.html",
        "public",
        plans=await platform.get_platform_plans(session, only_active=True),
        trial_days=settings.get("trial_days", "0"),
        signup_open=settings.get("signup_enabled") == "1",
    )


@router.get("/cadastro", response_class=HTMLResponse)
async def signup_form(request: Request, session: AsyncSession = Depends(get_session)):
    if "uid" in request.session:
        return back("/")
    if not await _signup_open(session):
        raise HTTPException(status_code=404)
    settings = await platform.get_platform_settings(session)
    return page(request, None, "signup.html", "public", min_password=MIN_PASSWORD,
                trial_days=settings.get("trial_days", "0"), form_stamp=_signer.sign("ok").decode())


async def _free_username(session: AsyncSession, email: str) -> str:
    base = re.sub(r"[^a-z0-9._-]", "", email.split("@", 1)[0].lower()).strip("._-")[:24]
    if not USERNAME_RE.match(base):
        base = "conta"
    candidate, n = base, 2
    while await session.scalar(select(User.id).where(User.username == candidate)):
        candidate, n = f"{base}{n}", n + 1
    return candidate


@router.post("/cadastro")
async def signup(
    request: Request,
    name: str = Form(""),
    email: str = Form(""),
    password: str = Form(""),
    website: str = Form(""),  # armadilha para robôs: pessoas não veem este campo
    stamp: str = Form(""),
    session: AsyncSession = Depends(get_session),
):
    if not await _signup_open(session):
        raise HTTPException(status_code=404)
    email = email.strip().lower()
    if website or not _human_timing(stamp):
        flash(request, "Não conseguimos confirmar o envio. Confira os dados e tente de novo.", "error")
        return back("/cadastro")
    if not EMAIL_RE.match(email):
        flash(request, "Informe um e-mail válido.", "error")
        return back("/cadastro")
    if len(password) < MIN_PASSWORD:
        flash(request, f"A senha precisa ter ao menos {MIN_PASSWORD} caracteres.", "error")
        return back("/cadastro")
    if await session.scalar(select(User.id).where(func.lower(User.email) == email).limit(1)):
        flash(request, "Já existe uma conta com este e-mail. Entre com ele e a sua senha.", "error")
        return back("/login")
    if signup_blocked(request.client.host if request.client else "?"):
        flash(request, "Muitos cadastros feitos daqui. Tente de novo mais tarde.", "error")
        return back("/cadastro")

    user = User(
        username=await _free_username(session, email),
        name=name.strip()[:128],
        email=email,
        password_hash=hash_password(password),
        subscription_until=await platform.trial_until(session),
    )
    session.add(user)
    await session.commit()
    await seed_tenant(session, user.id)
    login_session(request, user)
    flash(request, "Conta criada! Vamos deixar o seu bot pronto em 3 passos.")
    return back("/primeiros-passos")
