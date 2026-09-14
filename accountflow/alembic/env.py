from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

# Import all models so Alembic sees their metadata
from app.config import get_settings
from app.database import Base
import app.models  # noqa: F401

config = context.config

# Migrations run as the table OWNER (the Postgres image's `accountflow` user);
# the API and worker run as the non-owner `accountflow_app` role so that
# row-level security applies to them (ADR-001, migration 010). Owner DSN from
# MIGRATION_DATABASE_URL, falling back to DATABASE_URL only for a database that
# predates the role split. Settings reads the environment first, then .env,
# and alembic.ini's placeholder is ignored.
settings = get_settings()
db_url = settings.migration_database_url or settings.database_url
# Alembic uses sync driver — swap asyncpg → psycopg2
sync_url = db_url.replace("postgresql+asyncpg://", "postgresql://")
config.set_main_option("sqlalchemy.url", sync_url)

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
