"""Métricas do painel. Agregação em Python para respeitar o fuso local."""

from collections import Counter, defaultdict
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.keyboards import METHOD_LABELS
from app.models import Event, Member, Payment, Plan, PlatformPayment, PlatformPlan, SupportTicket, User
from app.utils import local_to_utc, to_local, utcnow


def _month_start_local(dt_local):
    return dt_local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def _shift_month(dt_local, months: int):
    month = dt_local.month - 1 + months
    year = dt_local.year + month // 12
    return dt_local.replace(year=year, month=month % 12 + 1)


async def compute(session: AsyncSession, tenant_id: int) -> dict:
    now = utcnow()
    now_local = to_local(now)
    month_start_local = _month_start_local(now_local)
    month_start = local_to_utc(month_start_local.replace(tzinfo=None))
    prev_month_start = local_to_utc(_shift_month(month_start_local, -1).replace(tzinfo=None))
    six_months_start = local_to_utc(_shift_month(month_start_local, -5).replace(tzinfo=None))

    plans = {p.code: p for p in (await session.scalars(select(Plan).where(Plan.tenant_id == tenant_id))).all()}

    active_members = (await session.scalars(select(Member).where(Member.tenant_id == tenant_id, Member.status == "active"))).all()
    active = len(active_members)
    recurring_active = sum(1 for m in active_members if m.recurring_status == "authorized")
    mrr_cents = 0
    for m in active_members:
        plan = plans.get(m.plan_code or "")
        if plan and plan.days:
            mrr_cents += round(plan.price_cents * 30 / plan.days)

    payments = (
        await session.scalars(
            select(Payment).where(
                Payment.tenant_id == tenant_id, Payment.status == "approved", Payment.paid_at >= six_months_start
            )
        )
    ).all()
    # Moeda estrangeira não soma com real: fica à parte, por moeda, sem conversão.
    foreign_month = Counter()
    for p in payments:
        if p.currency and p.paid_at >= month_start:
            foreign_month[p.currency] += p.amount_cents
    payments = [p for p in payments if not p.currency]

    revenue_month = sum(p.amount_cents for p in payments if p.paid_at >= month_start)
    sales_month = sum(1 for p in payments if p.paid_at >= month_start)
    revenue_prev = sum(p.amount_cents for p in payments if prev_month_start <= p.paid_at < month_start)

    # Receita diária (últimos 30 dias) e mensal (últimos 6 meses)
    daily = defaultdict(int)
    monthly = defaultdict(int)
    by_method = Counter()
    by_plan = Counter()
    since_30 = now - timedelta(days=30)
    for p in payments:
        local = to_local(p.paid_at)
        monthly[local.strftime("%m/%Y")] += p.amount_cents
        if p.paid_at >= since_30:
            daily[local.strftime("%d/%m")] += p.amount_cents
            by_method[METHOD_LABELS.get(p.method, p.method)] += p.amount_cents
            plan = plans.get(p.plan_code)
            by_plan[plan.name if plan else p.plan_code] += p.amount_cents

    daily_labels = [(now_local - timedelta(days=i)).strftime("%d/%m") for i in range(29, -1, -1)]
    monthly_labels = [_shift_month(month_start_local, -i).strftime("%m/%Y") for i in range(5, -1, -1)]

    events = (await session.scalars(select(Event).where(Event.tenant_id == tenant_id, Event.created_at >= month_start))).all()
    kinds = Counter(e.kind for e in events)
    new_month = kinds["new"]
    reactivated_month = kinds["reactivate"]
    churned_month = kinds["expired"] + kinds["removed"]
    active_at_start = max(active - new_month - reactivated_month + churned_month, 0)
    churn_rate = (churned_month / active_at_start * 100) if active_at_start else 0.0

    created_30 = await session.scalar(
        select(func.count(Payment.id)).where(
            Payment.tenant_id == tenant_id, Payment.created_at >= since_30, Payment.method != "manual"
        )
    )
    approved_30 = await session.scalar(
        select(func.count(Payment.id)).where(
            Payment.tenant_id == tenant_id,
            Payment.created_at >= since_30,
            Payment.method != "manual",
            Payment.status == "approved",
        )
    )

    upcoming = (
        await session.scalars(
            select(Member)
            .where(
                Member.tenant_id == tenant_id, Member.status == "active", Member.expires_at <= now + timedelta(days=7)
            )
            .order_by(Member.expires_at)
        )
    ).all()

    pending_manual = await session.scalar(
        select(func.count(Payment.id)).where(
            Payment.tenant_id == tenant_id, Payment.method == "pix_manual", Payment.status == "pending"
        )
    )

    return {
        "active": active,
        "recurring_active": recurring_active,
        "mrr_cents": mrr_cents,
        "revenue_month": revenue_month,
        "revenue_prev": revenue_prev,
        "foreign_month": dict(foreign_month),
        "sales_month": sales_month,
        "avg_ticket": revenue_month // sales_month if sales_month else 0,
        "new_month": new_month,
        "reactivated_month": reactivated_month,
        "renewals_month": kinds["renew"],
        "churned_month": churned_month,
        "churn_rate": churn_rate,
        "conversion": (approved_30 / created_30 * 100) if created_30 else 0.0,
        "pending_manual": pending_manual or 0,
        "upcoming": upcoming,
        "chart_daily": {
            "labels": daily_labels,
            "values": [daily[label] / 100 for label in daily_labels],
        },
        "chart_monthly": {
            "labels": monthly_labels,
            "values": [monthly[label] / 100 for label in monthly_labels],
        },
        "chart_method": {"labels": list(by_method), "values": [v / 100 for v in by_method.values()]},
        "chart_plan": {"labels": list(by_plan), "values": [v / 100 for v in by_plan.values()]},
    }


