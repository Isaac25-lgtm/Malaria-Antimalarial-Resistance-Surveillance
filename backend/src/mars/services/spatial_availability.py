"""What a map cell is allowed to say, and what it must refuse to say.

A blank cell on a malaria map is read as *no malaria here*. It almost never
means that. It can mean the facility did not report, that the measure has no
denominator, that a real value was withheld because the cell was too small,
that the viewer is not authorised for that district, or that MARS has no
approved rule for showing patient-derived detail at all. Six situations, one
colour, and only one of them is good news.

So this service never returns a bare number. Every cell carries a status, and
the six statuses are kept apart all the way to the caller.

**Refusal is structured, not silent, and not total.** Patient-derived spatial
detail requires an approved privacy policy: a minimum cell count and a minimum
aggregation level. Without them this service refuses that detail and says
exactly which keys are missing. It does not fabricate zeroes, and it does not
return an empty map that looks like an absence of disease.

It also does not disable mapping. Base geography, boundaries, hierarchy and
navigation, and facility metadata already permitted by scope are served by the
geography services and never pass through here. This gate covers one thing:
analytic layers built from patient encounters. Every series kind MARS currently
aggregates is one of those, so the gate always applies to this service;
``PATIENT_DERIVED_SERIES`` is written out so that a later operational layer -
commodity stock conditions, say, which are a fact about a store rather than
about a person - is not swept into it by default.

**Suppression protects people, not zeroes.** A cell counting nobody has nobody
in it to identify, so a reported zero is shown. Withholding it would hide
exactly the districts with no malaria and would make a genuine zero
indistinguishable from a withheld figure - the confusion this module exists to
prevent.

Returns plain dictionaries. No ORM object leaves this module.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from mars.core.errors import GeographyScopeDeniedError
from mars.core.logging import get_logger
from mars.domain.enums import (
    BaselineSeriesKind,
    GeographyGrain,
    GeographyLevel,
    IndicatorValueStatus,
    LifecycleStatus,
    SpatialAggregationBasis,
    SpatialCellStatus,
)
from mars.domain.geography import GeographyUnit
from mars.domain.governance import ConfigurationKey, ConfigurationVersion
from mars.domain.spatial import GeographicAggregationResult

logger = get_logger(__name__)

#: The governed privacy policy for spatial output. Registered by governance;
#: **not** shipped with values. What counts as a cell too small to show is a
#: disclosure decision, and it belongs to the programme and its data
#: protection authority, not to this file.
PRIVACY_POLICY_KEY = "spatial_privacy_policy"

#: Both are required before any patient-derived spatial detail is served.
REQUIRED_POLICY_KEYS = ("minimum_cell_count", "minimum_aggregation_level")

#: Series built from patient encounters. Their small cells can identify a
#: person in a sparsely populated area, so they pass through the gate.
#: Commodity stock conditions are deliberately absent: a store having no
#: artemether-lumefantrine is a fact about a store.
PATIENT_DERIVED_SERIES = frozenset(
    {
        BaselineSeriesKind.INDICATOR,
        BaselineSeriesKind.TESTING_MEASURE,
        BaselineSeriesKind.TREATMENT_MEASURE,
    }
)

#: Coarse to fine. Used to compare a requested grain against the approved
#: minimum aggregation level; never to choose one.
GRAIN_ORDER: dict[GeographyGrain, int] = {
    GeographyGrain.NATIONAL: 0,
    GeographyGrain.DISTRICT: 1,
    GeographyGrain.SUBCOUNTY: 2,
    GeographyGrain.FACILITY: 3,
}

#: The analytical grain and the administrative level are different axes, and
#: the national grain is the country level rather than a level of its own.
#: Mapped explicitly so nothing has to guess.
GRAIN_TO_LEVEL: dict[GeographyGrain, GeographyLevel] = {
    GeographyGrain.NATIONAL: GeographyLevel.COUNTRY,
    GeographyGrain.DISTRICT: GeographyLevel.DISTRICT,
    GeographyGrain.SUBCOUNTY: GeographyLevel.SUBCOUNTY,
}


@dataclass(frozen=True, slots=True)
class PrivacyPolicy:
    """The approved rules for showing patient-derived spatial detail."""

    configuration_version_id: uuid.UUID
    minimum_cell_count: int
    minimum_aggregation_level: GeographyGrain


def privacy_policy(session: Session) -> tuple[PrivacyPolicy | None, list[str]]:
    """The approved spatial privacy policy, or ``None`` with what is missing.

    ``None`` is the expected state of a fresh deployment. Callers must treat it
    as "cannot show patient-derived detail" rather than substituting a minimum,
    because a substituted minimum is a disclosure decision made by an engineer.
    """
    version = (
        session.execute(
            select(ConfigurationVersion)
            .join(
                ConfigurationKey,
                ConfigurationKey.id == ConfigurationVersion.configuration_key_id,
            )
            .where(
                ConfigurationKey.key == PRIVACY_POLICY_KEY,
                ConfigurationVersion.status == LifecycleStatus.ACTIVE,
            )
        )
        .scalars()
        .first()
    )
    if version is None or not isinstance(version.value, dict):
        return None, [f"configuration:{PRIVACY_POLICY_KEY}", *REQUIRED_POLICY_KEYS]

    values = version.value
    missing = [key for key in REQUIRED_POLICY_KEYS if values.get(key) is None]
    if missing:
        return None, missing

    try:
        level = GeographyGrain(values["minimum_aggregation_level"])
    except ValueError:
        return None, ["minimum_aggregation_level"]

    minimum = int(values["minimum_cell_count"])
    if minimum < 1:
        return None, ["minimum_cell_count"]

    return (
        PrivacyPolicy(
            configuration_version_id=version.id,
            minimum_cell_count=minimum,
            minimum_aggregation_level=level,
        ),
        [],
    )


def _approved_level_only(session: Session) -> GeographyGrain | None:
    """The approved aggregation level when it alone has been decided.

    Reported as the highest safe geography in a refusal. It is read from an
    approved configuration, so naming it invents nothing; when no level has
    been approved the refusal says so instead of guessing one.
    """
    version = (
        session.execute(
            select(ConfigurationVersion)
            .join(
                ConfigurationKey,
                ConfigurationKey.id == ConfigurationVersion.configuration_key_id,
            )
            .where(
                ConfigurationKey.key == PRIVACY_POLICY_KEY,
                ConfigurationVersion.status == LifecycleStatus.ACTIVE,
            )
        )
        .scalars()
        .first()
    )
    if version is None or not isinstance(version.value, dict):
        return None
    raw = version.value.get("minimum_aggregation_level")
    if raw is None:
        return None
    try:
        return GeographyGrain(raw)
    except ValueError:
        return None


def _in_scope(unit: GeographyUnit, authorised_paths: tuple[str, ...] | None) -> bool:
    if authorised_paths is None:
        return True
    path = unit.path or ""
    return any(path == root or path.startswith(f"{root}/") for root in authorised_paths)


def spatial_cells(
    session: Session,
    *,
    series_kind: BaselineSeriesKind,
    series_key: str,
    period_start: date,
    geography_grain: GeographyGrain,
    basis: SpatialAggregationBasis,
    boundary_version_id: uuid.UUID,
    authorised_paths: tuple[str, ...] | None = None,
    requested_unit_ids: tuple[uuid.UUID, ...] | None = None,
) -> dict[str, object]:
    """One map layer, with every cell's status stated.

    ``authorised_paths`` are the materialised paths of the principal's
    geography scopes; ``None`` means national scope. ``requested_unit_ids``,
    when given, are identifiers the caller named explicitly: naming one outside
    scope is rejected rather than filtered away, so a caller learns their
    request was refused instead of quietly receiving less than they asked for.
    """
    administrative_level = GRAIN_TO_LEVEL.get(geography_grain)
    if administrative_level is None:
        # A facility is a reporting unit, not an administrative area. Mapping
        # patient-derived figures to facility points is exactly what the
        # blueprint forbids, so this service does not offer it.
        raise ValueError("spatial cells are administrative units; facility grain is not mapped")

    units = list(
        session.execute(
            select(GeographyUnit).where(
                GeographyUnit.boundary_version_id == boundary_version_id,
                GeographyUnit.level == administrative_level,
                GeographyUnit.is_active.is_(True),
            )
        )
        .scalars()
        .all()
    )
    by_id = {unit.id: unit for unit in units}

    if requested_unit_ids is not None:
        for unit_id in requested_unit_ids:
            unit = by_id.get(unit_id)
            if unit is None or not _in_scope(unit, authorised_paths):
                raise GeographyScopeDeniedError(
                    "The request names a geography unit outside your authorised scope."
                )
        units = [by_id[unit_id] for unit_id in requested_unit_ids]

    patient_derived = series_kind in PATIENT_DERIVED_SERIES
    policy, missing = privacy_policy(session) if patient_derived else (None, [])

    if patient_derived and policy is None:
        level = _approved_level_only(session)
        logger.info(
            "spatial_output_refused",
            reason="privacy_configuration_required",
            series=series_key,
            grain=geography_grain.value,
        )
        # No cells, and deliberately no empty list either: an empty map is read
        # as an absence of disease.
        return {
            "status": SpatialCellStatus.NOT_CONFIGURED.value,
            "reason": "privacy_configuration_required",
            "missing_configuration": sorted(missing),
            "highest_safe_geography": level.value if level else None,
            "series_kind": series_kind.value,
            "series_key": series_key,
            "geography_grain": geography_grain.value,
            "note": (
                "MARS has no approved policy for showing patient-derived "
                "spatial detail, so this layer is withheld. This is a statement "
                "about configuration, not about malaria: no conclusion about "
                "burden may be drawn from its absence. Base geography, "
                "boundaries and non-patient-derived layers are unaffected."
            ),
        }

    if (
        policy is not None
        and GRAIN_ORDER[geography_grain] > GRAIN_ORDER[policy.minimum_aggregation_level]
    ):
        return {
            "status": SpatialCellStatus.NOT_CONFIGURED.value,
            "reason": "geography_finer_than_approved_minimum",
            "missing_configuration": [],
            "highest_safe_geography": policy.minimum_aggregation_level.value,
            "series_kind": series_kind.value,
            "series_key": series_key,
            "geography_grain": geography_grain.value,
            "note": (
                "The approved privacy policy permits patient-derived spatial "
                "output no finer than "
                f"{policy.minimum_aggregation_level.value}. The coarser layer "
                "is available."
            ),
        }

    rows = (
        session.execute(
            select(GeographicAggregationResult).where(
                GeographicAggregationResult.period_start == period_start,
                GeographicAggregationResult.series_kind == series_kind,
                GeographicAggregationResult.series_key == series_key,
                GeographicAggregationResult.geography_grain == geography_grain,
                GeographicAggregationResult.aggregation_basis == basis,
            )
        )
        .scalars()
        .all()
    )
    latest: dict[uuid.UUID, GeographicAggregationResult] = {}
    for row in rows:
        seen = latest.get(row.geography_unit_id)
        if seen is None or row.computed_at > seen.computed_at:
            latest[row.geography_unit_id] = row

    cells: list[dict[str, object]] = []
    counts: dict[str, int] = {}
    for unit in units:
        cell = _cell(unit, latest.get(unit.id), policy, authorised_paths)
        counts[str(cell["status"])] = counts.get(str(cell["status"]), 0) + 1
        cells.append(cell)

    return {
        "status": "available",
        "series_kind": series_kind.value,
        "series_key": series_key,
        "geography_grain": geography_grain.value,
        "aggregation_basis": basis.value,
        "period_start": period_start.isoformat(),
        "privacy_configuration_version_id": (
            str(policy.configuration_version_id) if policy else None
        ),
        "minimum_cell_count": policy.minimum_cell_count if policy else None,
        "cells": cells,
        "status_counts": dict(sorted(counts.items())),
        "note": (
            "Every cell states why it has the value it has. A cell with no "
            "value is never an assertion that there is no malaria there."
        ),
    }


def _cell(
    unit: GeographyUnit,
    row: GeographicAggregationResult | None,
    policy: PrivacyPolicy | None,
    authorised_paths: tuple[str, ...] | None,
) -> dict[str, object]:
    base: dict[str, object] = {
        "geography_unit_id": str(unit.id),
        "preferred_code": unit.preferred_code,
        "name": unit.raw_name,
        "value": None,
        "numerator": None,
        "denominator": None,
        "reporting_completeness": None,
    }

    if not _in_scope(unit, authorised_paths):
        # The unit's existence is public geography; its figure is not.
        return {**base, "status": SpatialCellStatus.OUTSIDE_SCOPE.value}

    if row is None:
        return {
            **base,
            "status": SpatialCellStatus.MISSING.value,
            "reason": "no_result_for_this_period",
        }

    if row.value_status is not IndicatorValueStatus.AVAILABLE:
        return {
            **base,
            "status": SpatialCellStatus.UNAVAILABLE.value,
            "reason": row.value_status.value,
            "reporting_completeness": _number(row.reporting_completeness),
        }

    if policy is not None and row.numerator is None:
        # A value with no count behind it cannot be checked against the
        # minimum, so it is withheld rather than assumed safe.
        return {
            **base,
            "status": SpatialCellStatus.SUPPRESSED.value,
            "reason": "cell_size_unknown",
            "minimum_cell_count": policy.minimum_cell_count,
            "reporting_completeness": _number(row.reporting_completeness),
        }

    if policy is not None and 0 < (row.numerator or 0) < policy.minimum_cell_count:
        # A real value exists and is withheld. Saying so is the point: a
        # suppressed cell and an empty one are different facts.
        #
        # The lower bound is deliberate. Small-cell suppression protects the
        # people counted in a cell, and a cell of zero counts nobody - there is
        # no individual in it to identify. Suppressing zeroes would withhold
        # exactly the districts with no malaria, which is the good news a
        # programme most needs to see, and would make a genuine zero
        # indistinguishable from a withheld figure on the map.
        return {
            **base,
            "status": SpatialCellStatus.SUPPRESSED.value,
            "reason": "below_minimum_cell_count",
            "minimum_cell_count": policy.minimum_cell_count,
            "reporting_completeness": _number(row.reporting_completeness),
        }

    return {
        **base,
        "status": SpatialCellStatus.AVAILABLE.value,
        # Zero is a figure. It is reported as one.
        "value": _number(row.value),
        "numerator": row.numerator,
        "denominator": row.denominator,
        "reporting_completeness": _number(row.reporting_completeness),
        "contributing_facilities": row.contributing_facilities,
        "expected_facilities": row.expected_facilities,
    }


def _number(value: object) -> str | None:
    if value is None:
        return None
    return str(Decimal(str(value)))


__all__ = [
    "GRAIN_ORDER",
    "GRAIN_TO_LEVEL",
    "PATIENT_DERIVED_SERIES",
    "PRIVACY_POLICY_KEY",
    "REQUIRED_POLICY_KEYS",
    "PrivacyPolicy",
    "privacy_policy",
    "spatial_cells",
]
