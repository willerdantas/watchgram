"""Movimento do grupo: entradas, saídas, retenção, relatórios e alertas para o dono da área."""

import io
from collections import Counter
from datetime import datetime, timedelta

from fpdf import FPDF
from openpyxl import Workbook
from openpyxl.styles import Font
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.keyboards import METHOD_LABELS
from app.models import Event, Member, Payment
from app.services import notifier
from app.services.store import Settings, get_settings, set_settings
from app.utils import brl, fmt_datetime, local_to_utc, money, time_ago, to_local, utcnow

ACTIONS = {
    "joined": "entrou no grupo",
    "left": "saiu do grupo",
    "new": "assinou pela primeira vez",
    "renew": "renovou a assinatura",
    "reactivate": "voltou a assinar",
    "expired": "ficou com a assinatura vencida",
    "removed": "foi removido",
    "recurring_canceled": "cancelou a renovação automática",
}
PERIODS = (7, 30, 90)


def percent(current: float, previous: float) -> float | None:
    return round((current - previous) * 100 / previous, 1) if previous else None


def _today_start(now: datetime) -> datetime:
    local = to_local(now).replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=None)
    return local_to_utc(local)


async def _movement(session: AsyncSession, tenant_id: int, since: datetime) -> list[Event]:
    return (
        await session.scalars(
            select(Event).where(
                Event.tenant_id == tenant_id, Event.created_at >= since, Event.kind.in_(("joined", "left"))
            )
        )
    ).all()


def _daily(events: list[Event], days: int, now: datetime) -> list[dict]:
    joined, left = Counter(), Counter()
    for event in events:
        day = to_local(event.created_at).strftime("%d/%m")
        (joined if event.kind == "joined" else left)[day] += 1
    labels = [(to_local(now) - timedelta(days=i)).strftime("%d/%m") for i in range(days - 1, -1, -1)]
    return [{"dia": label, "novos": joined[label], "saidas": left[label]} for label in labels]


async def overview(session: AsyncSession, tenant_id: int) -> dict:
    """Indicadores da visão geral: total, entradas e saídas de hoje e retenção em 30 dias."""
    now = utcnow()
    today = _today_start(now)
    yesterday = local_to_utc(to_local(today).replace(tzinfo=None) - timedelta(days=1))
    month, prev_month = now - timedelta(days=30), now - timedelta(days=60)
    events = await _movement(session, tenant_id, now - timedelta(days=90))

    def count(kind: str, start: datetime, end: datetime = now) -> int:
        return sum(1 for e in events if e.kind == kind and start <= e.created_at < end)

    total = await session.scalar(
        select(func.count(Member.id)).where(Member.tenant_id == tenant_id, Member.in_group.is_(True))
    )
    left_month, left_prev = count("left", month), count("left", prev_month, month)
    base = max(total - count("joined", month) + left_month, 0)  # quantos havia 30 dias atrás
    base_prev = max(base - count("joined", prev_month, month) + left_prev, 0)
    retention = (1 - left_month / base) * 100 if base else None
    retention_prev = (1 - left_prev / base_prev) * 100 if base_prev else None
    return {
        "total": total,
        "total_change": percent(total, base),
        "joined_today": count("joined", today),
        "joined_change": percent(count("joined", today), count("joined", yesterday, today)),
        "left_today": count("left", today),
        "left_change": percent(count("left", today), count("left", yesterday, today)),
        "retention": retention,
        "retention_change": round(retention - retention_prev, 1)
        if retention is not None and retention_prev is not None
        else None,
        "daily": _daily(events, 90, now),
    }


async def activity(session: AsyncSession, tenant_id: int, limit: int = 8, since: datetime | None = None) -> list[dict]:
    query = (
        select(Event, Member)
        .join(Member, Event.member_id == Member.id)
        .where(Event.tenant_id == tenant_id, Event.kind.in_(ACTIONS))
        .order_by(Event.created_at.desc(), Event.id.desc())
        .limit(limit)
    )
    if since is not None:
        query = query.where(Event.created_at >= since)
    rows = (await session.execute(query)).all()
    return [
        {
            "name": member.display_name,
            "action": ACTIONS[event.kind],
            "kind": event.kind,
            "when": time_ago(event.created_at),
            "at": event.created_at,
            "entered": event.kind in ("joined", "new", "reactivate"),
        }
        for event, member in rows
    ]


