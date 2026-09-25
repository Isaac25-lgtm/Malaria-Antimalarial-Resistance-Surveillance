"""The demonstration configuration pack.

MARS ships no analytical parameters. Every engine refuses to run until a
programme approves its method, which is right for a Ministry deployment and
leaves a demonstration showing nothing but *not configured*.

This pack supplies one version of each method and configuration key the
demonstration needs, **for the synthetic dataset only**:

* every value is illustrative, chosen so the planted storylines can surface,
  and none is a recommendation for real data;
* every version is approved by :data:`DEMO_APPROVER`, a name that says what it
  is wherever ``approved_by`` is shown or exported;
* it refuses to run unless demo mode is on in a non-protected environment;
* it never touches a key or method that already has an active version approved
  by anyone else. A real approval always wins over the pack.

Re-running is safe. A pack version that is already active with the same
parameters is left alone.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from mars.analytics.anomaly import ANOMALY_RULE_CODE
from mars.analytics.baseline import BASELINE_METHOD_CODE
from mars.analytics.clustering import CLUSTER_METHOD_CODE
from mars.analytics.episodes import EPISODE_RULE_CODE
from mars.analytics.hotspot import HOTSPOT_DEFINITION_CODE
from mars.analytics.indicator_registry import IndicatorRegistryService
from mars.analytics.recurrence import INTERVAL_BANDS_KEY
from mars.core.errors import NotFoundError
from mars.core.settings import Settings
from mars.domain.enums import LifecycleStatus, MethodKind
from mars.domain.governance import (
    ConfigurationKey,
    ConfigurationVersion,
    MethodDefinition,
    MethodVersion,
)
from mars.domain.indicator import IndicatorDefinitionVersion
from mars.security.permissions import SensitivityLevel
from mars.security.principal import AuthenticatedPrincipal
from mars.services.audit_service import AuditService
from mars.services.governance_service import (
    ConfigurationService,
    MethodRegistryService,
    canonical_checksum,
)
from mars.services.spatial_availability import PRIVACY_POLICY_KEY
from mars.signals.engine import SIGNAL_METHOD_CODE

#: Written into ``approved_by`` on every version the pack activates. Kept under
#: the 128-character column and readable on its own in an export.
DEMO_APPROVER = "demo-pack:synthetic-not-programme-approved"

DEMO_SEMANTIC_VERSION = "demo-1.0"

#: The pack's versions take effect from this date so that every month of the
#: default synthetic dataset falls under them.
DEMO_EFFECTIVE_FROM = date(2000, 1, 1)

DEMO_REASON = (
    "Synthetic demonstration configuration. Illustrative values for the demo "
    "dataset only; not approved by any programme."
)

_PRINCIPAL_ID = uuid.uuid5(uuid.NAMESPACE_URL, "mars:demo-configuration-pack")


@dataclass(frozen=True, slots=True)
class DemoMethod:
    code: str
    label: str
    kind: MethodKind
    purpose: str
    parameters: dict[str, Any]


@dataclass(frozen=True, slots=True)
class DemoConfiguration:
    key: str
    label: str
    description: str
    category: str
    value: dict[str, Any]


#: One version per method. Values are illustrative; the comment beside each
#: says what it does in the demo, not what a programme should choose.
METHODS: tuple[DemoMethod, ...] = (
    DemoMethod(
        code=EPISODE_RULE_CODE,
        label="Malaria episode rule (demonstration)",
        kind=MethodKind.EPISODE_RULE,
        purpose="Groups positive encounters into episodes for the synthetic dataset.",
        # A positive more than a week after the last one opens a new episode,
        # so the planted returns at 7 to 42 days become visible recurrences.
        parameters={"episode_window_days": 7},
    ),
    DemoMethod(
        code=BASELINE_METHOD_CODE,
        label="Historical baseline (demonstration)",
        kind=MethodKind.TEMPORAL_BASELINE,
        purpose="Expected levels from each series' own recent history, for the demo.",
        parameters={
            "baseline_method": "historical_median",
            "history_periods": 6,
            "minimum_history_periods": 3,
            "minimum_completeness": 0.8,
            "uncertainty_multiplier": 2,
        },
    ),
    DemoMethod(
        code=ANOMALY_RULE_CODE,
        label="Temporal anomaly rule (demonstration)",
        kind=MethodKind.SIGNAL_RULE,
        purpose="Flags a period that departs from its baseline, for the demo.",
        parameters={
            "detection_method": "relative_deviation",
            "deviation_threshold": 0.3,
            "minimum_case_count": 10,
        },
    ),
    DemoMethod(
        code=HOTSPOT_DEFINITION_CODE,
        label="Hotspot definition (demonstration)",
        kind=MethodKind.SPATIAL_METHOD,
        purpose="Marks areas well above their own history, for the demo.",
        parameters={
            "detection_method": "relative_deviation",
            "deviation_threshold": 0.5,
            "minimum_case_count": 20,
            "minimum_completeness": 0.8,
            "persistence_periods": 2,
        },
    ),
    DemoMethod(
        code=CLUSTER_METHOD_CODE,
        label="Spatial clustering (demonstration)",
        kind=MethodKind.SPATIAL_METHOD,
        purpose="Finds areas concentrated above their neighbours, for the demo.",
        parameters={
            "method": "neighbour_concentration",
            "minimum_case_count": 20,
            "minimum_completeness": 0.8,
            "minimum_neighbours": 2,
            "neighbour_ratio_threshold": 1.5,
        },
    ),
    DemoMethod(
        code=SIGNAL_METHOD_CODE,
        label="Signal prioritisation (demonstration)",
        kind=MethodKind.SIGNAL_RULE,
        purpose="Combines judged upstream evidence into signals, for the demo.",
        parameters={
            "rules": [
                {
                    "code": "demo-repeat-positive",
                    "signal_type": "repeat_positive",
                    "source_kinds": ["recurrence"],
                    "minimum_evidence": 2,
                    "minimum_score": 2,
                    "weights": {"recurrence": 1},
                    "priority_bands": [
                        {"priority": "attention", "minimum_score": 2},
                        {"priority": "high", "minimum_score": 4},
                        {"priority": "urgent", "minimum_score": 6},
                    ],
                    "recommended_action_codes": [
                        "review_patient_evidence",
                        "check_treatment_recording",
                    ],
                },
                {
                    "code": "demo-testing-disruption",
                    "signal_type": "testing_anomaly",
                    "source_kinds": ["testing", "temporal_anomaly"],
                    "minimum_evidence": 1,
                    "minimum_score": 2,
                    "weights": {"testing": 2, "temporal_anomaly": 1},
                    "priority_bands": [
                        {"priority": "attention", "minimum_score": 2},
                        {"priority": "high", "minimum_score": 3},
                        {"priority": "urgent", "minimum_score": 5},
                    ],
                    "recommended_action_codes": [
                        "verify_test_stock_and_reporting",
                        "review_with_district_team",
                    ],
                },
                {
                    "code": "demo-treatment-pattern",
                    "signal_type": "treatment_anomaly",
                    "source_kinds": ["treatment"],
                    "minimum_evidence": 1,
                    "minimum_score": 1,
                    "weights": {"treatment": 1},
                    "priority_bands": [
                        {"priority": "informational", "minimum_score": 1},
                        {"priority": "attention", "minimum_score": 2},
                    ],
                    "recommended_action_codes": ["check_treatment_recording"],
                },
                {
                    "code": "demo-area-rise",
                    "signal_type": "spatial_cluster",
                    "source_kinds": ["hotspot", "spatial_cluster"],
                    "minimum_evidence": 1,
                    "minimum_score": 1,
                    "weights": {"hotspot": 1, "spatial_cluster": 2},
                    "priority_bands": [
                        {"priority": "attention", "minimum_score": 1},
                        {"priority": "high", "minimum_score": 3},
                    ],
                    "recommended_action_codes": ["review_with_district_team"],
                },
            ]
        },
    ),
)

CONFIGURATIONS: tuple[DemoConfiguration, ...] = (
    DemoConfiguration(
        key=INTERVAL_BANDS_KEY,
        label="Recurrence interval bands (demonstration)",
        description="Bands for the days between positive encounters, for the demo.",
        category="recurrence",
        value={
            "bands": [
                {"label": "0-6 days", "lower_days": 0, "upper_days": 6},
                {"label": "7-13 days", "lower_days": 7, "upper_days": 13},
                {"label": "14-27 days", "lower_days": 14, "upper_days": 27},
                {"label": "28-41 days", "lower_days": 28, "upper_days": 41},
                {"label": "42 days or more", "lower_days": 42, "upper_days": None},
            ]
        },
    ),
    DemoConfiguration(
        key=PRIVACY_POLICY_KEY,
        label="Spatial privacy policy (demonstration)",
        description="Smallest cell and finest level for patient-derived map layers, for the demo.",
        category="privacy",
        value={"minimum_cell_count": 5, "minimum_aggregation_level": "subcounty"},
    ),
)


class DemoPackRefused(RuntimeError):
    """The pack was asked to run somewhere it must not."""


@dataclass(slots=True)
class PackReport:
    activated: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    kept_existing: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, list[str]]:
        return {
            "activated": sorted(self.activated),
            "unchanged": sorted(self.unchanged),
            "kept_existing": sorted(self.kept_existing),
        }


def demo_principal() -> AuthenticatedPrincipal:
    """The actor recorded on every approval and audit event the pack writes."""
    return AuthenticatedPrincipal(
        user_id=_PRINCIPAL_ID,
        subject="mars-demo-configuration-pack",
        username=DEMO_APPROVER,
        display_name="MARS demonstration configuration pack",
        roles=frozenset(),
        permissions=frozenset(),
        max_sensitivity=SensitivityLevel.AGGREGATE,
        auth_method="demo_pack",
        is_synthetic=True,
    )


def ensure_allowed(settings: Settings) -> None:
    if settings.environment.is_protected:
        raise DemoPackRefused(
            f"The demonstration pack is refused in {settings.environment.value}: it would put "
            "illustrative parameters in force under a real deployment."
        )
    if not settings.demo_mode_enabled:
        raise DemoPackRefused(
            "The demonstration pack runs only with MARS_DEMO_MODE_ENABLED=true, so its "
            "parameters can never govern live data."
        )


def apply_pack(session: Session, settings: Settings) -> PackReport:
    """Put the pack in force. Flushes but does not commit."""
    ensure_allowed(settings)
    report = PackReport()
    principal = demo_principal()
    audit = AuditService(session)
    _apply_indicators(session, report)
    methods = MethodRegistryService(session, audit)
    for method in METHODS:
        _apply_method(session, methods, principal, method, report)
    configurations = ConfigurationService(session, audit)
    for configuration in CONFIGURATIONS:
        _apply_configuration(session, configurations, principal, configuration, report)
    session.flush()
    return report


def _apply_indicators(session: Session, report: PackReport) -> None:
    """Approve each shipped indicator definition that nobody has approved yet."""
    registry = IndicatorRegistryService(session)
    registry.seed_catalogue()
    active = registry.active_versions()
    for definition in registry.list_definitions():
        label = f"indicator:{definition.code}"
        current = active.get(definition.code)
        if current is not None:
            if current.approved_by == DEMO_APPROVER:
                report.unchanged.append(label)
            else:
                report.kept_existing.append(label)
            continue
        draft = next(
            (
                version
                for version in sorted(definition.versions, key=_version_number, reverse=True)
                if version.status in {LifecycleStatus.DRAFT, LifecycleStatus.IN_REVIEW}
            ),
            None,
        )
        if draft is None:
            continue
        registry.approve_version(
            draft.id, approved_by=DEMO_APPROVER, effective_from=DEMO_EFFECTIVE_FROM
        )
        registry.activate_version(draft.id)
        report.activated.append(label)


def _version_number(version: IndicatorDefinitionVersion) -> int:
    return version.version_number


def _apply_method(
    session: Session,
    registry: MethodRegistryService,
    principal: AuthenticatedPrincipal,
    method: DemoMethod,
    report: PackReport,
) -> None:
    label = f"method:{method.code}"
    try:
        definition = registry.get_method(method.code)
    except NotFoundError:
        definition = registry.register_method(
            code=method.code,
            label=method.label,
            kind=method.kind,
            purpose=method.purpose,
            owner=DEMO_APPROVER,
            principal=principal,
        )

    active = session.execute(
        select(MethodVersion).where(
            MethodVersion.method_definition_id == definition.id,
            MethodVersion.status == LifecycleStatus.ACTIVE,
        )
    ).scalar_one_or_none()
    checksum = canonical_checksum(method.parameters)
    if active is not None:
        if active.approved_by != DEMO_APPROVER:
            report.kept_existing.append(label)
            return
        if canonical_checksum(active.parameters or {}) == checksum:
            report.unchanged.append(label)
            return

    semantic_version = _next_semantic_version(session, definition)
    version = registry.draft_version(
        code=method.code,
        semantic_version=semantic_version,
        summary=DEMO_REASON,
        parameters=method.parameters,
        artifact_reference="backend/src/mars/demo/configuration_pack.py",
        artifact_checksum=checksum,
        owner=DEMO_APPROVER,
    )
    for target in (LifecycleStatus.IN_REVIEW, LifecycleStatus.APPROVED, LifecycleStatus.ACTIVE):
        registry.promote(
            version_id=version.id,
            target=target,
            principal=principal,
            reason=DEMO_REASON,
            effective_from=DEMO_EFFECTIVE_FROM if target is LifecycleStatus.ACTIVE else None,
        )
    report.activated.append(label)


def _next_semantic_version(session: Session, definition: MethodDefinition) -> str:
    """``demo-1.0`` the first time, ``demo-1.1`` when the pack's values change."""
    existing = set(
        session.execute(
            select(MethodVersion.semantic_version).where(
                MethodVersion.method_definition_id == definition.id
            )
        )
        .scalars()
        .all()
    )
    if DEMO_SEMANTIC_VERSION not in existing:
        return DEMO_SEMANTIC_VERSION
    minor = 1
    while f"demo-1.{minor}" in existing:
        minor += 1
    return f"demo-1.{minor}"


