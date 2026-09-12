"""Version-aware dispatch of governed recurrence computation.

Governed recurrence has two engines and a method version says which one it
means:

* the **episode engine** (``analytics/episodes.py`` and ``analytics/recurrence.py``,
  engine ``1.0.0``) counts positives within attendance-gap episodes. Its builds
  and results keep that meaning and stay reproducible; nothing here touches
  them;
* the **positive-to-positive engine** (``positive-recurrence/...``) is the
  shared engine every new dashboard output uses. A governed method version whose
  parameters name it dispatches to a programme analysis run that freezes the
  definition and records the engine version.

A method version without an engine version is historical and stays on the
episode engine. Nothing is reinterpreted silently.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal

from mars.domain.longitudinal import ENGINE_VERSION

EngineFamily = Literal["episode", "positive"]
POSITIVE_PREFIX = "positive-recurrence/"


def engine_family(parameters: Mapping[str, Any] | None) -> EngineFamily:
    """Which engine a governed method version's parameters name."""
    engine = str((parameters or {}).get("engine_version") or "")
    return "positive" if engine.startswith(POSITIVE_PREFIX) else "episode"


def runs_on_this_engine(parameters: Mapping[str, Any] | None) -> bool:
    """Whether a positive-engine method version matches the deployed engine."""
    return str((parameters or {}).get("engine_version") or "") == ENGINE_VERSION


__all__ = ["POSITIVE_PREFIX", "EngineFamily", "engine_family", "runs_on_this_engine"]
