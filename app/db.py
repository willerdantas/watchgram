from sqlalchemy import event, text
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Session
from sqlalchemy.pool import NullPool

from app import config


class Base(DeclarativeBase):
    pass


IS_SQLITE = config.DATABASE_URL.startswith("sqlite")

# SQLite em arquivo: conexões baratas; NullPool evita prender conexões a um event loop.
# PostgreSQL: pool pequeno (um único processo) e teste da conexão antes de usar.
_engine_kwargs = {"poolclass": NullPool} if IS_SQLITE else {"pool_size": 5, "max_overflow": 5, "pool_pre_ping": True}
engine = create_async_engine(config.DATABASE_URL, **_engine_kwargs)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


async def init_db() -> None:
    from app import models  # noqa: F401  (registra as tabelas)
    from app.services import seed

    if IS_SQLITE:
        db_path = config.DATABASE_URL.split("///", 1)[-1]
        if db_path and db_path != ":memory:":
            from pathlib import Path

            Path(db_path).parent.mkdir(parents=True, exist_ok=True)

    use_app_role = False
    async with engine.begin() as conn:
        if not IS_SQLITE:
            await conn.exec_driver_sql("RESET ROLE")  # criar e migrar tabelas exige o usuário dono
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(_add_missing_columns)
        if not IS_SQLITE:
            await conn.run_sync(_enable_row_security)
            use_app_role = await conn.run_sync(_prepare_app_role)
    if use_app_role:
        _state["app_role"] = True
        await engine.dispose()  # as próximas conexões já nascem com o papel sem privilégios
    async with SessionLocal() as session:
        await seed.seed_defaults(session)


# Tabelas por área: no Postgres, além do filtro no código, o banco só entrega as linhas
# da área ligada à sessão (app.tenant_id). Sem área ligada (bot, agendador, webhooks,
# login) o acesso segue as regras do código.
TENANT_TABLES = ("settings", "plans", "message_templates", "members", "payments", "events", "custom_messages")
_RLS_RULE = (
    "coalesce(current_setting('app.tenant_id', true), '') = '' "
    "OR tenant_id::text = current_setting('app.tenant_id', true)"
)


# Superusuário (e quem tem BYPASSRLS) ignora a segurança em nível de linha, e o usuário criado
# pela imagem oficial do Postgres é superusuário. Nesse caso o app roda as consultas com um papel
# sem privilégios especiais; criar e migrar tabelas continua com o usuário dono (init_db).
APP_ROLE = "watchgram_app"
_state = {"app_role": False}


@event.listens_for(Engine, "connect")
def _use_app_role(dbapi_connection, connection_record) -> None:
    if _state["app_role"] and hasattr(dbapi_connection, "run_async"):
        dbapi_connection.run_async(lambda conn: conn.execute(f"SET ROLE {APP_ROLE}"))


def _prepare_app_role(sync_conn) -> bool:
    """Cria o papel do app quando o usuário do banco ignoraria a RLS. Retorna se deve usá-lo."""
    bypass = sync_conn.exec_driver_sql(
        "SELECT rolsuper OR rolbypassrls FROM pg_roles WHERE rolname = current_user"
    ).scalar()
    if not bypass:
        return False  # usuário comum: a RLS (com FORCE) já vale para ele
    if not sync_conn.exec_driver_sql(f"SELECT 1 FROM pg_roles WHERE rolname = '{APP_ROLE}'").scalar():
        sync_conn.exec_driver_sql(f"CREATE ROLE {APP_ROLE} NOLOGIN NOSUPERUSER NOBYPASSRLS NOCREATEROLE NOCREATEDB")
    sync_conn.exec_driver_sql(f"GRANT USAGE ON SCHEMA public TO {APP_ROLE}")
    sync_conn.exec_driver_sql(f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {APP_ROLE}")
    sync_conn.exec_driver_sql(f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {APP_ROLE}")
    return True


def _enable_row_security(sync_conn) -> None:
    for table in TENANT_TABLES:
        sync_conn.exec_driver_sql(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        sync_conn.exec_driver_sql(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")  # vale também para o dono
        sync_conn.exec_driver_sql(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")
        sync_conn.exec_driver_sql(
            f"CREATE POLICY tenant_isolation ON {table} USING ({_RLS_RULE}) WITH CHECK ({_RLS_RULE})"
        )


@event.listens_for(Session, "after_begin")
def _apply_tenant(session, transaction, connection) -> None:
    tenant_id = session.info.get("tenant_id")
    if tenant_id is not None and not IS_SQLITE:
        connection.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(tenant_id)})


async def bind_tenant(session: AsyncSession, tenant_id: int) -> None:
    """Liga a sessão a uma área: no Postgres, o banco passa a esconder as linhas das outras."""
    session.info["tenant_id"] = tenant_id
    if session.in_transaction() and not IS_SQLITE:
        await session.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(tenant_id)})


def _add_missing_columns(sync_conn) -> None:
    """create_all não altera tabelas existentes: adiciona colunas novas anuláveis.

    Basta para evoluções simples (novos campos opcionais) sem ferramenta de migração.
    """
    from sqlalchemy import inspect

    inspector = inspect(sync_conn)
    for table in Base.metadata.sorted_tables:
        if not inspector.has_table(table.name):
            continue
        existing = {c["name"] for c in inspector.get_columns(table.name)}
        for column in table.columns:
            if column.name in existing:
                continue
            if not column.nullable:
                raise RuntimeError(f"Coluna obrigatória nova {table.name}.{column.name}: migre o banco manualmente.")
            ddl = column.type.compile(dialect=sync_conn.dialect)
            sync_conn.exec_driver_sql(f'ALTER TABLE {table.name} ADD COLUMN {column.name} {ddl}')
