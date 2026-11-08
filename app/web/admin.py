"""Gestão de usuários (só superadmin).

O superadmin cria, ativa/desativa e redefine a senha das contas, mas não tem
acesso aos dados das áreas: esta página mostra só dados da conta.
"""

import re

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import config, runtime
from app.bot.runner import SUBSCRIPTION_EXPIRED
from app.db import SessionLocal
from app.models import User
from app.services import platform
from app.services.passwords import generate_password, hash_password
from app.services.seed import seed_tenant
from app.web.auth import get_session, require_superadmin
from app.services import mailer
from app.utils import EMAIL_RE, parse_brl, parse_int_list
from app.web.routes import MAX_DAYS, MIN_PASSWORD, back, flash, page

router = APIRouter(prefix="/admin")

USERNAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{2,31}$")


async def _other_user(session: AsyncSession, admin: User, user_id: int) -> User:
    user = await session.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404)
    if user.id == admin.id:
        raise HTTPException(status_code=400, detail="Use Minha conta para alterar a sua própria conta.")
    return user


@router.get("/usuarios", response_class=HTMLResponse)
async def users_list(
    request: Request, admin: User = Depends(require_superadmin), session: AsyncSession = Depends(get_session)
):
    users = (await session.scalars(select(User).order_by(User.created_at))).all()
    return page(
        request,
        admin,
        "admin_users.html",
        "admin",
        users=users,
        usernames=runtime.usernames,
        errors=runtime.errors,
        min_password=MIN_PASSWORD,
    )


@router.post("/usuarios")
async def user_create(
    request: Request,
    username: str = Form(...),
    name: str = Form(""),
    password: str = Form(""),
    admin: User = Depends(require_superadmin),
    session: AsyncSession = Depends(get_session),
):
    username = username.strip().lower()
    if not USERNAME_RE.match(username):
        flash(request, "Usuário inválido: 3 a 32 caracteres, letras minúsculas, números, ponto, hífen ou _.", "error")
        return back("/admin/usuarios")
    if await session.scalar(select(User.id).where(User.username == username)):
        flash(request, f"O usuário {username} já existe.", "error")
        return back("/admin/usuarios")
    generated = not password
    password = password or generate_password()
    if len(password) < MIN_PASSWORD:
        flash(request, f"A senha precisa ter ao menos {MIN_PASSWORD} caracteres.", "error")
        return back("/admin/usuarios")

    user = User(
        username=username,
        name=name.strip()[:128],
        password_hash=hash_password(password),
        subscription_until=await platform.trial_until(session),  # teste grátis
    )
    session.add(user)
    await session.commit()
    async with SessionLocal() as tenant_session:  # sessão sem área ligada: grava na área nova
        await seed_tenant(tenant_session, user.id)  # planos, mensagens e configurações padrão
    msg = f"Usuário {username} criado."
    if generated:
        msg += f" Senha inicial: {password} (anote, ela não será exibida de novo)."
    flash(request, msg)
    return back("/admin/usuarios")


@router.post("/usuarios/{user_id}/ativo")
async def user_toggle_active(
    request: Request,
    user_id: int,
    admin: User = Depends(require_superadmin),
    session: AsyncSession = Depends(get_session),
):
    user = await _other_user(session, admin, user_id)
    user.active = not user.active
    await session.commit()
    if not config.DISABLE_BACKGROUND:
        if user.active:
            await request.app.state.bots.start_tenant(user.id)
        else:
            await request.app.state.bots.stop(user.id)
    flash(request, f"{user.username} {'reativado' if user.active else 'desativado: login e bot bloqueados'}.")
    return back("/admin/usuarios")


@router.post("/usuarios/{user_id}/isento")
async def user_toggle_exempt(
    request: Request,
    user_id: int,
    admin: User = Depends(require_superadmin),
    session: AsyncSession = Depends(get_session),
):
    user = await _other_user(session, admin, user_id)
    user.subscription_exempt = not user.subscription_exempt
    await session.commit()
    if not config.DISABLE_BACKGROUND and user.active:
        if user.subscription_active:
            await request.app.state.bots.start_tenant(user.id)
        else:
            await request.app.state.bots.stop(user.id, SUBSCRIPTION_EXPIRED)
    if user.subscription_exempt:
        flash(request, f"{user.username} isento da assinatura: o bot funciona sem pagamento.")
    else:
        flash(request, f"Isenção de {user.username} removida: o bot volta a depender da assinatura.")
    return back("/admin/usuarios")


@router.post("/usuarios/{user_id}/senha")
async def user_reset_password(
    request: Request,
    user_id: int,
    admin: User = Depends(require_superadmin),
    session: AsyncSession = Depends(get_session),
):
    user = await _other_user(session, admin, user_id)
    password = generate_password()
    user.password_hash = hash_password(password)
    user.session_version += 1  # derruba as sessões abertas do usuário
    await session.commit()
    flash(request, f"Nova senha de {user.username}: {password} (anote, ela não será exibida de novo).")
    return back("/admin/usuarios")


