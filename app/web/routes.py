"""Área do usuário no painel.

Isolamento: toda rota recebe `user = Depends(current_user)` e só lê ou grava
dados com tenant_id == user.id. Registros acessados por id passam por
`_own_member`/`_own_payment`, que respondem 404 para dados de outro usuário.
"""

import logging
import mimetypes
import re

from fastapi import APIRouter, BackgroundTasks, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import config, runtime
from app.bot.keyboards import METHOD_LABELS, menu_kb
from app.db import SessionLocal
from app.models import CustomMessage, Member, MessageTemplate, Payment, Plan, PlatformPayment, Setting, User
from app.services import brcode, membership, notifier, payments, paypal, platform, reports, stats
from app.services.defaults import DEFAULT_MESSAGES, SECRET_SETTINGS
from app.services.mercadopago import MercadoPagoError, verify_webhook_signature
from app.security import fingerprint
from app.services.passwords import hash_password, verify_password
from app.services.store import get_message, get_plan, get_plans, get_settings, render_custom, set_settings
from app.utils import EMAIL_RE, admin_entry, brl, fmt_date, fmt_datetime, money, parse_admin_ids, parse_brl, parse_keywords, slugify, time_greeting, utcnow
from app.web.auth import authenticate, current_user, get_session, is_locked, login_session, register_failure

log = logging.getLogger(__name__)
router = APIRouter()
templates = Jinja2Templates(directory=config.BASE_DIR / "app" / "web" / "templates")
templates.env.filters.update(brl=brl, money=money, date=fmt_date, datetime=fmt_datetime)
templates.env.globals.update(METHOD_LABELS=METHOD_LABELS)

STATUS_LABELS = {"none": "Sem assinatura", "active": "Ativo", "expired": "Expirado"}
PAYMENT_STATUS_LABELS = {"pending": "Pendente", "approved": "Aprovado", "rejected": "Recusado", "expired": "Expirado"}
templates.env.globals.update(STATUS_LABELS=STATUS_LABELS, PAYMENT_STATUS_LABELS=PAYMENT_STATUS_LABELS)

COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
MIN_PASSWORD = 8
MAX_FIELD = 2000  # limite dos campos de texto curtos das configurações


def flash(request: Request, text: str, kind: str = "ok") -> None:
    # Reatribui a chave: a sessão só é regravada quando uma chave muda (mutar a lista não basta).
    request.session["flash"] = [*request.session.get("flash", []), {"text": text, "kind": kind}]


def page(request: Request, user: User | None, name: str, nav: str, **ctx) -> HTMLResponse:
    messages = request.session.pop("flash", [])
    tenant_id = user.id if user else None
    return templates.TemplateResponse(
        request,
        name,
        {
            "nav": nav,
            "flashes": messages,
            "user": user,
            "bot_username": runtime.usernames.get(tenant_id),
            "bot_error": runtime.errors.get(tenant_id),
            **ctx,
        },
    )


def back(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


def back_to_referer(request: Request, default: str) -> RedirectResponse:
    """Volta para a página de origem, só se for deste mesmo site."""
    referer = request.headers.get("referer") or ""
    host = request.headers.get("host") or ""
    rest = referer.split("//", 1)[-1]
    if host and rest.startswith(host + "/"):
        return back("/" + rest.split("/", 1)[1])
    return back(default)


async def _own_member(session: AsyncSession, user: User, member_id: int) -> Member:
    member = await session.get(Member, member_id)
    if member is None or member.tenant_id != user.id:
        raise HTTPException(status_code=404)
    return member


async def _own_payment(session: AsyncSession, user: User, payment_id: int) -> Payment:
    payment = await session.get(Payment, payment_id)
    if payment is None or payment.tenant_id != user.id:
        raise HTTPException(status_code=404)
    return payment


# ------------------------------------------------------------------ login


@router.get("/login", response_class=HTMLResponse)
async def login_form(request: Request):
    return page(request, None, "login.html", "login")


@router.post("/login")
async def login(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    session: AsyncSession = Depends(get_session),
):
    ip = request.client.host if request.client else "?"
    if is_locked(ip, username):
        flash(request, "Muitas tentativas. Aguarde 5 minutos.", "error")
        return back("/login")
    user = await authenticate(session, ip, username, password)
    if user is None:
        flash(request, "Usuário ou senha inválidos.", "error")
        return back("/login")
    login_session(request, user)
    return back("/")


@router.post("/logout")
async def logout(request: Request):
    request.session.clear()
    return back("/login")


# -------------------------------------------------------------- dashboard


def setup_steps(settings, bot_username: str | None) -> dict | None:
    """Primeiros passos da área, pelo que já está configurado; None quando estiver tudo pronto."""
    has_mp = bool(settings.get("mp_access_token")) and (settings.flag("enable_pix_auto") or settings.flag("enable_card"))
    has_pix = settings.flag("enable_pix_manual") and bool(settings.get("pix_key"))
    has_paypal = settings.flag("enable_paypal") and bool(settings.get("paypal_client_id"))
    steps = [
        {"label": "Conecte o seu bot do Telegram", "done": bool(bot_username), "href": "/primeiros-passos", "action": "Conectar o bot"},
        {"label": "Escolha o grupo ou canal VIP", "done": settings.group_chat_id is not None, "href": "/primeiros-passos", "action": "Escolher o grupo"},
        {"label": "Escolha como quer receber", "done": has_mp or has_pix or has_paypal, "href": "/primeiros-passos", "action": "Configurar pagamentos"},
        {"label": "Diga quem recebe os avisos de venda", "done": bool(settings.admin_ids), "href": "/primeiros-passos", "action": "Adicionar administradores"},
    ]
    pending = [step for step in steps if not step["done"]]
    if not pending:
        return None
    done = len(steps) - len(pending)
    return {"steps": steps, "done": done, "percent": round(done * 100 / len(steps)), "next": pending[0]}


@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request, session: AsyncSession = Depends(get_session)):
    if "uid" not in request.session:  # visitante: página inicial; sessão vencida vai para o login
        return await public_home(request, session)
    user = await current_user(request, session)
    data = await stats.compute(session, user.id)
    plat = await stats.compute_platform(session) if user.is_superadmin else None
    settings = await get_settings(session, user.id)
    return page(
        request,
        user,
        "dashboard.html",
        "dashboard",
        s=data,
        p=plat,
        g=await reports.overview(session, user.id),
        activity=await reports.activity(session, user.id),
        setup=setup_steps(settings, runtime.usernames.get(user.id)),
        now=utcnow(),
    )