async def summary(session: AsyncSession, tenant_id: int, days: int) -> dict:
    """Dados da página Relatórios e dos arquivos exportados."""
    now = utcnow()
    since = now - timedelta(days=days)
    events = await _movement(session, tenant_id, since)
    joined = sum(1 for e in events if e.kind == "joined")
    left = len(events) - joined
    paid = (
        await session.scalars(
            select(Payment).where(
                Payment.tenant_id == tenant_id, Payment.status == "approved", Payment.paid_at >= since
            )
        )
    ).all()
    foreign = Counter()
    for payment in paid:
        if payment.currency:
            foreign[payment.currency] += payment.amount_cents
    daily = _daily(events, days, now)
    best = max(daily, key=lambda d: d["novos"])

    highlights = []
    if joined and left:
        ratio = str(round(joined / left, 1)).replace(".", ",").removesuffix(",0")
        highlights.append(f"Entraram {ratio} pessoas para cada 1 que saiu.")
    elif joined:
        highlights.append("Ninguém saiu do grupo no período.")
    if best["novos"]:
        highlights.append(f"O dia com mais entradas foi {best['dia']}, com {best['novos']} membro(s) novo(s).")
    if not events:
        highlights.append("Ainda não há entradas ou saídas neste período.")
    return {
        "days": days,
        "joined": joined,
        "left": left,
        "net": joined - left,
        "revenue_cents": sum(p.amount_cents for p in paid if not p.currency),
        "sales": len(paid),
        "foreign": dict(foreign),
        "daily": daily,
        "highlights": highlights,
        "activity": await activity(session, tenant_id, limit=1000, since=since),
        "payments": [
            {
                "at": p.paid_at,
                "plan": p.plan_code,
                "method": METHOD_LABELS.get(p.method, p.method),
                "value": money(p.amount_cents, p.currency),
            }
            for p in sorted(paid, key=lambda p: p.paid_at, reverse=True)
        ],
    }


# ------------------------------------------------------------------ arquivos


def _cell(value):
    """Nomes vêm do Telegram: um texto iniciado por = não pode virar fórmula no Excel."""
    if isinstance(value, str) and value[:1] in ("=", "+", "-", "@"):
        return "'" + value
    return value


def to_xlsx(data: dict, area: str) -> bytes:
    book = Workbook()
    sheet = book.active
    sheet.title = "Resumo"
    rows = [
        ("Área", area),
        ("Período", f"Últimos {data['days']} dias"),
        ("Entraram", data["joined"]),
        ("Saíram", data["left"]),
        ("Ganho no período", data["net"]),
        ("Vendas aprovadas", data["sales"]),
        ("Receita em reais", data["revenue_cents"] / 100),
        *[(f"Receita em {currency}", cents / 100) for currency, cents in data["foreign"].items()],
    ]
    for row in rows:
        sheet.append([_cell(value) for value in row])
    sheet.column_dimensions["A"].width = 24
    sheet.column_dimensions["B"].width = 28

    def add_sheet(title: str, header: list[str], lines: list[list]) -> None:
        tab = book.create_sheet(title)
        tab.append(header)
        for cell in tab[1]:
            cell.font = Font(bold=True)
        for line in lines:
            tab.append([_cell(value) for value in line])
        for index in range(len(header)):
            tab.column_dimensions[chr(65 + index)].width = 26

    add_sheet("Por dia", ["Dia", "Membros novos", "Membros que saíram"],
              [[d["dia"], d["novos"], d["saidas"]] for d in data["daily"]])
    add_sheet("Atividade", ["Quando", "Quem", "O que aconteceu"],
              [[fmt_datetime(a["at"]), a["name"], a["action"]] for a in data["activity"]])
    add_sheet("Pagamentos", ["Pago em", "Plano", "Forma", "Valor"],
              [[fmt_datetime(p["at"]), p["plan"], p["method"], p["value"]] for p in data["payments"]])
    buffer = io.BytesIO()
    book.save(buffer)
    return buffer.getvalue()


