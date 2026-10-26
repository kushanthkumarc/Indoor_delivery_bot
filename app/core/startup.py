"""
Optional migration helper for explicit local/single-process use.

Production migrations should run once as a controlled release step rather
than from each application worker.

We invoke the alembic CLI via subprocess rather than importing
`alembic.command` directly. Reason: the project's local `./alembic/`
directory (migration scripts) shadows the installed `alembic` library
at import time, and Alembic's internal relative imports do not survive
loading under a different module name.

PlanetScale / Aiven compatibility: the user's `DATABASE_URL` may end
with `?ssl-mode=REQUIRED` (PlanetScale convention) or similar SSL
query-string keys. SQLAlchemy's MySQL/aiomysql dialect forwards those
as kwargs to `aiomysql.connect()`, which does not accept them. We
normalize the URL via `app.db.session._normalize_database_url` before
spawning the subprocess.
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess

from app.core.config import settings

logger = logging.getLogger(__name__)

_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


def upgrade_head() -> None:
    """
    Run `alembic upgrade head` synchronously via the alembic CLI binary.

    Idempotent. Raises on failure; production callers should use a
    controlled single migration runner, not concurrent web workers.
    """
    # Import inside the function so this module is import-safe even if
    # `app.db.session` has not finished initialising yet.
    from app.db.session import _normalize_database_url

    clean_url, _connect_args = _normalize_database_url(settings.DATABASE_URL)

    alembic_bin = shutil.which("alembic")
    if alembic_bin is None:
        # Fall back to `python -m alembic` (only works when the project
        # `alembic/` directory is not on sys.path).
        import sys
        cmd = [sys.executable, "-m", "alembic", "upgrade", "head"]
    else:
        cmd = [alembic_bin, "upgrade", "head"]

    # Pass the cleaned URL via env so it overrides whatever alembic.ini
    # contains (and so the subprocess doesn't see ssl-mode=…).
    env = os.environ.copy()
    env["DATABASE_URL"] = clean_url

    logger.info("Running schema migration command in %s", _PROJECT_ROOT)
    result = subprocess.run(
        cmd,
        cwd=_PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )
    if result.returncode != 0:
        logger.error("Schema migration failed (exit code %s); command output suppressed", result.returncode)
        raise RuntimeError(
            f"alembic upgrade head failed (rc={result.returncode})"
        )
    logger.info("alembic upgrade head complete.")


def run_in_thread() -> None:
    """Blocking variant — call from `loop.run_in_executor(None, run_in_thread)`."""
    upgrade_head()