async def public_home(request: Request, session: AsyncSession) -> HTMLResponse:
    settings = await platform.get_platform_settings(session)
    return page(
        request,
        None,
        "landing.html",
        "public",
        signup_open=settings.get("signup_enabled") == "1",
        trial_days=settings.get("trial_days", "0"),
    )


# ---------------------------------------------------------------- membros

MAX_DAYS = 3650


@router.get("/membros", response_class=HTMLResponse)
async def members_list(
    request: Request,
    status: str = "active",
    q: str = "",
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
):
    stmt = (
        select(Member)
        .where(Member.tenant_id == user.id)
        .order_by(Member.expires_at.desc().nullslast(), Member.created_at.desc())
        .limit(500)
    )
    if status in STATUS_LABELS:
        stmt = stmt.where(Member.status == status)
    if q:
        like = f"%{q.strip().lstrip('@')}%"
        conditions = [Member.first_name.ilike(like), Member.username.ilike(like), Member.email.ilike(like)]
        if q.strip().isdigit():
            conditions.append(Member.telegram_id == int(q.strip()))
        stmt = stmt.where(or_(*conditions))
    members = (await session.scalars(stmt)).all()
    plans = {p.code: p for p in await get_plans(session, user.id)}
    return page(
        request, user, "members.html", "members", members=members, plans=plans, status=status, q=q, now=utcnow()
    )


@router.post("/membros/adicionar")
async def member_add(
    request: Request,
    telegram_id: int = Form(...),
    days: int = Form(...),
    plan_code: str = Form(""),
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
):
    if not 1 <= days <= MAX_DAYS:
        flash(request, f"Informe de 1 a {MAX_DAYS} dias.", "error")
        return back("/membros")
    member = await membership.get_member_by_tg(session, user.id, telegram_id)
    if member is None:
        member = Member(tenant_id=user.id, telegram_id=telegram_id)
        session.add(member)
        await session.flush()
    plan = await get_plan(session, user.id, plan_code)
    if plan:
        membership.extend(session, member, plan, days)
    else:
        membership.add_days(session, member, days)
    session.add(
        Payment(
            tenant_id=user.id,
            member_id=member.id,
            plan_code=plan.code if plan else "manual",
            amount_cents=0,
            method="manual",
            status="approved",
            external_ref=f"manual-{member.id}-{utcnow().timestamp():.0f}",
            paid_at=utcnow(),
        )
    )
    await session.commit()
    await notifier.send_access(session, member, plan.name if plan else "Acesso liberado")
    await session.commit()
    flash(request, f"Acesso de {days} dia(s) liberado para {member.display_name}.")
    return back("/membros")


@router.post("/membros/{member_id}/estender")
async def member_extend(
    request: Request,
    member_id: int,
    days: int = Form(...),
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
):
    member = await _own_member(session, user, member_id)
    if not 1 <= days <= MAX_DAYS:
        flash(request, f"Informe de 1 a {MAX_DAYS} dias.", "error")
        return back_to_referer(request, "/membros")
    membership.add_days(session, member, days, detail="admin")
    await session.commit()
    if not member.in_group:
        await notifier.send_access(session, member, "Acesso liberado")
        await session.commit()
    flash(request, f"{member.display_name}: +{days} dia(s). Novo vencimento {fmt_date(member.expires_at)}.")
    return back_to_referer(request, "/membros")


@router.post("/membros/{member_id}/remover")
async def member_remove(
    request: Request, member_id: int, user: User = Depends(current_user), session: AsyncSession = Depends(get_session)
):
    member = await _own_member(session, user, member_id)
    settings = await get_settings(session, user.id)
    membership.mark_expired(session, member, reason="removed")
    kicked = await notifier.kick(session, settings, member)
    await session.commit()
    flash(request, f"{member.display_name} removido." + ("" if kicked else " (não foi possível tirar do grupo)"))
    return back_to_referer(request, "/membros")


@router.post("/membros/{member_id}/convite")
async def member_invite(
    request: Request, member_id: int, user: User = Depends(current_user), session: AsyncSession = Depends(get_session)
):
    member = await _own_member(session, user, member_id)
    if membership.is_active(member):
        member.in_group = False
        plan = await get_plan(session, user.id, member.plan_code)
        await notifier.send_access(session, member, plan.name if plan else "Acesso")
        await session.commit()
        flash(request, f"Novo convite enviado para {member.display_name}.")
    return back_to_referer(request, "/membros")


@router.post("/membros/{member_id}/cancelar-recorrencia")
async def member_cancel_recurring(
    request: Request, member_id: int, user: User = Depends(current_user), session: AsyncSession = Depends(get_session)
):
    member = await _own_member(session, user, member_id)
    try:
        await payments.cancel_recurring(session, await get_settings(session, user.id), member)
        flash(request, f"Recorrência de {member.display_name} cancelada.")
    except MercadoPagoError as exc:
        flash(request, f"Erro no Mercado Pago: {exc}", "error")
    return back_to_referer(request, "/membros")


# ------------------------------------------------------------- pagamentos


@router.get("/pagamentos", response_class=HTMLResponse)
async def payments_list(
    request: Request,
    status: str = "",
    method: str = "",
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
):
    stmt = (
        select(Payment, Member)
        .join(Member, Member.id == Payment.member_id)
        .where(Payment.tenant_id == user.id, Member.tenant_id == user.id)
        .order_by(Payment.created_at.desc())
        .limit(500)
    )
    if status:
        stmt = stmt.where(Payment.status == status)
    if method:
        stmt = stmt.where(Payment.method == method)
    rows = (await session.execute(stmt)).all()
    plans = {p.code: p for p in await get_plans(session, user.id)}
    return page(request, user, "payments.html", "payments", rows=rows, plans=plans, status=status, method=method)


