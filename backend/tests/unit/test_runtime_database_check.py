"""The startup probe must enforce every grant without accessing protected data."""

import runpy
from pathlib import Path
from unittest.mock import MagicMock

import pytest

CHECK = runpy.run_path(
    str(Path(__file__).resolve().parents[3] / "scripts/check-live-runtime-databases.py")
)["_check_live_sync_privileges"]


@pytest.mark.parametrize(
    ("label", "permissions", "accepted"),
    [
        ("application", [True, True, True, True], True),
        ("application", [True, False, False, False], False),
        ("application", [True, True, True, False], False),
        ("identity", [False, False, False, False], True),
        ("identity", [False, False, True, False], False),
    ],
)
def test_live_sync_permissions(label: str, permissions: list[bool], accepted: bool) -> None:
    connection = MagicMock()
    oid_result = MagicMock()
    oid_result.scalar_one_or_none.return_value = 42
    results = [oid_result]
    for allowed in permissions:
        result = MagicMock()
        result.scalar_one.return_value = allowed
        results.append(result)
    connection.execute.side_effect = results
    if accepted:
        CHECK(connection, label)
    else:
        with pytest.raises(RuntimeError):
            CHECK(connection, label)


def test_missing_live_sync_table_fails_closed() -> None:
    connection = MagicMock()
    connection.execute.return_value.scalar_one_or_none.return_value = None
    with pytest.raises(RuntimeError, match="table is missing"):
        CHECK(connection, "identity")