async def compute_platform(session: AsyncSession) -> dict:
    """Ganhos com as assinaturas da plataforma (só para o superadmin)."""
    now = utcnow()
    month_start_local = _month_start_local(to_local(now))
    month_start = local_to_utc(month_start_local.replace(tzinfo=None))
    prev_month_start = local_to_utc(_shift_month(month_start_local, -1).replace(tzinfo=None))
    six_months_start = local_to_utc(_shift_month(month_start_local, -5).replace(tzinfo=None))

    paid = (
        await session.scalars(
            select(PlatformPayment).where(
                PlatformPayment.status == "approved",
                PlatformPayment.method != "manual",  # dias dados pelo superadmin não são receita
                PlatformPayment.paid_at.is_not(None),
            )
        )
    ).all()
    revenue_month = sum(p.amount_cents for p in paid if p.paid_at >= month_start)
    sales_month = sum(1 for p in paid if p.paid_at >= month_start)
    revenue_prev = sum(p.amount_cents for p in paid if prev_month_start <= p.paid_at < month_start)
    monthly = defaultdict(int)
    for p in paid:
        if p.paid_at >= six_months_start:
            monthly[to_local(p.paid_at).strftime("%m/%Y")] += p.amount_cents
    monthly_labels = [_shift_month(month_start_local, -i).strftime("%m/%Y") for i in range(5, -1, -1)]

    last_paid = {}
    for p in sorted(paid, key=lambda p: p.paid_at):
        last_paid[p.user_id] = p
    plans = {p.code: p for p in (await session.scalars(select(PlatformPlan))).all()}

    users = (await session.scalars(select(User).where(User.is_superadmin.is_(False)))).all()
    counts = Counter()
    mrr_cents = 0
    for u in users:
        if not u.active:
            counts["disabled"] += 1
        elif u.exempt:
            counts["exempt"] += 1
        elif not u.subscription_active:
            counts["expired"] += 1
        elif u.id in last_paid:
            counts["paying"] += 1
            # Valor mensal: o plano da renovação automática ou o último pagamento
            plan = plans.get(u.platform_recurring_plan or "") if u.recurring_active else None
            price, days = (plan.price_cents, plan.days) if plan else (last_paid[u.id].amount_cents, last_paid[u.id].days)
            mrr_cents += round(price * 30 / days) if days else 0
        else:
            counts["trial"] += 1
        if u.active and u.recurring_active:
            counts["recurring"] += 1

    expiring = sorted(
        (
            u for u in users
            if u.active and not u.exempt and not u.recurring_active and u.subscription_until
            and now <= u.subscription_until <= now + timedelta(days=7)
        ),
        key=lambda u: u.subscription_until,
    )
    usernames = {u.id: u.username for u in users}
    recent = [
        {"username": usernames.get(p.user_id, "?"), "plan": plans[p.plan_code].name if p.plan_code in plans else p.plan_code,
         "amount_cents": p.amount_cents, "method": p.method, "paid_at": p.paid_at}
        for p in sorted(paid, key=lambda p: p.paid_at, reverse=True)[:10]
    ]
    open_tickets = await session.scalar(select(func.count(SupportTicket.id)).where(SupportTicket.status == "open"))

    return {
        "revenue_month": revenue_month,
        "revenue_prev": revenue_prev,
        "sales_month": sales_month,
        "revenue_total": sum(p.amount_cents for p in paid),
        "mrr_cents": mrr_cents,
        "users_total": len(users),
        "paying": counts["paying"],
        "trial": counts["trial"],
        "expired": counts["expired"],
        "exempt": counts["exempt"],
        "disabled": counts["disabled"],
        "recurring": counts["recurring"],
        "expiring": expiring,
        "recent": recent,
        "open_tickets": open_tickets or 0,
        "chart_monthly": {
            "labels": monthly_labels,
            "values": [monthly[label] / 100 for label in monthly_labels],
        },
    }