@router.post("/pagamentos/{payment_id}/aprovar")
async def payment_approve(
    request: Request, payment_id: int, user: User = Depends(current_user), session: AsyncSession = Depends(get_session)
):
    payment = await _own_payment(session, user, payment_id)
    if payment.status == "pending":
        await payments.approve(session, payment)
        flash(request, f"Pagamento #{payment.id} aprovado e acesso liberado.")
    return back_to_referer(request, "/pagamentos")


@router.post("/pagamentos/{payment_id}/recusar")
async def payment_reject(
    request: Request, payment_id: int, user: User = Depends(current_user), session: AsyncSession = Depends(get_session)
):
    payment = await _own_payment(session, user, payment_id)
    if payment.status == "pending":
        await payments.reject(session, payment)
        flash(request, f"Pagamento #{payment.id} recusado.")
    return back_to_referer(request, "/pagamentos")


@router.post("/pagamentos/{payment_id}/verificar")
async def payment_check(
    request: Request, payment_id: int, user: User = Depends(current_user), session: AsyncSession = Depends(get_session)
):
    payment = await _own_payment(session, user, payment_id)
    if payment.status == "pending":
        try:
            await payments.refresh_payment(session, await get_settings(session, user.id), payment)
            flash(request, f"Pagamento #{payment.id}: {PAYMENT_STATUS_LABELS[payment.status]}.")
        except payments.GATEWAY_ERRORS as exc:
            gateway = "PayPal" if payment.method == "paypal" else "Mercado Pago"
            flash(request, f"Erro no {gateway}: {exc}", "error")
    return back_to_referer(request, "/pagamentos")


MAX_PROOF_BYTES = 10 * 1024 * 1024
PROOF_INLINE_TYPES = {"image/jpeg", "image/png", "image/webp", "application/pdf"}


@router.get("/pagamentos/{payment_id}/comprovante")
async def payment_proof(payment_id: int, user: User = Depends(current_user), session: AsyncSession = Depends(get_session)):
    payment = await _own_payment(session, user, payment_id)
    bot = runtime.get_bot(user.id)
    if not payment.proof_file_id or bot is None:
        return Response("Comprovante indisponível.", status_code=404)
    kind, file_id = payment.proof_file_id.split(":", 1)
    file = await bot.get_file(file_id)
    if (getattr(file, "file_size", None) or 0) > MAX_PROOF_BYTES:
        raise HTTPException(status_code=413, detail="Comprovante grande demais (máximo 10 MB).")
    content = await bot.download(file)
    media_type = "image/jpeg" if kind == "photo" else (mimetypes.guess_type(file.file_path or "")[0] or "")
    # O arquivo vem do assinante: só exibe imagem/PDF; o resto (HTML, SVG...) vira download inerte.
    headers = {"X-Content-Type-Options": "nosniff", "Content-Security-Policy": "sandbox"}
    if media_type not in PROOF_INLINE_TYPES:
        media_type = "application/octet-stream"
        headers["Content-Disposition"] = f'attachment; filename="comprovante-{payment.id}"'
    return Response(content.read(), media_type=media_type, headers=headers)


# ------------------------------------------------------------------ planos


@router.get("/planos", response_class=HTMLResponse)
async def plans_form(request: Request, user: User = Depends(current_user), session: AsyncSession = Depends(get_session)):
    settings = await get_settings(session, user.id)
    return page(
        request,
        user,
        "plans.html",
        "plans",
        plans=await get_plans(session, user.id),
        max_days=MAX_DAYS,
        paypal_currency=settings.get("paypal_currency", "USD"),
    )


def _plan_values(form, prefix: str, recurring: bool, current_name: str = "") -> tuple[dict, str | None]:
    """Valida os campos de um plano; devolve (valores, erro)."""
    name = (str(form.get(f"{prefix}_name") or "").strip() or current_name)[:64]
    if not name:
        return {}, "informe o nome."
    try:
        price = parse_brl(str(form.get(f"{prefix}_price", "")))
        if price < 100:
            raise ValueError
    except ValueError:
        return {}, "valor inválido (mínimo R$ 1,00)."
    days_text = str(form.get(f"{prefix}_days") or "").strip()
    days = int(days_text) if days_text.isdigit() else 0
    if not recurring and not 1 <= days <= MAX_DAYS:
        return {}, f"duração inválida (1 a {MAX_DAYS} dias)."
    foreign_text = str(form.get(f"{prefix}_foreign") or "").strip()
    foreign = None
    if foreign_text and not recurring:
        try:
            foreign = parse_brl(foreign_text)
            if foreign < 100:
                raise ValueError
        except ValueError:
            return {}, "valor no PayPal inválido (mínimo 1.00)."
    order_text = str(form.get(f"{prefix}_order") or "").strip()
    values = {
        "name": name,
        "description": str(form.get(f"{prefix}_description") or "").strip()[:1000],
        "price_cents": price,
        "foreign_price_cents": foreign,
        "days": 30 if recurring else days,  # recorrente: cobrança mensal
        "active": form.get(f"{prefix}_active") == "on",
    }
    if order_text.lstrip("-").isdigit():
        values["sort_order"] = int(order_text)
    return values, None


@router.post("/planos")
async def plans_save(request: Request, user: User = Depends(current_user), session: AsyncSession = Depends(get_session)):
    form = await request.form()
    errors = []
    for plan in await get_plans(session, user.id):
        values, error = _plan_values(form, plan.code, plan.recurring, plan.name)
        if error:
            errors.append(f"{plan.name}: {error}")
            continue
        for key, value in values.items():
            setattr(plan, key, value)
    await session.commit()
    for error in errors:
        flash(request, error, "error")
    if not errors:
        flash(request, "Planos salvos.")
    return back("/planos")


