"""
Startup helpers: idempotent DB-migration runner.

Runs `alembic upgrade head` programmatically on application boot, so
deploys (Render, Docker, etc.) do not need a separate release-phase
command. Safe to call on every cold start — Alembic is a no-op when the
DB is already at the latest revision.

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

    Idempotent. Raises on failure — caller should treat that as fatal
    because subsequent code will assume the schema is in sync.
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

    logger.info("Running %s in %s", cmd, _PROJECT_ROOT)
    result = subprocess.run(
        cmd,
        cwd=_PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        logger.error("alembic upgrade FAILED:\n%s", result.stderr)
        raise RuntimeError(
            f"alembic upgrade head failed (rc={result.returncode}): {result.stderr}"
        )
    if result.stdout:
        logger.info("alembic output: %s", result.stdout.strip())
    logger.info("alembic upgrade head complete.")


def run_in_thread() -> None:
    """Blocking variant — call from `loop.run_in_executor(None, run_in_thread)`."""
    upgrade_head()
