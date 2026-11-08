import os
import tempfile
from pathlib import Path

_tmp = Path(tempfile.mkdtemp(prefix="bottest-"))
# TEST_DATABASE_URL=postgresql+asyncpg://... roda os testes no PostgreSQL (banco descartável!).
os.environ["DATABASE_URL"] = os.getenv("TEST_DATABASE_URL") or f"sqlite+aiosqlite:///{_tmp / 'test.db'}"
os.environ["ADMIN_USER"] = "admin"
os.environ["ADMIN_PASSWORD"] = "senha-teste"
os.environ["SECRET_KEY"] = "chave-de-teste-" + "x" * 32
os.environ["DISABLE_BACKGROUND"] = "1"

import pytest  # noqa: E402

from app import runtime  # noqa: E402
from sqlalchemy.ext.asyncio import create_async_engine  # noqa: E402
from sqlalchemy.pool import NullPool  # noqa: E402

from app import db  # noqa: E402

# Cada teste roda em um event loop próprio: sem pool, nenhuma conexão passa de um loop para outro.
db.engine = create_async_engine(os.environ["DATABASE_URL"], poolclass=NullPool)
db.SessionLocal.configure(bind=db.engine)
from app.db import Base, SessionLocal, engine, init_db  # noqa: E402
from app.services import notifier  # noqa: E402


@pytest.fixture(autouse=True)
async def fresh_db():
    async with engine.begin() as conn:
        if not db.IS_SQLITE:
            await conn.exec_driver_sql("RESET ROLE")  # apagar tabelas exige o usuário dono
        await conn.run_sync(Base.metadata.drop_all)
    await init_db()
    yield


@pytest.fixture
async def other_tenant():
    """Segundo usuário, com área própria (para testar o isolamento)."""
    from app.models import User
    from app.services.passwords import hash_password
    from app.services.seed import seed_tenant

    async with SessionLocal() as s:
        user = User(username="outro", password_hash=hash_password("senha-do-outro"), name="Área do outro")
        s.add(user)
        await s.commit()
        await seed_tenant(s, user.id)
        return user.id


@pytest.fixture
async def session():
    async with SessionLocal() as s:
        yield s


@pytest.fixture(autouse=True)
def sent_messages(monkeypatch):
    """Captura mensagens em vez de chamar o Telegram."""
    sent: list[tuple[int, str]] = []

    async def fake_send(tenant_id, chat_id, text, reply_markup=None):
        sent.append((chat_id, text))
        return True

    async def fake_invite(settings, member):
        return "https://t.me/+convite-teste"

    async def fake_kick(session, settings, member):
        member.in_group = False
        return True

    monkeypatch.setattr(notifier, "send", fake_send)
    monkeypatch.setattr(notifier, "create_invite", fake_invite)
    monkeypatch.setattr(notifier, "kick", fake_kick)
    runtime.bots.clear()
    runtime.usernames.clear()
    runtime.errors.clear()
    return sent