@router.post("/planos/novo")
async def plan_create(request: Request, user: User = Depends(current_user), session: AsyncSession = Depends(get_session)):
    form = await request.form()
    recurring = form.get("new_recurring") == "on"
    values, error = _plan_values(form, "new", recurring)
    if error:
        flash(request, f"Novo plano: {error}", "error")
        return back("/planos#novo")
    existing = await get_plans(session, user.id)
    base = slugify(values["name"])[:24] or "plano"
    codes = {p.code for p in existing}
    code, n = base, 2
    while code in codes:
        code, n = f"{base}_{n}", n + 1
    values.setdefault("sort_order", max((p.sort_order for p in existing), default=0) + 1)
    session.add(Plan(tenant_id=user.id, code=code, recurring=recurring, **values))
    await session.commit()
    flash(request, f"Plano \"{values['name']}\" criado.")
    return back(f"/planos#plano-{code}")


@router.post("/planos/{code}/excluir")
async def plan_delete(
    request: Request, code: str, user: User = Depends(current_user), session: AsyncSession = Depends(get_session)
):
    plan = await get_plan(session, user.id, code)
    if plan is None:
        raise HTTPException(status_code=404)
    in_use = await session.scalar(
        select(Member.id).where(Member.tenant_id == user.id, Member.plan_code == code).limit(1)
    ) or await session.scalar(
        select(Payment.id).where(Payment.tenant_id == user.id, Payment.plan_code == code).limit(1)
    )
    if in_use:
        flash(request, f"\"{plan.name}\" já tem membros ou pagamentos e não pode ser excluído. Desative-o para não aparecer mais no bot.", "error")
        return back("/planos")
    await session.delete(plan)
    await session.commit()
    flash(request, f"Plano \"{plan.name}\" excluído.")
    return back("/planos")


# ---------------------------------------------------------------- mensagens


@router.get("/mensagens", response_class=HTMLResponse)
async def messages_form(request: Request, user: User = Depends(current_user), session: AsyncSession = Depends(get_session)):
    rows = {
        m.key: m.text
        for m in (await session.scalars(select(MessageTemplate).where(MessageTemplate.tenant_id == user.id))).all()
    }
    items = [
        {"key": key, "title": title, "vars": variables, "text": rows.get(key, default), "default": default}
        for key, (title, default, variables) in DEFAULT_MESSAGES.items()
    ]
    settings = await get_settings(session, user.id)
    return page(
        request,
        user,
        "messages.html",
        "messages",
        items=items,
        reminder_days=settings.get("reminder_days"),
        start_keywords="\n".join(parse_keywords(settings.get("start_keywords", ""))),
        reply_unknown=settings.flag("reply_unknown"),
        custom=(
            await session.scalars(
                select(CustomMessage).where(CustomMessage.tenant_id == user.id).order_by(CustomMessage.id)
            )
        ).all(),
    )


@router.post("/mensagens/palavras-de-entrada")
async def start_keywords_save(
    request: Request, user: User = Depends(current_user), session: AsyncSession = Depends(get_session)
):
    form = await request.form()
    keywords = parse_keywords(str(form.get("start_keywords", ""))[:5000])[:200]
    await set_settings(
        session,
        user.id,
        {"start_keywords": "\n".join(keywords), "reply_unknown": "1" if form.get("reply_unknown") else "0"},
    )
    flash(request, f"Palavras de entrada salvas ({len(keywords)}).")
    return back("/mensagens#entrada")


@router.post("/mensagens")
async def messages_save(request: Request, user: User = Depends(current_user), session: AsyncSession = Depends(get_session)):
    form = await request.form()
    for key in DEFAULT_MESSAGES:
        text = str(form.get(key, "")).strip()
        if not text:
            continue
        if len(text) > MAX_MESSAGE:
            flash(request, f"A mensagem \"{DEFAULT_MESSAGES[key][0]}\" passou de {MAX_MESSAGE} caracteres e não foi salva.", "error")
            continue
        row = await session.get(MessageTemplate, (user.id, key))
        if row is None:
            session.add(MessageTemplate(tenant_id=user.id, key=key, text=text))
        else:
            row.text = text
    await session.commit()
    flash(request, "Mensagens salvas.")
    return back("/mensagens")


MAX_MESSAGE = 4000  # o Telegram aceita até 4096 caracteres


def _custom_values(form) -> tuple[dict, str | None]:
    title = str(form.get("title") or "").strip()[:64]
    triggers = parse_keywords(str(form.get("triggers") or ""))
    text = str(form.get("text") or "").strip()
    if not title:
        return {}, "informe o título."
    if not triggers:
        return {}, "informe ao menos uma palavra-gatilho."
    if not text or len(text) > MAX_MESSAGE:
        return {}, f"o texto é obrigatório e pode ter até {MAX_MESSAGE} caracteres."
    return {
        "title": title,
        "triggers": "\n".join(triggers),
        "text": text,
        "show_plans": form.get("show_plans") == "on",
        "active": form.get("active") == "on",
    }, None


async def _own_custom(session: AsyncSession, user: User, message_id: int) -> CustomMessage:
    row = await session.get(CustomMessage, message_id)
    if row is None or row.tenant_id != user.id:
        raise HTTPException(status_code=404)
    return row


@router.post("/mensagens/personalizadas")
async def custom_create(request: Request, user: User = Depends(current_user), session: AsyncSession = Depends(get_session)):
    values, error = _custom_values(await request.form())
    if error:
        flash(request, f"Nova mensagem: {error}", "error")
        return back("/mensagens#nova")
    row = CustomMessage(tenant_id=user.id, **values)
    session.add(row)
    await session.commit()
    flash(request, f"Mensagem \"{row.title}\" criada.")
    return back(f"/mensagens#custom-{row.id}")


@router.post("/mensagens/personalizadas/{message_id}")
async def custom_update(
    request: Request, message_id: int, user: User = Depends(current_user), session: AsyncSession = Depends(get_session)
):
    row = await _own_custom(session, user, message_id)
    values, error = _custom_values(await request.form())
    if error:
        flash(request, f"{row.title}: {error}", "error")
        return back(f"/mensagens#custom-{row.id}")
    for key, value in values.items():
        setattr(row, key, value)
    await session.commit()
    flash(request, f"Mensagem \"{row.title}\" salva.")
    return back(f"/mensagens#custom-{row.id}")


