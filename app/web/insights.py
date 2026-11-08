"""Primeiros passos (assistente em 3 etapas), Alertas e Relatórios da área do usuário."""

import asyncio
import re

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app import config, runtime
from app.models import User
from app.services import reports
from app.services.store import get_settings, set_settings
from app.utils import EMAIL_RE, parse_admin_ids, to_local, utcnow
from app.web.auth import current_user, get_session
from app.web.routes import _token_in_use, back, flash, page

router = APIRouter()

TOKEN_RE = re.compile(r"^\d{5,12}:[A-Za-z0-9_-]{30,}$")
STEPS = ("Conectar o bot", "Escolher o grupo", "Receber e avisar")


def _first_pending(settings) -> int:
    if not settings.get("bot_token"):
        return 1
    if settings.group_chat_id is None:
        return 2
    return 3


@router.get("/primeiros-passos", response_class=HTMLResponse)
async def setup_wizard(
    request: Request, etapa: int = 0, user: User = Depends(current_user), session: AsyncSession = Depends(get_session)
):
    settings = await get_settings(session, user.id)
    step = etapa if etapa in (1, 2, 3) else _first_pending(settings)
    return page(
        request,
        user,
        "setup.html",
        "setup",
        step=step,
        steps=[{"name": name, "number": i, "done": i < step} for i, name in enumerate(STEPS, start=1)],
        percent=round((step - 1) * 100 / len(STEPS)) + 8,
        settings=settings,
        has_token=bool(settings.get("bot_token")),
        has_mp=bool(settings.get("mp_access_token")),
        bot_username=runtime.usernames.get(user.id),
        bot_error=runtime.errors.get(user.id),
    )


@router.post("/primeiros-passos/bot")
async def setup_bot(
    request: Request,
    bot_token: str = Form(""),
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
):
    token = bot_token.strip()
    if not TOKEN_RE.match(token):
        flash(request, "Esse código não parece o do @BotFather. Copie a mensagem inteira que ele enviou, só a parte com números e letras.", "error")
        return back("/primeiros-passos?etapa=1")
    if await _token_in_use(session, user, token):
        flash(request, "Este bot já está cadastrado em outra conta. Crie outro bot no @BotFather.", "error")
        return back("/primeiros-passos?etapa=1")
    await set_settings(session, user.id, {"bot_token": token})
    if not config.DISABLE_BACKGROUND:
        await request.app.state.bots.start(user.id, token)
        if runtime.get_bot(user.id) is None:
            flash(request, f"Não conseguimos conectar: {runtime.errors.get(user.id) or 'confira o código'}.", "error")
            return back("/primeiros-passos?etapa=1")
    flash(request, "Pronto! Seu bot foi conectado.")
    return back("/primeiros-passos?etapa=2")


@router.post("/primeiros-passos/grupo/{chat_id}")
async def setup_group(
    request: Request, chat_id: int, user: User = Depends(current_user), session: AsyncSession = Depends(get_session)
):
    settings = await get_settings(session, user.id)
    chat = next((c for c in settings.detected_chats if c.get("id") == chat_id), None)
    if chat is None:
        raise HTTPException(status_code=404)
    await set_settings(session, user.id, {"group_chat_id": str(chat_id)})
    flash(request, f"Pronto! O grupo {chat.get('title') or chat_id} foi escolhido.")
    return back("/primeiros-passos?etapa=3")


@router.post("/primeiros-passos/receber")
async def setup_payments(
    request: Request,
    pix_key: str = Form(""),
    pix_holder: str = Form(""),
    mp_access_token: str = Form(""),
    admin_ids: str = Form(""),
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
):
    settings = await get_settings(session, user.id)
    values: dict[str, str] = {}
    if mp_access_token.strip():
        values |= {"mp_access_token": mp_access_token.strip(), "enable_pix_auto": "1", "enable_card": "1"}
    if pix_key.strip():
        values |= {"pix_key": pix_key.strip()[:140], "pix_holder": pix_holder.strip()[:128], "enable_pix_manual": "1"}
    if not values and not settings.get("mp_access_token") and not settings.get("pix_key"):
        flash(request, "Escolha pelo menos uma forma de receber: Mercado Pago ou a sua chave Pix.", "error")
        return back("/primeiros-passos?etapa=3")
    if admin_ids.strip():
        if not parse_admin_ids(admin_ids):
            flash(request, "Informe o número do seu Telegram (envie /id ao seu bot para descobrir).", "error")
            return back("/primeiros-passos?etapa=3")
        values["admin_ids"] = admin_ids.strip()
    await set_settings(session, user.id, values)
    flash(request, "Tudo pronto! Seu bot já pode vender. Acompanhe tudo por aqui.")
    return back("/")


# --------------------------------------------------------------------- alertas

ALERT_FLAGS = ("alert_sale", "alert_joined", "alert_left", "alert_exits", "alert_weekly", "alert_telegram", "alert_email")


@router.get("/alertas", response_class=HTMLResponse)
async def alerts_form(request: Request, user: User = Depends(current_user), session: AsyncSession = Depends(get_session)):
    return page(request, user, "alerts.html", "alerts", settings=await get_settings(session, user.id))


@router.post("/alertas")
async def alerts_save(request: Request, user: User = Depends(current_user), session: AsyncSession = Depends(get_session)):
    form = await request.form()
    values = {key: "1" if form.get(key) == "on" else "0" for key in ALERT_FLAGS}
    email = str(form.get("alert_email_to", "")).strip()
    if values["alert_email"] == "1" and not EMAIL_RE.match(email):
        flash(request, "Informe um e-mail válido para receber os alertas por e-mail.", "error")
        return back("/alertas")
    minimum = str(form.get("alert_exits_min", "")).strip()
    values["alert_email_to"] = email
    values["alert_exits_min"] = minimum if minimum.isdigit() and 1 <= int(minimum) <= 10_000 else "5"
    await set_settings(session, user.id, values)
    flash(request, "Pronto! Suas preferências de alerta foram salvas.")
    return back("/alertas")


# ------------------------------------------------------------------ relatórios


def _days(dias: int) -> int:
    return dias if dias in reports.PERIODS else 30


@router.get("/relatorios", response_class=HTMLResponse)
async def reports_page(
    request: Request, dias: int = 30, user: User = Depends(current_user), session: AsyncSession = Depends(get_session)
):
    days = _days(dias)
    return page(
        request, user, "reports.html", "reports",
        r=await reports.summary(session, user.id, days), periods=reports.PERIODS,
    )


@router.get("/relatorios/exportar.{formato}")
async def reports_export(
    formato: str, dias: int = 30, user: User = Depends(current_user), session: AsyncSession = Depends(get_session)
):
    if formato not in ("pdf", "xlsx"):
        raise HTTPException(status_code=404)
    days = _days(dias)
    data = await reports.summary(session, user.id, days)
    build = reports.to_pdf if formato == "pdf" else reports.to_xlsx
    content = await asyncio.to_thread(build, data, user.area_name)
    name = f"relatorio-{days}-dias-{to_local(utcnow()):%Y-%m-%d}.{formato}"
    media = "application/pdf" if formato == "pdf" else "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    return Response(content, media_type=media, headers={"Content-Disposition": f'attachment; filename="{name}"'})