@router.post("/usuarios/{user_id}/dias")
async def user_grant_days(
    request: Request,
    user_id: int,
    days: int = Form(...),
    admin: User = Depends(require_superadmin),
    session: AsyncSession = Depends(get_session),
):
    user = await _other_user(session, admin, user_id)
    if not 1 <= days <= MAX_DAYS:
        flash(request, f"Informe de 1 a {MAX_DAYS} dias.", "error")
        return back("/admin/usuarios")
    await platform.grant_days(session, user, days)
    if user.active and not config.DISABLE_BACKGROUND:
        await request.app.state.bots.start_tenant(user.id)
    flash(request, f"{user.username}: +{days} dia(s) de assinatura.")
    return back("/admin/usuarios")


# ------------------------------------------------------------- plataforma


@router.get("/plataforma", response_class=HTMLResponse)
async def platform_form(
    request: Request, admin: User = Depends(require_superadmin), session: AsyncSession = Depends(get_session)
):
    settings = await platform.get_platform_settings(session)
    return page(
        request,
        admin,
        "admin_platform.html",
        "platform",
        settings=settings,
        secrets={k: bool(settings.get(k)) for k in platform.PLATFORM_SECRETS},
        plans=await platform.get_platform_plans(session),
        webhook_url=platform.platform_webhook_url(),
    )


@router.post("/plataforma")
async def platform_save(
    request: Request, admin: User = Depends(require_superadmin), session: AsyncSession = Depends(get_session)
):
    form = await request.form()
    values = {}
    for key in platform.PLATFORM_SECRETS:
        value = str(form.get(key, "")).strip()
        if value:
            values[key] = value  # em branco mantém o segredo salvo
        if form.get(f"clear_{key}") == "on":
            values[key] = ""
    trial = str(form.get("trial_days", "")).strip()
    if trial.isdigit() and int(trial) <= MAX_DAYS:
        values["trial_days"] = trial
    values["signup_enabled"] = "1" if form.get("signup_enabled") == "on" else "0"
    reminder_days = parse_int_list(str(form.get("reminder_days", "")))
    values["reminder_days"] = ",".join(str(d) for d in reminder_days if d <= 60)
    for key in ("smtp_host", "smtp_user", "smtp_from"):
        values[key] = str(form.get(key, "")).strip()
    port = str(form.get("smtp_port", "")).strip()
    values["smtp_port"] = port if port.isdigit() and 0 < int(port) < 65536 else "587"
    values["smtp_tls"] = "1" if form.get("smtp_tls") == "on" else "0"
    await platform.set_platform_settings(session, values)

    errors = []
    for plan in await platform.get_platform_plans(session):
        try:
            price = parse_brl(str(form.get(f"{plan.code}_price", "")))
            if price < 100:
                raise ValueError
        except ValueError:
            errors.append(f"{plan.name}: valor inválido (mínimo R$ 1,00).")
            continue
        days_text = str(form.get(f"{plan.code}_days", "")).strip()
        if not days_text.isdigit() or not 1 <= int(days_text) <= MAX_DAYS:
            errors.append(f"{plan.name}: dias inválidos.")
            continue
        plan.name = str(form.get(f"{plan.code}_name") or plan.name).strip()[:64]
        plan.price_cents = price
        plan.days = int(days_text)
        plan.active = form.get(f"{plan.code}_active") == "on"
    await session.commit()
    for error in errors:
        flash(request, error, "error")
    if not errors:
        flash(request, "Configurações da plataforma salvas.")
    return back("/admin/plataforma")


@router.post("/plataforma/testar-email")
async def platform_test_email(
    request: Request, admin: User = Depends(require_superadmin), session: AsyncSession = Depends(get_session)
):
    form = await request.form()
    to = str(form.get("test_to", "")).strip()
    if not EMAIL_RE.match(to):
        flash(request, "Informe um e-mail válido para o teste (salve as configurações de SMTP antes).", "error")
        return back("/admin/plataforma")
    settings = await platform.get_platform_settings(session)
    if not mailer.smtp_configured(settings):
        flash(request, "Configure e salve o servidor e o remetente do SMTP antes do teste.", "error")
        return back("/admin/plataforma")
    ok = await mailer.send_email(
        settings, to, "Teste de e-mail da plataforma", "Se você recebeu esta mensagem, o SMTP está funcionando."
    )
    flash(request, f"E-mail de teste enviado para {to}." if ok else "Falha ao enviar. Confira servidor, porta, usuário e senha (veja o log).",
          "ok" if ok else "error")
    return back("/admin/plataforma")