def _latin(text: str) -> str:
    """A fonte padrão do PDF só tem caracteres latinos; emojis viram "?"."""
    return str(text).encode("cp1252", "replace").decode("cp1252")


def to_pdf(data: dict, area: str) -> bytes:
    pdf = FPDF()
    pdf.set_auto_page_break(True, margin=16)
    pdf.add_page()
    width = pdf.w - 2 * pdf.l_margin

    def line(text: str, size: int = 11, bold: bool = False, gap: float = 6) -> None:
        pdf.set_font("Helvetica", "B" if bold else "", size)
        pdf.multi_cell(width, gap, _latin(text), new_x="LMARGIN", new_y="NEXT")

    line("Relatório do grupo", 18, True, 9)
    line(f"{area} - últimos {data['days']} dias", gap=8)
    pdf.ln(2)
    line("Resumo", 13, True, 8)
    line(f"Entraram: {data['joined']}")
    line(f"Saíram: {data['left']}")
    line(f"Ganho no período: {data['net']:+d}")
    line(f"Vendas aprovadas: {data['sales']}")
    line(f"Receita em reais: {brl(data['revenue_cents'])}")
    for currency, cents in data["foreign"].items():
        line(f"Receita em {currency}: {money(cents, currency)}")
    pdf.ln(2)
    line("O que aconteceu", 13, True, 8)
    for text in data["highlights"]:
        line(f"- {text}")
    pdf.ln(2)
    line("Atividade recente", 13, True, 8)
    for item in data["activity"][:40]:
        line(f"{fmt_datetime(item['at'])}  {item['name']} {item['action']}", 10, gap=5.5)
    return bytes(pdf.output())


# -------------------------------------------------------------------- alertas


async def alert_joined(session: AsyncSession, settings: Settings, member: Member) -> None:
    await notifier.alert(session, settings, "alert_joined", "Novo membro no grupo", f"{member.display_name} entrou no grupo.")


async def alert_left(session: AsyncSession, settings: Settings, member: Member) -> None:
    await notifier.alert(session, settings, "alert_left", "Saída do grupo", f"{member.display_name} saiu do grupo.")
    await _alert_many_exits(session, settings)


async def _alert_many_exits(session: AsyncSession, settings: Settings) -> None:
    """Avisa uma vez por dia quando as saídas chegam ao limite escolhido."""
    now = utcnow()
    day = to_local(now).strftime("%Y-%m-%d")
    if not settings.flag("alert_exits") or settings.get("alert_exits_date") == day:
        return
    exits = sum(1 for e in await _movement(session, settings.tenant_id, _today_start(now)) if e.kind == "left")
    if exits >= settings.as_int("alert_exits_min", 5):
        await notifier.alert(
            session, settings, "alert_exits", "Muitas saídas hoje", f"{exits} pessoas saíram do grupo hoje."
        )
        await set_settings(session, settings.tenant_id, {"alert_exits_date": day})


async def send_weekly(session: AsyncSession, tenant_id: int) -> None:
    """Resumo da semana, às segundas-feiras."""
    settings = await get_settings(session, tenant_id)
    today = to_local(utcnow())
    day = today.strftime("%Y-%m-%d")
    if not settings.flag("alert_weekly") or today.weekday() != 0 or settings.get("alert_weekly_date") == day:
        return
    data = await summary(session, tenant_id, 7)
    await notifier.alert(
        session,
        settings,
        "alert_weekly",
        "Resumo da semana",
        f"Entraram {data['joined']} e saíram {data['left']} pessoas. "
        f"Vendas: {data['sales']}, somando {brl(data['revenue_cents'])}.",
    )
    await set_settings(session, tenant_id, {"alert_weekly_date": day})
