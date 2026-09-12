"""Version dispatch keeps historical and positive recurrence engines separate."""

from __future__ import annotations

import uuid
from datetime import date
from types import SimpleNamespace
from unittest.mock import Mock

from sqlalchemy.orm import Session

from mars.analytics.recurrence import RecurrenceReport
from mars.domain.longitudinal import ENGINE_VERSION
from mars.services.recurrence_dispatch import engine_family, runs_on_this_engine
from mars.workers import recurrence_compute


def test_engine_family_defaults_historical_methods_to_episode_semantics() -> None:
    assert engine_family(None) == "episode"
    assert engine_family({}) == "episode"
    assert engine_family({"engine_version": "1.0.0"}) == "episode"


def test_engine_family_recognises_positive_versions_but_only_runs_the_exact_version() -> None:
    assert engine_family({"engine_version": ENGINE_VERSION}) == "positive"
    assert engine_family({"engine_version": "positive-recurrence/9.9.9"}) == "positive"
    assert runs_on_this_engine({"engine_version": ENGINE_VERSION}) is True
    assert runs_on_this_engine({"engine_version": "positive-recurrence/9.9.9"}) is False


def test_worker_keeps_a_historical_method_on_the_episode_path(monkeypatch) -> None:
    session = Mock(spec=Session)
    version = SimpleNamespace(parameters={"engine_version": "1.0.0"})
    session.get.return_value = version
    positive = Mock(side_effect=AssertionError("historical method reached positive engine"))
    monkeypatch.setattr(recurrence_compute, "_positive", positive)
    monkeypatch.setattr(recurrence_compute, "latest_build", lambda *_args: None)

    result = recurrence_compute.run(
        session,
        period_start=date(2026, 8, 1),
        period_end=date(2026, 8, 31),
        method_version_id=uuid.uuid4(),
    )

    assert isinstance(result, RecurrenceReport)
    positive.assert_not_called()


def test_worker_routes_the_deployed_positive_version_to_the_positive_engine(monkeypatch) -> None:
    session = Mock(spec=Session)
    version = SimpleNamespace(parameters={"engine_version": ENGINE_VERSION})
    session.get.return_value = version
    expected = object()
    positive = Mock(return_value=expected)
    monkeypatch.setattr(recurrence_compute, "_positive", positive)
    start = date(2026, 8, 1)
    end = date(2026, 8, 31)

    result = recurrence_compute.run(
        session,
        period_start=start,
        period_end=end,
        method_version_id=uuid.uuid4(),
    )

    assert result is expected
    positive.assert_called_once_with(session, version, start, end, None)
