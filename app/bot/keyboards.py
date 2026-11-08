from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.models import Plan
from app.services.store import Settings
from app.utils import brl, money

METHOD_LABELS = {
    "pix": "Pix (automático)",
    "pix_manual": "Pix (chave + comprovante)",
    "card": "Cartão de crédito",
    "card_recurring": "Cartão (recorrente)",
    "paypal": "PayPal",
    "manual": "Liberação manual",
}


def plans_kb(plans: list[Plan]) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=f"{p.name} — {brl(p.price_cents)}", callback_data=f"plan:{p.code}")]
        for p in plans
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def available_methods(plan: Plan, settings: Settings) -> list[str]:
    has_mp = bool(settings.get("mp_access_token"))
    if plan.recurring:
        return ["card_recurring"] if has_mp and settings.flag("enable_card") else []
    methods = []
    if has_mp and settings.flag("enable_pix_auto"):
        methods.append("pix")
    if settings.flag("enable_pix_manual") and settings.get("pix_key"):
        methods.append("pix_manual")
    if has_mp and settings.flag("enable_card"):
        methods.append("card")
    if plan.foreign_price_cents and settings.flag("enable_paypal") and (
        settings.get("paypal_client_id") and settings.get("paypal_client_secret")
    ):
        methods.append("paypal")
    return methods


def methods_kb(plan: Plan, methods: list[str], currency: str = "USD") -> InlineKeyboardMarkup:
    # O PayPal cobra em outra moeda: o valor precisa aparecer já no botão.
    labels = {**METHOD_LABELS, "paypal": f"PayPal — {money(plan.foreign_price_cents, currency)}"}
    rows = [
        [InlineKeyboardButton(text=labels[m], callback_data=f"pay:{plan.code}:{m}")] for m in methods
    ]
    rows.append([InlineKeyboardButton(text="« Voltar", callback_data="plans")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def url_kb(text: str, url: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=text, url=url)]])


def menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="📋 Ver planos", callback_data="plans")]])


def renew_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔄 Renovar agora", callback_data="plans")]])


def pix_check_kb(payment_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="Já paguei, verificar", callback_data=f"check:{payment_id}")]]
    )


def manual_review_kb(payment_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Aprovar", callback_data=f"manual:approve:{payment_id}"),
                InlineKeyboardButton(text="❌ Recusar", callback_data=f"manual:reject:{payment_id}"),
            ]
        ]
    )