@router.post("/mensagens/personalizadas/{message_id}/excluir")
async def custom_delete(
    request: Request, message_id: int, user: User = Depends(current_user), session: AsyncSession = Depends(get_session)
):
    row = await _own_custom(session, user, message_id)
    await session.delete(row)
    await session.commit()
    flash(request, f"Mensagem \"{row.title}\" excluída.")
    return back("/mensagens#personalizadas")


@router.post("/mensagens/personalizadas/{message_id}/testar")
async def custom_test(
    request: Request, message_id: int, user: User = Depends(current_user), session: AsyncSession = Depends(get_session)
):
    row = await _own_custom(session, user, message_id)
    settings = await get_settings(session, user.id)
    if not settings.admin_ids:
        flash(request, "Configure ao menos um admin em Configurações para receber o teste.", "error")
        return back(f"/mensagens#custom-{row.id}")
    text = render_custom(row, settings, "Fulano")
    markup = menu_kb() if row.show_plans else None
    sent = all([await notifier.send(user.id, admin_id, f"🧪 Teste\n\n{text}", markup) for admin_id in settings.admin_ids])
    flash(request, "Mensagem de teste enviada aos admins." if sent else "Falha ao enviar (verifique o bot e se os admins deram /start).", "ok" if sent else "error")
    return back(f"/mensagens#custom-{row.id}")


@router.post("/mensagens/{key}/restaurar")
async def message_reset(
    request: Request, key: str, user: User = Depends(current_user), session: AsyncSession = Depends(get_session)
):
    if key in DEFAULT_MESSAGES:
        row = await session.get(MessageTemplate, (user.id, key))
        if row:
            row.text = DEFAULT_MESSAGES[key][1]
            await session.commit()
        flash(request, f"Mensagem \"{DEFAULT_MESSAGES[key][0]}\" restaurada.")
    return back(f"/mensagens#{key}")


@router.post("/mensagens/{key}/testar")
async def message_test(
    request: Request, key: str, user: User = Depends(current_user), session: AsyncSession = Depends(get_session)
):
    settings = await get_settings(session, user.id)
    if key not in DEFAULT_MESSAGES or not settings.admin_ids:
        flash(request, "Configure ao menos um admin em Configurações para receber o teste.", "error")
        return back("/mensagens")
    sample = {
        "nome": "Fulano", "plano": "Mensal", "valor": "R$ 29,90", "vencimento": "10/12/2026", "dias": 3,
        "descricao": "Acesso por 30 dias.", "pix_codigo": "00020126...EXEMPLO", "pix_chave": settings.get("pix_key") or "chave",
        "pix_titular": settings.get("pix_holder") or "Titular", "suporte": settings.get("support_contact") or "-",
        "recorrencia": "não", "telegram_id": 123456, "metodo": "Pix (automático)", "saudacao": time_greeting(),
    }
    text = await get_message(session, user.id, key, **sample)
    sent = all([await notifier.send(user.id, admin_id, f"🧪 Teste\n\n{text}") for admin_id in settings.admin_ids])
    flash(request, "Mensagem de teste enviada aos admins." if sent else "Falha ao enviar (verifique o bot e se os admins deram /start).", "ok" if sent else "error")
    return back(f"/mensagens#{key}")


# ---------------------------------------------------------- configuração


CONFIG_FIELDS = [
    "bot_token", "group_chat_id", "admin_ids", "support_contact",
    "mp_access_token", "mp_webhook_secret", "paypal_client_id", "paypal_client_secret",
    "pix_key", "pix_holder", "pix_city", "grace_hours", "reminder_days",
]
CONFIG_FLAGS = ["enable_pix_auto", "enable_pix_manual", "pix_manual_qr", "enable_card", "enable_paypal", "paypal_sandbox", "remove_on_expire"]


@router.get("/config", response_class=HTMLResponse)
async def config_form(request: Request, user: User = Depends(current_user), session: AsyncSession = Depends(get_session)):
    settings = await get_settings(session, user.id)
    group_status = None
    bot = runtime.get_bot(user.id)
    if bot and settings.group_chat_id:
        try:
            chat = await bot.get_chat(settings.group_chat_id)
            me = await bot.get_chat_member(settings.group_chat_id, bot.id)
            group_status = {
                "title": chat.title,
                "is_admin": me.status == "administrator",
                "can_invite": getattr(me, "can_invite_users", False),
                "can_ban": getattr(me, "can_restrict_members", False),
            }
        except Exception as exc:  # noqa: BLE001 - só diagnóstico na tela
            group_status = {"error": str(exc)}
    return page(
        request,
        user,
        "config.html",
        "config",
        settings=settings,
        secrets={k: bool(settings.get(k)) for k in SECRET_SETTINGS},
        group_status=group_status,
        webhook_url=payments.webhook_url(user.webhook_key),
        paypal_webhook_url=payments.paypal_webhook_url(user.webhook_key),
        paypal_currencies=paypal.CURRENCIES,
    )


async def _token_in_use(session: AsyncSession, user: User, token: str) -> bool:
    """O mesmo bot não pode atender duas áreas (o Telegram só entrega cada update uma vez).

    Compara a impressão digital do token (os tokens ficam criptografados) numa sessão
    sem área ligada, porque a busca precisa olhar as outras contas.
    """
    async with SessionLocal() as other_session:
        other = await other_session.scalar(
            select(Setting.tenant_id).where(
                Setting.key == "bot_token_fp", Setting.value == fingerprint(token), Setting.tenant_id != user.id
            )
        )
    return other is not None


