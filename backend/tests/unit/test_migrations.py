"""Structural checks on the migration set.

These run without a database. They catch the drift that would otherwise only
surface when a migration is applied to a real server:

* a model added without a corresponding table in a migration,
* an identifier PostgreSQL would refuse,
* a migration with no downgrade,
* a broken or branched revision chain.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from mars.db.models import Base
from mars.db.schemas import ALL_SCHEMAS

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations" / "versions"
ENV_PY = MIGRATIONS_DIR.parent / "env.py"

#: PostgreSQL truncates identifiers at NAMEDATALEN - 1.
MAX_IDENTIFIER_LENGTH = 63


def _migration_files() -> list[Path]:
    return sorted(p for p in MIGRATIONS_DIR.glob("*.py") if not p.name.startswith("_"))


def _module_source() -> str:
    return "\n".join(p.read_text(encoding="utf-8") for p in _migration_files())


def _enum_calls() -> list[tuple[str, str, bool]]:
    """Every ``postgresql.ENUM(...)`` call, classified by what it does.

    Returns ``(source, kind, disclaims_creation)`` where kind is ``create`` for
    an explicit type creation, ``drop`` for a teardown, and ``column`` for a
    column type reference.
    """
    source = _module_source()
    calls: list[tuple[str, str, bool]] = []
    index = 0
    marker = "postgresql.ENUM("
    while (start := source.find(marker, index)) != -1:
        depth, cursor = 1, start + len(marker)
        while cursor < len(source) and depth:
            if source[cursor] == "(":
                depth += 1
            elif source[cursor] == ")":
                depth -= 1
            cursor += 1
        call = source[start:cursor]
        tail = source[cursor : cursor + 40]
        if tail.startswith(".create("):
            kind = "create"
        elif tail.startswith(".drop("):
            kind = "drop"
        else:
            kind = "column"
        calls.append((call, kind, "create_type=False" in call))
        index = cursor
    return calls


class TestRevisionChain:
    def test_migrations_exist(self) -> None:
        assert _migration_files(), "no migration files found"

    def test_every_migration_declares_a_revision(self) -> None:
        for path in _migration_files():
            source = path.read_text(encoding="utf-8")
            assert re.search(r'^revision: str = "', source, re.M), (
                f"{path.name} does not declare a revision identifier"
            )

    def test_revision_chain_is_linear_and_complete(self) -> None:
        revisions: dict[str, str | None] = {}
        for path in _migration_files():
            source = path.read_text(encoding="utf-8")
            rev = re.search(r'^revision: str = "([^"]+)"', source, re.M)
            down = re.search(r'^down_revision: str \| None = (None|"[^"]+")', source, re.M)
            assert rev and down, f"{path.name} is missing revision metadata"
            down_value = down.group(1)
            revisions[rev.group(1)] = None if down_value == "None" else down_value.strip('"')

        roots = [r for r, d in revisions.items() if d is None]
        assert len(roots) == 1, f"expected exactly one root revision, found {roots}"

        heads = set(revisions) - {d for d in revisions.values() if d is not None}
        assert len(heads) == 1, f"expected exactly one head revision, found {heads}"

        for revision, down in revisions.items():
            if down is not None:
                assert down in revisions, f"{revision} points at unknown down_revision {down}"

    def test_every_migration_has_a_real_downgrade(self) -> None:
        """A migration that cannot be reversed cannot safely be applied."""
        for path in _migration_files():
            tree = ast.parse(path.read_text(encoding="utf-8"))
            downgrade = next(
                (
                    node
                    for node in tree.body
                    if isinstance(node, ast.FunctionDef) and node.name == "downgrade"
                ),
                None,
            )
            assert downgrade is not None, f"{path.name} has no downgrade()"
            body = list(downgrade.body)
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                body = body[1:]
            assert body and not all(isinstance(n, ast.Pass) for n in body), (
                f"{path.name} has an empty downgrade()"
            )


class TestModelMigrationParity:
    def test_every_model_table_is_created_by_a_migration(self) -> None:
        """A model without a migration would never reach the database."""
        source = _module_source()
        missing = [
            table.name
            for table in Base.metadata.tables.values()
            if f'op.create_table(\n        "{table.name}"' not in source
            and f'op.create_table("{table.name}"' not in source
        ]
        assert not missing, f"tables with no create_table in any migration: {missing}"

    def test_every_created_table_belongs_to_a_model(self) -> None:
        """A migration creating a table no model knows about is dead weight."""
        source = _module_source()
        created = set(re.findall(r'op\.create_table\(\s*"([a-z_]+)"', source))
        modelled = {t.name for t in Base.metadata.tables.values()}
        orphans = created - modelled
        assert not orphans, f"tables created but not modelled: {orphans}"

    def test_all_schemas_are_created(self) -> None:
        source = _module_source()
        for schema in ALL_SCHEMAS:
            assert schema in source, f"{schema} is never created by a migration"

    def test_identity_schema_boundary_exists_from_the_first_migration(self) -> None:
        """The identity boundary must not be retrofitted."""
        first = _migration_files()[0].read_text(encoding="utf-8")
        assert "mars_identity" in first

    def test_postgis_geometry_columns_and_indexes_are_migrated(self) -> None:
        source = _module_source()
        geometry = Base.metadata.tables["mars_core.geography_unit_geometry"]
        assert {"geom", "geom_web"} <= set(geometry.columns.keys())
        assert "CREATE EXTENSION IF NOT EXISTS postgis" in source
        assert '"geom"' in source and '"geom_web"' in source
        assert 'postgresql_using="gist"' in source

    def test_hierarchy_cycle_guards_are_installed_and_removed(self) -> None:
        source = _module_source()
        assert "CREATE OR REPLACE FUNCTION mars_core.reject_hierarchy_cycle" in source
        assert "CREATE TRIGGER geography_unit_reject_cycle" in source
        assert "CREATE TRIGGER organisation_unit_reject_cycle" in source
        assert "DROP TRIGGER IF EXISTS geography_unit_reject_cycle" in source
        assert "DROP TRIGGER IF EXISTS organisation_unit_reject_cycle" in source


class TestIdentifierLengths:
    """PostgreSQL silently truncates over-long identifiers; SQLAlchemy raises."""

    def test_no_table_name_is_too_long(self) -> None:
        for table in Base.metadata.tables.values():
            assert len(table.name) <= MAX_IDENTIFIER_LENGTH, table.name

    def test_no_column_name_is_too_long(self) -> None:
        for table in Base.metadata.tables.values():
            for column in table.columns:
                assert len(column.name) <= MAX_IDENTIFIER_LENGTH, f"{table.name}.{column.name}"

    def test_no_constraint_name_is_too_long(self) -> None:
        offenders: list[tuple[str, str, int]] = []
        for table in Base.metadata.tables.values():
            for constraint in table.constraints:
                name = getattr(constraint, "name", None)
                if isinstance(name, str) and len(name) > MAX_IDENTIFIER_LENGTH:
                    offenders.append((table.fullname, name, len(name)))
        assert not offenders, f"constraint names over {MAX_IDENTIFIER_LENGTH} chars: {offenders}"

    def test_no_index_name_is_too_long(self) -> None:
        offenders: list[tuple[str, str, int]] = []
        for table in Base.metadata.tables.values():
            for index in table.indexes:
                if index.name and len(index.name) > MAX_IDENTIFIER_LENGTH:
                    offenders.append((table.fullname, index.name, len(index.name)))
        assert not offenders, f"index names over {MAX_IDENTIFIER_LENGTH} chars: {offenders}"


class TestEnumTypeHandling:
    def test_enum_columns_do_not_recreate_their_type(self) -> None:
        """The migration creates each enum type once, explicitly.

        Without ``create_type=False`` on the column definitions, a shared type
        such as ``lifecycle_status`` would be created twice and the second
        CREATE TYPE would fail on a real apply.
        """
        offenders = [
            call[:80].replace("\n", " ")
            for call, kind, disclaims in _enum_calls()
            if kind == "column" and not disclaims
        ]
        assert not offenders, (
            f"enum column definitions that would re-create their type: {offenders}"
        )

    def test_every_enum_type_is_dropped_on_downgrade(self) -> None:
        calls = _enum_calls()
        creates = [c for c in calls if c[1] == "create"]
        drops = [c for c in calls if c[1] == "drop"]
        assert len(creates) == len(drops), (
            f"{len(creates)} enum types created but {len(drops)} dropped; "
            "downgrade would leak types"
        )

    def test_every_enum_column_type_is_explicitly_created(self) -> None:
        """A column referencing a type nothing creates would fail to apply."""
        import re

        calls = _enum_calls()
        created = {
            re.search(r'name="([a-z_]+)"', c[0]).group(1)  # type: ignore[union-attr]
            for c in calls
            if c[1] == "create"
        }
        referenced = {
            re.search(r'name="([a-z_]+)"', c[0]).group(1)  # type: ignore[union-attr]
            for c in calls
            if c[1] == "column"
        }
        assert referenced <= created, f"types referenced but never created: {referenced - created}"


class TestModelConventions:
    """Blueprint appendix 159 conventions, asserted rather than assumed."""

    def test_every_table_has_a_uuid_primary_key(self) -> None:
        for table in Base.metadata.tables.values():
            pk_columns = list(table.primary_key.columns)
            assert len(pk_columns) == 1, f"{table.fullname} has a composite primary key"
            assert pk_columns[0].name == "id", f"{table.fullname} primary key is not 'id'"
            assert "UUID" in str(pk_columns[0].type).upper(), (
                f"{table.fullname}.id is {pk_columns[0].type}, not UUID"
            )

    def test_every_table_lives_in_a_mars_schema(self) -> None:
        for table in Base.metadata.tables.values():
            assert table.schema in ALL_SCHEMAS, (
                f"{table.name} is in schema {table.schema!r}, not a MARS schema"
            )

    def test_all_names_are_snake_case(self) -> None:
        pattern = re.compile(r"^[a-z][a-z0-9_]*$")
        for table in Base.metadata.tables.values():
            assert pattern.match(table.name), table.name
            for column in table.columns:
                assert pattern.match(column.name), f"{table.name}.{column.name}"

    def test_timestamp_columns_are_timezone_aware(self) -> None:
        """A naive timestamp is how a reporting period silently shifts a day."""
        for table in Base.metadata.tables.values():
            for column in table.columns:
                type_name = type(column.type).__name__
                if type_name == "DateTime":
                    assert column.type.timezone, f"{table.fullname}.{column.name} is timezone-naive"

    def test_foreign_key_columns_end_in_id(self) -> None:
        for table in Base.metadata.tables.values():
            for column in table.columns:
                if column.foreign_keys:
                    assert column.name.endswith("_id"), (
                        f"{table.fullname}.{column.name} is a foreign key not ending in _id"
                    )

    def test_no_overloaded_generic_status_column(self) -> None:
        """Appendix 159: avoid a 'status' field with several unrelated meanings.

        Each lifecycle gets its own named column: import_status, match_status,
        validity_state, outcome.
        """
        for table in Base.metadata.tables.values():
            names = {c.name for c in table.columns}
            if "status" in names:
                # The governance registries use 'status' for exactly one
                # lifecycle each, which is the permitted case.
                assert table.schema == "mars_governance", (
                    f"{table.fullname} has a generic 'status' column"
                )


@pytest.mark.parametrize("schema", ALL_SCHEMAS)
def test_schema_is_documented(schema: str) -> None:
    from mars.db.schemas import SCHEMA_PURPOSE

    assert schema in SCHEMA_PURPOSE
    assert SCHEMA_PURPOSE[schema].strip()


class TestMigrationEnvironmentTargetsTheRequestedDatabase:
    """A caller's explicit database URL must survive ``env.py``.

    ``env.py`` reads the URL from settings so that no connection string is
    committed. Applying that unconditionally was a real defect: a caller that
    had already set ``sqlalchemy.url`` on the config - which is how the
    integration fixtures point Alembic at a disposable database - had its choice
    silently replaced by whatever the environment happened to hold, and the
    migration ran against a different server than the one requested.

    The failure is quiet and destructive in the wrong direction, so the guard is
    asserted structurally rather than left to be noticed.
    """

    def _tree(self) -> ast.Module:
        return ast.parse(ENV_PY.read_text(encoding="utf-8"))

    def _set_main_option_calls(self, tree: ast.Module) -> list[ast.Call]:
        """Every ``config.set_main_option("sqlalchemy.url", ...)`` in the tree.

        The tree is passed in rather than re-parsed, because the caller compares
        these nodes by identity against nodes from the same parse.
        """
        return [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "set_main_option"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and node.args[0].value == "sqlalchemy.url"
        ]

    def test_the_url_is_assigned_exactly_once(self) -> None:
        calls = self._set_main_option_calls(self._tree())
        assert len(calls) == 1, (
            f"env.py sets sqlalchemy.url {len(calls)} times; one assignment keeps "
            "the precedence between caller and settings readable"
        )

    def test_the_assignment_is_conditional(self) -> None:
        """The settings URL is a fallback, never an override."""
        tree = self._tree()
        call = self._set_main_option_calls(tree)[0]

        guarded = any(call in ast.walk(node) for node in ast.walk(tree) if isinstance(node, ast.If))
        assert guarded, (
            "env.py assigns sqlalchemy.url unconditionally, so it overwrites a URL "
            "the caller already set and migrates the wrong database"
        )

    def test_alembic_ini_declares_no_url(self) -> None:
        """The guard above is only safe while the ini contributes no URL.

        If ``alembic.ini`` gained a ``sqlalchemy.url``, that placeholder would
        win over settings for every ordinary CLI invocation.
        """
        ini = ENV_PY.parent.parent / "alembic.ini"
        declared = [
            line
            for line in ini.read_text(encoding="utf-8").splitlines()
            if line.strip().startswith("sqlalchemy.url")
        ]
        assert not declared, (
            f"alembic.ini declares a database URL {declared}; env.py treats any "
            "preset URL as a deliberate caller override, so the ini must stay silent"
        )
