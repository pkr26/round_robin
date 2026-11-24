from logging.config import fileConfig
import os

from alembic import context
from sqlalchemy import engine_from_config, pool

from round_robin.database.models import Base
from round_robin.database import visual_models, claim_models

config = context.config
if config.config_file_name:
    fileConfig(config.config_file_name, disable_existing_loggers=False)
if config.attributes.get("url_override"):
    config.set_main_option("sqlalchemy.url", config.attributes["url_override"].replace("%", "%%"))
elif os.environ.get("ROUND_ROBIN_DATABASE_URL"):
    config.set_main_option("sqlalchemy.url", os.environ["ROUND_ROBIN_DATABASE_URL"].replace("%", "%%"))
target_metadata = Base.metadata


def offline():
    context.configure(url=config.get_main_option("sqlalchemy.url"), target_metadata=target_metadata, literal_binds=True, dialect_opts={"paramstyle": "named"})
    with context.begin_transaction():
        context.run_migrations()


def online():
    connectable = engine_from_config(config.get_section(config.config_ini_section), prefix="sqlalchemy.", poolclass=pool.NullPool, hide_parameters=True)
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


offline() if context.is_offline_mode() else online()