async def _save_config(request: Request, user: User, session: AsyncSession) -> bool:
    """Grava o formulário de Configurações; devolve se o token do bot mudou."""
    form = await request.form()
    current = await get_settings(session, user.id)
    values: dict[str, str] = {}
    for key in CONFIG_FIELDS:
        value = str(form.get(key, "")).strip()
        if key in SECRET_SETTINGS and not value:
            continue  # campo em branco mantém o segredo salvo
        if len(value) > MAX_FIELD:
            flash(request, f"O campo {key} é longo demais e não foi salvo.", "error")
            continue
        values[key] = value
    for key in CONFIG_FLAGS:
        values[key] = "1" if form.get(key) == "on" else "0"
    for key in SECRET_SETTINGS:
        if form.get(f"clear_{key}") == "on":
            values[key] = ""

    if values.get("bot_token") and await _token_in_use(session, user, values["bot_token"]):
        flash(request, "Este bot já está cadastrado em outra conta. Crie outro bot no @BotFather.", "error")
        values.pop("bot_token")
    if not values.get("grace_hours", "0").isdigit():
        values["grace_hours"] = current.get("grace_hours", "24")
    currency = str(form.get("paypal_currency", "")).strip()
    if currency in paypal.CURRENCIES:
        values["paypal_currency"] = currency
    if values["pix_manual_qr"] == "1":
        missing = brcode.missing_fields(values)
        try:
            missing or brcode.build(values["pix_key"], values["pix_holder"], values["pix_city"], 100)
        except ValueError:
            missing = ["uma chave Pix válida (até 77 caracteres)"]
        if missing:
            values["pix_manual_qr"] = "0"
            flash(request, "QR Code Pix não foi ligado: falta " + ", ".join(missing) + ".", "error")

    await set_settings(session, user.id, values)
    token_changed = values.get("bot_token", current.get("bot_token")) != current.get("bot_token")
    if token_changed and not config.DISABLE_BACKGROUND:
        await request.app.state.bots.start(user.id, values.get("bot_token", ""))
    return token_changed


@router.post("/config")
async def config_save(request: Request, user: User = Depends(current_user), session: AsyncSession = Depends(get_session)):
    token_changed = await _save_config(request, user, session)
    flash(request, "Configurações salvas." + (" Bot reiniciado." if token_changed else ""))
    return back("/config")


@router.post("/config/testar-paypal")
async def config_test_paypal(
    request: Request, user: User = Depends(current_user), session: AsyncSession = Depends(get_session)
):
    await _save_config(request, user, session)
    settings = await get_settings(session, user.id)
    try:
        await payments.paypal_client(settings).get_token()
        mode = "Sandbox" if settings.flag("paypal_sandbox") else "produção"
        flash(request, f"Configurações salvas. PayPal conectado ({mode}).")
    except paypal.PayPalError as exc:
        flash(request, f"Configurações salvas, mas o PayPal falhou: {exc}", "error")
    return back("/config#paypal")


@router.post("/config/reiniciar-bot")
async def config_restart_bot(
    request: Request, user: User = Depends(current_user), session: AsyncSession = Depends(get_session)
):
    settings = await get_settings(session, user.id)
    await request.app.state.bots.start(user.id, settings.get("bot_token", ""))
    ok = runtime.get_bot(user.id) is not None
    flash(request, "Bot reiniciado." if ok else f"Falha: {runtime.errors.get(user.id)}", "ok" if ok else "error")
    return back("/config")


@router.post("/config/admins-do-grupo")
async def config_group_admins(
    request: Request, user: User = Depends(current_user), session: AsyncSession = Depends(get_session)
):
    """Preenche os admins com os administradores do grupo (id + @usuário), mantendo os já cadastrados."""
    settings = await get_settings(session, user.id)
    bot = runtime.get_bot(user.id)
    if bot is None or settings.group_chat_id is None:
        flash(request, "Conecte o bot e configure o grupo antes de buscar os administradores.", "error")
        return back("/config")
    try:
        admins = await bot.get_chat_administrators(settings.group_chat_id)
    except Exception as exc:  # noqa: BLE001 - erro do Telegram vai para a tela
        flash(request, f"Não foi possível ler os administradores do grupo: {exc}", "error")
        return back("/config")
    people = [a.user for a in admins if not a.user.is_bot]
    group_ids = {p.id for p in people}
    lines = [admin_entry(p.id, p.username, p.full_name) for p in people]
    # Admins cadastrados à mão que não estão no grupo continuam na lista.
    for entry in re.split(r"[,;\n]+", settings.get("admin_ids", "")):
        if any(i not in group_ids for i in parse_admin_ids(entry)):
            lines.append(entry.strip())
    await set_settings(session, user.id, {"admin_ids": "\n".join(lines)})
    flash(request, f"{len(people)} administrador(es) do grupo adicionados aos admins.")
    return back("/config")


@router.post("/config/usar-grupo/{chat_id}")
async def config_use_group(
    request: Request, chat_id: int, user: User = Depends(current_user), session: AsyncSession = Depends(get_session)
):
    settings = await get_settings(session, user.id)
    if chat_id not in {c.get("id") for c in settings.detected_chats}:
        raise HTTPException(status_code=404)
    await set_settings(session, user.id, {"group_chat_id": str(chat_id)})
    flash(request, f"Grupo {chat_id} selecionado.")
    return back("/config")


# ---------------------------------------------------------------- minha conta


@router.get("/conta", response_class=HTMLResponse)
async def account_form(request: Request, user: User = Depends(current_user)):
    return page(request, user, "account.html", "account")


@router.post("/conta")
async def account_save(
    request: Request,
    name: str = Form(""),
    accent_color: str = Form(""),
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
):
    user.name = name.strip()[:128]
    user.accent_color = accent_color if COLOR_RE.match(accent_color or "") else ""
    await session.commit()
    flash(request, "Área personalizada.")
    return back("/conta")


@router.post("/conta/senha")
async def account_password(
    request: Request,
    current_password: str = Form(...),
    new_password: str = Form(...),
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
):
    if is_locked(f"senha:{user.id}"):
        flash(request, "Muitas tentativas. Aguarde 5 minutos.", "error")
        return back("/conta")
    if not verify_password(current_password, user.password_hash):
        register_failure(f"senha:{user.id}")
        flash(request, "Senha atual incorreta.", "error")
        return back("/conta")
    if len(new_password) < MIN_PASSWORD:
        flash(request, f"A nova senha precisa ter ao menos {MIN_PASSWORD} caracteres.", "error")
        return back("/conta")
    user.password_hash = hash_password(new_password)
    user.session_version += 1  # derruba as outras sessões abertas
    await session.commit()
    login_session(request, user)
    flash(request, "Senha alterada.")
    return back("/conta")


