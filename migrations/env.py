"""Alembic environment: uses the application's settings and models."""
from alembic import context
from sqlalchemy import engine_from_config, pool

from pitchforce import models  # noqa: F401  (register tables)
from pitchforce.config import get_settings
from pitchforce.db import Base

config = context.config
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(url=get_settings().db_url, target_metadata=target_metadata, literal_binds=True, render_as_batch=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = config.attributes.get("connection")
    if connectable is None:
        cfg = config.get_section(config.config_ini_section) or {}
        cfg["sqlalchemy.url"] = get_settings().db_url
        engine = engine_from_config(cfg, prefix="sqlalchemy.", poolclass=pool.NullPool)
        with engine.connect() as connection:
            context.configure(connection=connection, target_metadata=target_metadata, render_as_batch=True)
            with context.begin_transaction():
                context.run_migrations()
    else:
        context.configure(connection=connectable, target_metadata=target_metadata, render_as_batch=True)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