def _apply_configuration(
    session: Session,
    service: ConfigurationService,
    principal: AuthenticatedPrincipal,
    configuration: DemoConfiguration,
    report: PackReport,
) -> None:
    label = f"configuration:{configuration.key}"
    key = session.execute(
        select(ConfigurationKey).where(ConfigurationKey.key == configuration.key)
    ).scalar_one_or_none()
    if key is None:
        service.create_key(
            key=configuration.key,
            label=configuration.label,
            description=configuration.description,
            category=configuration.category,
            owner=DEMO_APPROVER,
        )

    active = session.execute(
        select(ConfigurationVersion)
        .join(ConfigurationKey, ConfigurationKey.id == ConfigurationVersion.configuration_key_id)
        .where(
            ConfigurationKey.key == configuration.key,
            ConfigurationVersion.status == LifecycleStatus.ACTIVE,
        )
    ).scalar_one_or_none()
    if active is not None:
        if active.approved_by != DEMO_APPROVER:
            report.kept_existing.append(label)
            return
        if active.value_checksum == canonical_checksum(configuration.value):
            report.unchanged.append(label)
            return

    version = service.draft_version(
        key=configuration.key,
        value=configuration.value,
        reason_for_change=DEMO_REASON,
        provenance="backend/src/mars/demo/configuration_pack.py",
        owner=DEMO_APPROVER,
        effective_from=DEMO_EFFECTIVE_FROM,
    )
    for target in (LifecycleStatus.IN_REVIEW, LifecycleStatus.APPROVED, LifecycleStatus.ACTIVE):
        service.transition(
            version_id=version.id,
            target=target,
            principal=principal,
            reason=DEMO_REASON,
        )
    report.activated.append(label)


__all__ = [
    "CONFIGURATIONS",
    "DEMO_APPROVER",
    "METHODS",
    "DemoPackRefused",
    "PackReport",
    "apply_pack",
    "demo_principal",
    "ensure_allowed",
]
