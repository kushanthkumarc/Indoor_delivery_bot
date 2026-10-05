from collections.abc import AsyncGenerator

from sqlalchemy.engine.url import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.core.config import settings


def _normalize_database_url(raw_url: str) -> tuple[str, dict]:
    """
    Sanitize a database URL for use with SQLAlchemy + aiomysql.

    Some managed MySQL providers (PlanetScale, Aiven, etc.) embed SSL params
    as query-string keys like `?ssl-mode=REQUIRED` or `?ssl-ca=/path/to/ca`.
    SQLAlchemy's MySQL/aiomysql dialect forwards every query-string key as
    a `**kwarg` to `aiomysql.connect()`, which does NOT accept `ssl-mode`
    (only `ssl` as a dict).

    This helper:
      1. Parses the URL with `sqlalchemy.engine.url.make_url`.
      2. Pops any `ssl-*` / `ssl_*` keys from the query.
      3. Returns a tuple of (clean_url, connect_args) where `connect_args`
         contains an aiomysql-compatible `{"ssl": {...}}` dict.

    For URLs that already work locally (no SSL params), the function
    returns the URL unchanged and `connect_args={}`.
    """
    url = make_url(raw_url)
    query = dict(url.query)

    # Pop every SSL-related key (hyphen OR underscore form).
    ssl_keys = [
        "ssl-mode", "ssl_mode",
        "ssl-ca", "ssl_ca",
        "ssl-cert", "ssl_cert",
        "ssl-key", "ssl_key",
    ]
    ssl_dict: dict = {}
    for k in ssl_keys:
        if k in query:
            # Convert hyphens to underscores in the dict keys for aiomysql.
            norm_key = k.replace("-", "_")
            ssl_dict[norm_key] = query.pop(k)

    # Render the URL with the remaining query params.
    clean_url = url.set(query={k: v for k, v in query.items()}).render_as_string(
        hide_password=False
    )

    connect_args: dict = {}
    if ssl_dict:
        connect_args["ssl"] = ssl_dict
    return clean_url, connect_args


_clean_db_url, _connect_args = _normalize_database_url(settings.DATABASE_URL)

engine = create_async_engine(
    _clean_db_url,
    echo=settings.DEBUG,
    pool_pre_ping=False,
    pool_size=10,
    max_overflow=20,
    connect_args=_connect_args,
)

AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autocommit=False,
    autoflush=False,
)


class Base(DeclarativeBase):
    pass


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency that provides a database session per request."""
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