# ------------------------------------------- assinatura da plataforma (usuário)


async def _own_platform_payment(session: AsyncSession, user: User, payment_id: int) -> PlatformPayment:
    payment = await session.get(PlatformPayment, payment_id)
    if payment is None or payment.user_id != user.id:
        raise HTTPException(status_code=404)
    return payment


@router.get("/assinatura", response_class=HTMLResponse)
async def subscription_page(
    request: Request, user: User = Depends(current_user), session: AsyncSession = Depends(get_session)
):
    history = (
        await session.scalars(
            select(PlatformPayment)
            .where(PlatformPayment.user_id == user.id, PlatformPayment.status != "pending")
            .order_by(PlatformPayment.created_at.desc())
            .limit(20)
        )
    ).all()
    settings = await platform.get_platform_settings(session)
    return page(
        request,
        user,
        "subscription.html",
        "subscription",
        plans=await platform.get_platform_plans(session, only_active=True),
        history=history,
        can_pay=bool(settings.get("mp_access_token")),
    )


@router.post("/assinatura/email")
async def subscription_email(
    request: Request, email: str = Form(...), user: User = Depends(current_user), session: AsyncSession = Depends(get_session)
):
    email = email.strip().lower()
    if not EMAIL_RE.match(email):
        flash(request, "E-mail inválido.", "error")
        return back("/assinatura")
    user.email = email
    await session.commit()
    flash(request, "E-mail salvo.")
    return back("/assinatura")


@router.post("/assinatura/pagar")
async def subscription_pay(
    request: Request,
    plan_code: str = Form(...),
    method: str = Form(...),
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
):
    plan = await session.get(platform.PlatformPlan, plan_code)
    if user.exempt or plan is None or not plan.active or method not in ("pix", "card", "card_recurring"):
        raise HTTPException(status_code=404)
    if not user.email:
        flash(request, "Informe seu e-mail antes de pagar (o Mercado Pago exige).", "error")
        return back("/assinatura")
    if method == "card_recurring":
        if user.recurring_active:
            flash(request, "Você já tem a renovação automática ativa.", "error")
            return back("/assinatura")
        try:
            checkout = await platform.start_recurring(session, user, plan)
        except MercadoPagoError as exc:
            log.error("Recorrência da plataforma falhou (usuário %s): %s", user.id, exc)
            flash(request, "Não foi possível iniciar a renovação automática agora. Tente novamente.", "error")
            return back("/assinatura")
        return RedirectResponse(checkout, status_code=303)
    # Reaproveita a cobrança pendente recente do mesmo plano e forma (evita gerar várias).
    recent = await session.scalar(
        select(PlatformPayment)
        .where(
            PlatformPayment.user_id == user.id,
            PlatformPayment.plan_code == plan.code,
            PlatformPayment.method == method,
            PlatformPayment.status == "pending",
            PlatformPayment.created_at >= utcnow() - platform.REUSE_WINDOW,
        )
        .order_by(PlatformPayment.created_at.desc())
    )
    if recent is None:
        try:
            recent = await (platform.start_pix if method == "pix" else platform.start_card)(session, user, plan)
        except MercadoPagoError as exc:
            log.error("Cobrança da plataforma falhou (usuário %s): %s", user.id, exc)
            flash(request, "Não foi possível gerar a cobrança agora. Tente novamente em instantes.", "error")
            return back("/assinatura")
    return back(f"/assinatura/pagamento/{recent.id}")


@router.post("/assinatura/recorrencia/cancelar")
async def subscription_cancel_recurring(
    request: Request, user: User = Depends(current_user), session: AsyncSession = Depends(get_session)
):
    if not user.platform_preapproval_id or user.platform_recurring_status == "cancelled":
        raise HTTPException(status_code=404)
    try:
        await platform.cancel_recurring(session, user)
    except MercadoPagoError as exc:
        log.error("Cancelamento da recorrência da plataforma falhou (usuário %s): %s", user.id, exc)
        flash(request, "Não foi possível cancelar agora. Tente novamente em instantes.", "error")
        return back("/assinatura")
    flash(request, f"Renovação automática cancelada. A assinatura continua até {fmt_date(user.subscription_until)}.")
    return back("/assinatura")


@router.post("/assinatura/recorrencia/verificar")
async def subscription_check_recurring(
    request: Request, user: User = Depends(current_user), session: AsyncSession = Depends(get_session)
):
    if not user.platform_preapproval_id:
        raise HTTPException(status_code=404)
    try:
        renewed = await platform.sync_recurring(session, user)
    except MercadoPagoError as exc:
        log.warning("Falha ao verificar recorrência da plataforma (usuário %s): %s", user.id, exc)
        renewed = False
    if renewed and not config.DISABLE_BACKGROUND:
        await request.app.state.bots.start_tenant(user.id)
    if renewed:
        flash(request, "Cobrança confirmada! Assinatura renovada.")
    elif user.recurring_active:
        flash(request, "Renovação automática ativa. A primeira cobrança aparece aqui assim que for aprovada.")
    else:
        flash(request, "Ainda não identificamos a autorização no Mercado Pago. Aguarde alguns segundos.", "error")
    return back("/assinatura")


@router.get("/assinatura/pagamento/{payment_id}", response_class=HTMLResponse)
async def subscription_payment_page(
    request: Request, payment_id: int, user: User = Depends(current_user), session: AsyncSession = Depends(get_session)
):
    payment = await _own_platform_payment(session, user, payment_id)
    plan = await session.get(platform.PlatformPlan, payment.plan_code)
    return page(request, user, "subscription_payment.html", "subscription", payment=payment, plan=plan)


