import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

# Alembic Config object — gives access to alembic.ini values
config = context.config

# Wire Python logging from alembic.ini
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Import Base and all models so Alembic can detect schema changes
from app.core.config import settings  # noqa: E402
from app.db.session import Base, _normalize_database_url  # noqa: E402

# Models must be imported for autogenerate to pick them up.
from app.db import models as _models  # noqa: F401

target_metadata = Base.metadata

# Override the URL from the environment (ignores the blank value in alembic.ini).
# Strip PlanetScale / Aiven-style `?ssl-mode=…` query params and re-emit the
# cleaned URL plus the corresponding connect_args. SQLAlchemy's aiomysql
# dialect forwards query-string keys as kwargs to `aiomysql.connect()`,
# which doesn't accept `ssl-mode` — so we must drop it here.
_clean_url, _connect_args = _normalize_database_url(settings.DATABASE_URL)
config.set_main_option("sqlalchemy.url", _clean_url)
# Persist the connect_args so the async engine picks them up.
for _k, _v in _connect_args.items():
    config.set_main_option(f"sqlalchemy.{_k}", str(_v))


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode (no DB connection required)."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """Run migrations using an async engine."""
    section = config.get_section(config.config_ini_section, {})
    # Re-inject connect_args (set above) into the engine config so
    # aiomysql's connect() gets them as **kwargs.
    if _connect_args:
        section["connect_args"] = _connect_args
    connectable = async_engine_from_config(
        section,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
