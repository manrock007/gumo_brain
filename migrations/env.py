"""Alembic environment (Epic F1) — Postgres only.

This codebase has NO SQLAlchemy models, so target_metadata is None and every
revision is hand-written raw SQL (the baseline is generated from the live
SQLite schema by scripts/gen_pg_baseline.py). The DB URL comes from the app
settings' database_url — a SQLite install leaves it empty and never runs
Alembic at all.
"""

import os
import sys
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# resolve the Postgres URL from the app settings (env-driven), falling back to
# an explicit -x dburl=... override or the ALEMBIC_DATABASE_URL env var.
def _database_url() -> str:
    url = context.get_x_argument(as_dictionary=True).get("dburl")
    if url:
        return url
    url = os.environ.get("ALEMBIC_DATABASE_URL", "")
    if url:
        return url
    try:
        from app.config import get_settings
        return get_settings().database_url
    except Exception:
        return ""


target_metadata = None


def run_migrations_offline():
    url = _database_url()
    context.configure(url=url, target_metadata=target_metadata,
                      literal_binds=True, dialect_opts={"paramstyle": "named"})
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online():
    url = _database_url()
    if not url:
        raise RuntimeError(
            "No database_url configured — Alembic is Postgres-only. Set "
            "DATABASE_URL (postgresql://…) or pass -x dburl=...")
    cfg = config.get_section(config.config_ini_section) or {}
    cfg["sqlalchemy.url"] = url
    connectable = engine_from_config(cfg, prefix="sqlalchemy.", poolclass=pool.NullPool)
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
