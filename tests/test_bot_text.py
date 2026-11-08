from types import SimpleNamespace

from app.bot import handlers_member
from app.db import SessionLocal
from app.services.store import set_settings
from tests.tenant import TID


class FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, reply_markup=None):
        self.sent.append(text)


def _message(text, bot):
    async def answer(text, reply_markup=None):
        bot.sent.append(text)

    user = SimpleNamespace(id=555, username="fulano", first_name="Fulano")
    return SimpleNamespace(text=text, from_user=user, chat=SimpleNamespace(id=555), bot=bot, answer=answer)


async def test_greeting_opens_menu():
    bot = FakeBot()
    await handlers_member.msg_free_text(_message("Bom dia!!", bot), tenant_id=TID)
    assert len(bot.sent) == 1 and "Fulano" in bot.sent[0] and "Escolha um plano" in bot.sent[0]
    assert "{saudacao}" not in bot.sent[0]


async def test_unknown_text_reply_and_toggle():
    bot = FakeBot()
    await handlers_member.msg_free_text(_message("quanto custa o frete?", bot), tenant_id=TID)
    assert "não entendi" in bot.sent[0]

    async with SessionLocal() as session:
        await set_settings(session, TID, {"reply_unknown": "0"})
    bot = FakeBot()
    await handlers_member.msg_free_text(_message("quanto custa o frete?", bot), tenant_id=TID)
    assert bot.sent == []


async def test_status_without_subscription_lists_plans():
    answers = []

    async def answer(text, reply_markup=None):
        answers.append((text, reply_markup))

    user = SimpleNamespace(id=556, username=None, first_name="Bia")
    await handlers_member.cmd_status(SimpleNamespace(from_user=user, answer=answer), tenant_id=TID)
    text, markup = answers[0]
    assert "não tem uma assinatura ativa" in text
    buttons = [row[0].callback_data for row in markup.inline_keyboard]
    assert buttons == ["plan:mensal", "plan:mensal_recorrente", "plan:trimestral", "plan:semestral"]


async def test_messages_that_mentioned_start_now_show_plans(session, monkeypatch):
    from app.models import Member, Payment
    from app.services import notifier

    sent = []

    async def capture(tenant_id, chat_id, text, reply_markup=None):
        sent.append((text, reply_markup))
        return True

    monkeypatch.setattr(notifier, "send", capture)
    member = Member(tenant_id=TID, telegram_id=557, first_name="Caio")
    session.add(member)
    await session.flush()
    payment = Payment(tenant_id=TID, member_id=member.id, plan_code="mensal", amount_cents=2990, method="pix", external_ref="x-1")
    await notifier.payment_rejected(session, payment, member)

    text, markup = sent[0]
    assert "/start" not in text and "Escolha um plano abaixo" in text
    assert [row[0].callback_data for row in markup.inline_keyboard][0] == "plan:mensal"


def test_default_bot_messages_do_not_mention_start():
    from app.services.defaults import DEFAULT_MESSAGES

    assert [key for key, (_, text, _) in DEFAULT_MESSAGES.items() if "/start" in text] == []
