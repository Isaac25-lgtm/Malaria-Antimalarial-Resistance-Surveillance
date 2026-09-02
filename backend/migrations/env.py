"""Alembic migration environment.

Two things distinguish this from a default env.py:

* The database URL comes from settings, never from alembic.ini, so no
  environment's connection string is ever committed. A caller that has already
  set ``sqlalchemy.url`` on the config keeps it: that is how the integration
  tests migrate a disposable database without having to mutate the process
  environment first.
* Autogenerate is made schema-aware. ``include_schemas=True`` alone would also
  drag in PostGIS's own tables, so ``include_object`` restricts comparison to
  the MARS schemas.
"""

from __future__ import annotations

from logging.config import fileConfig
from typing import Any

from alembic import context
from sqlalchemy import engine_from_config, pool

from mars.core.settings import get_settings
from mars.db.base import Base
from mars.db.schemas import ALL_SCHEMAS

# Import every model so that autogenerate can see it.
import mars.db.models  # noqa: F401  # isort: skip

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata

# Settings are the default source, not an override. ``alembic.ini`` deliberately
# carries no ``sqlalchemy.url``, so anything present here was set by a caller in
# this process and states an intent more specific than the environment's.
# Clobbering it would silently migrate a different database than the one asked
# for, which is the kind of mistake that is only noticed after it has happened.
if not config.get_main_option("sqlalchemy.url", None):
    config.set_main_option("sqlalchemy.url", get_settings().database_url)

#: Tables PostGIS creates in the public schema. Never MARS's to manage.
_POSTGIS_TABLES = {"spatial_ref_sys", "geometry_columns", "geography_columns", "raster_columns"}


def include_object(
    obj: Any, name: str | None, type_: str, reflected: bool, compare_to: Any
) -> bool:
    """Restrict autogenerate to MARS-owned objects."""
    if type_ == "table":
        if name in _POSTGIS_TABLES:
            return False
        schema = getattr(obj, "schema", None)
        return schema in ALL_SCHEMAS
    return True


def include_name(name: str | None, type_: str, parent_names: dict[str, Any]) -> bool:
    """Restrict reflection to MARS schemas when comparing."""
    if type_ == "schema":
        return name in ALL_SCHEMAS
    return True


def run_migrations_offline() -> None:
    """Emit SQL without a live connection, for review before a production apply."""
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        include_schemas=True,
        include_object=include_object,
        include_name=include_name,
        version_table_schema=None,
        compare_type=True,
        compare_server_default=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Apply migrations against a live connection."""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            include_schemas=True,
            include_object=include_object,
            include_name=include_name,
            compare_type=True,
            compare_server_default=True,
            # Deterministic constraint names come from the metadata naming
            # convention; render_as_batch is off because PostgreSQL supports
            # in-place ALTER and batch mode would obscure the generated DDL.
            render_as_batch=False,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
