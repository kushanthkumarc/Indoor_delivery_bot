import ssl
from collections.abc import AsyncGenerator

from sqlalchemy.engine.url import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.core.config import settings


def _build_ssl_arg(ssl_dict: dict) -> bool | ssl.SSLContext:
    """
    Convert a normalized ssl-params dict into an `ssl` arg suitable for
    aiomysql.connect().

    Always returns a proper `ssl.SSLContext` (never a dict, never the
    bare bool `True`). The context settings depend on the SSL mode:

      - `?ssl-mode=REQUIRED` (or any non-verify mode) → SSL on, cert
        verification OFF. This is the standard pattern for cloud MySQL
        providers (PlanetScale, Aiven, Render) whose self-signed certs
        are not in the system trust store. The connection is still
        encrypted; we just don't validate the server's identity.
      - `?ssl-mode=VERIFY_CA` or `?ssl-mode=VERIFY_IDENTITY` (or
        `?ssl-ca=...`) → full cert verification with the system trust
        store, optionally augmented by a custom CA file.

    A **dict** is NEVER returned — the asyncio SSL transport calls
    `ssl_context.wrap_bio(...)`, which raises
    `AttributeError: 'dict' object has no attribute 'wrap_bio'`.
    """
    if not ssl_dict:
        return False

    ssl_mode = (ssl_dict.get("ssl_mode") or "REQUIRED").upper()
    ssl_ca = ssl_dict.get("ssl_ca")

    # Always start from a real SSLContext so the connection is encrypted.
    ctx = ssl.create_default_context()

    if ssl_ca:
        ctx.load_verify_locations(cafile=ssl_ca)

    if ssl_mode in ("VERIFY_CA", "VERIFY_IDENTITY"):
        ctx.check_hostname = True
        ctx.verify_mode = ssl.CERT_REQUIRED
    else:
        # REQUIRED, PREFERRED, or no explicit mode → SSL on, cert verify
        # off. Required for cloud MySQL providers with self-signed certs.
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE

    return ctx


def _normalize_database_url(raw_url: str) -> tuple[str, dict]:
    """
    Sanitize a database URL for use with SQLAlchemy + aiomysql.

    Some managed MySQL providers (PlanetScale, Aiven, etc.) embed SSL params
    as query-string keys like `?ssl-mode=REQUIRED` or `?ssl-ca=/path/to/ca`.
    SQLAlchemy's MySQL/aiomysql dialect forwards every query-string key as
    a `**kwarg` to `aiomysql.connect()`, which does NOT accept `ssl-mode`
    (only `ssl` as a bool or `ssl.SSLContext`).

    This helper:
      1. Parses the URL with `sqlalchemy.engine.url.make_url`.
      2. Pops any `ssl-*` / `ssl_*` keys from the query.
      3. Returns a tuple of (clean_url, connect_args) where `connect_args`
         contains `{"ssl": <bool|SSLContext>}` — never a dict.

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
        ssl_arg = _build_ssl_arg(ssl_dict)
        if ssl_arg is not False:
            connect_args["ssl"] = ssl_arg
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
