"""
Startup helpers: idempotent DB-migration runner.

Runs `alembic upgrade head` programmatically on application boot, so
deploys (Render, Docker, etc.) do not need a separate release-phase
command. Safe to call on every cold start — Alembic is a no-op when the
DB is already at the latest revision.

Implementation note: the project's local `./alembic/` directory
(migration scripts) shadows the installed `alembic` library at import
time. To work around that, we use `alembic` as a subprocess with
`cwd` set to the project root — the `alembic.ini` file in the project
root directs the CLI to the local `alembic/` script directory, while
the alembic CLI itself is resolved from the system PATH (the installed
entry point, not the project shadow).
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess

logger = logging.getLogger(__name__)

_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


def upgrade_head() -> None:
    """
    Run `alembic upgrade head` synchronously via the alembic CLI binary.

    Idempotent. Raises on failure — caller should treat that as fatal
    because subsequent code will assume the schema is in sync.
    """
    alembic_bin = shutil.which("alembic")
    if alembic_bin is None:
        # Fall back to `python -m alembic` (only works when the project
        # `alembic/` directory is not on sys.path, e.g. inside a Docker
        # image that uses `WORKDIR /app`).
        import sys
        cmd = [sys.executable, "-m", "alembic", "upgrade", "head"]
    else:
        cmd = [alembic_bin, "upgrade", "head"]

    logger.info("Running %s in %s", cmd, _PROJECT_ROOT)
    result = subprocess.run(
        cmd,
        cwd=_PROJECT_ROOT,
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