@router.post("/assinatura/pagamento/{payment_id}/verificar")
async def subscription_payment_check(
    request: Request, payment_id: int, user: User = Depends(current_user), session: AsyncSession = Depends(get_session)
):
    payment = await _own_platform_payment(session, user, payment_id)
    try:
        approved = await platform.refresh(session, payment)
    except MercadoPagoError as exc:
        log.warning("Falha ao verificar pagamento da plataforma %s: %s", payment.id, exc)
        approved = False
    if approved or payment.status == "approved":
        if approved and not config.DISABLE_BACKGROUND:
            await request.app.state.bots.start_tenant(user.id)
        flash(request, "Pagamento confirmado! Assinatura renovada e bot ligado.")
        return back("/assinatura")
    flash(request, "Ainda não identificamos o pagamento. Aguarde alguns segundos e verifique de novo.", "error")
    return back(f"/assinatura/pagamento/{payment.id}")


# ------------------------------------------------------- públicas (sem login)


async def _process_webhook(tenant_id: int, topic: str, data_id: str) -> None:
    async with SessionLocal() as session:
        try:
            await payments.handle_webhook(session, tenant_id, topic, data_id)
        except MercadoPagoError as exc:
            log.warning("Webhook %s/%s (usuário %s): %s", topic, data_id, tenant_id, exc)
        except Exception:  # noqa: BLE001
            log.exception("Erro ao processar webhook %s/%s (usuário %s)", topic, data_id, tenant_id)


def _webhook_topic_and_id(request: Request, body: dict) -> tuple[str, str]:
    params = request.query_params
    topic = params.get("type") or params.get("topic") or body.get("type") or body.get("topic") or ""
    data_id = params.get("data.id") or params.get("id") or str((body.get("data") or {}).get("id") or "")
    return topic, data_id


async def _process_platform_webhook(app, topic: str, data_id: str) -> None:
    async with SessionLocal() as session:
        try:
            renewed = await platform.handle_webhook(session, topic, data_id)
        except MercadoPagoError as exc:
            log.warning("Webhook da plataforma %s: %s", data_id, exc)
            return
    if renewed and not config.DISABLE_BACKGROUND:
        await app.state.bots.start_tenant(renewed)


# Declarada antes da rota com {webhook_key}, senão "plataforma" seria lida como chave.
@router.post(platform.PLATFORM_WEBHOOK_PATH)
async def platform_webhook(request: Request, background: BackgroundTasks):
    try:
        body = await request.json()
    except ValueError:
        body = {}
    topic, data_id = _webhook_topic_and_id(request, body)
    if not topic or not data_id:
        return JSONResponse({"ok": True})
    async with SessionLocal() as session:
        secret = (await platform.get_platform_settings(session)).get("mp_webhook_secret", "")
    if secret and not verify_webhook_signature(
        secret, request.headers.get("x-signature", ""), request.headers.get("x-request-id", ""), data_id
    ):
        log.warning("Webhook da plataforma com assinatura inválida (%s)", data_id)
        return JSONResponse({"ok": False}, status_code=401)
    background.add_task(_process_platform_webhook, request.app, topic, data_id)
    return JSONResponse({"ok": True})


@router.post("/webhooks/mercadopago/{webhook_key}")
async def mp_webhook(request: Request, webhook_key: str, background: BackgroundTasks):
    async with SessionLocal() as session:
        tenant = await session.scalar(select(User).where(User.webhook_key == webhook_key, User.active.is_(True)))
        tenant_id = tenant.id if tenant else None
        secret = (await get_settings(session, tenant_id)).get("mp_webhook_secret", "") if tenant_id else ""
    if tenant is None:
        return JSONResponse({"ok": False}, status_code=404)
    if not tenant.subscription_active:
        # Área pausada (assinatura vencida): o pagamento é conciliado pelo agendador após renovar.
        return JSONResponse({"ok": True})

    try:
        body = await request.json()
    except ValueError:
        body = {}
    topic, data_id = _webhook_topic_and_id(request, body)
    if not topic or not data_id:
        return JSONResponse({"ok": True})

    if not secret:
        log.warning("Webhook aceito sem validação (usuário %s): configure a assinatura secreta", tenant_id)
    elif not verify_webhook_signature(
        secret, request.headers.get("x-signature", ""), request.headers.get("x-request-id", ""), data_id
    ):
        log.warning("Webhook com assinatura inválida (%s/%s, usuário %s)", topic, data_id, tenant_id)
        return JSONResponse({"ok": False}, status_code=401)

    background.add_task(_process_webhook, tenant_id, topic, data_id)
    return JSONResponse({"ok": True})


def _paypal_order_id(body: dict) -> str | None:
    event = str(body.get("event_type") or "")
    resource = body.get("resource") or {}
    if event.startswith("CHECKOUT.ORDER."):
        return resource.get("id")
    if event.startswith("PAYMENT.CAPTURE."):
        return ((resource.get("supplementary_data") or {}).get("related_ids") or {}).get("order_id")
    return None


async def _process_paypal_webhook(tenant_id: int, order_id: str) -> None:
    async with SessionLocal() as session:
        try:
            await payments.handle_paypal_webhook(session, tenant_id, order_id)
        except paypal.PayPalError as exc:
            log.warning("Falha ao processar webhook do PayPal (usuário %s): %s", tenant_id, exc)
        except Exception:  # noqa: BLE001
            log.exception("Erro ao processar webhook do PayPal (usuário %s)", tenant_id)


@router.post("/webhooks/paypal/{webhook_key}")
async def paypal_webhook(request: Request, webhook_key: str, background: BackgroundTasks):
    async with SessionLocal() as session:
        tenant = await session.scalar(select(User).where(User.webhook_key == webhook_key, User.active.is_(True)))
    if tenant is None:
        return JSONResponse({"ok": False}, status_code=404)
    if not tenant.subscription_active:
        return JSONResponse({"ok": True})  # área pausada: o agendador concilia após renovar
    try:
        body = await request.json()
    except ValueError:
        body = {}
    order_id = _paypal_order_id(body) if isinstance(body, dict) else None
    if order_id:
        # Só aponta o pedido: a liberação depende da consulta à API do PayPal.
        background.add_task(_process_paypal_webhook, tenant.id, str(order_id))
    return JSONResponse({"ok": True})


@router.get("/obrigado", response_class=HTMLResponse)
async def thanks(request: Request):
    return templates.TemplateResponse(request, "thanks.html", {})


@router.get("/health")
async def health():
    return {"ok": True}
