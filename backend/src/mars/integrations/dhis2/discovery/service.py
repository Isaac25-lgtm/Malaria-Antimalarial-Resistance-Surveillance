"""Run metadata-only DHIS2 discovery and assemble a sanitized report."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from mars.core.logging import get_logger
from mars.core.timeutils import utc_now
from mars.domain.enums import IntegrationErrorCategory
from mars.integrations.dhis2.discovery.allowlists import (
    METADATA_CAPABILITIES,
    PATIENT_COLLECTION_CAPABILITIES,
    PATIENT_COLLECTION_PATHS,
)
from mars.integrations.dhis2.discovery.classify import candidate_mappings, compact_unit
from mars.integrations.dhis2.discovery.client import (
    DISCOVERY_CLIENT_VERSION,
    DiscoveryClient,
    DiscoveryError,
)
from mars.integrations.dhis2.discovery.models import (
    CapabilityRecord,
    CapabilityStatus,
    DiscoveryReport,
    OrganisationUnitRecord,
)

logger = get_logger(__name__)

INTERPRETATION_LIMIT = (
    "This report is DHIS2 metadata only. Candidate mappings are proposals. "
    "MARS signals indicate patterns requiring investigation. They do not confirm "
    "antimalarial resistance. Discovery stops before patient retrieval."
)

_PATIENT_ROUTES: dict[str, str] = {
    "tracked_entities": "/api/trackedEntities",
    "tracked_entity_instances": "/api/trackedEntityInstances",
    "enrollments": "/api/enrollments",
    "events": "/api/events",
    "relationships": "/api/relationships",
    "tracker_tracked_entities": "/api/tracker/trackedEntities",
    "tracker_enrollments": "/api/tracker/enrollments",
    "tracker_events": "/api/tracker/events",
    "tracker_relationships": "/api/tracker/relationships",
    "patient_analytics": "/api/analytics/trackedEntities/query",
    "event_analytics": "/api/analytics/events/query",
    "aggregate_data_values": "/api/dataValueSets",
}


def run_discovery(client: DiscoveryClient, *, origin_host: str) -> DiscoveryReport:
    """Probe allowlisted metadata, never patient collections."""
    generated_at = _now()
    capabilities: list[CapabilityRecord] = [
        CapabilityRecord(
            name=name,
            route=route,
            status=CapabilityStatus.NOT_PROBED_TO_PROTECT_PATIENT_DATA,
            detail=(
                "This collection can contain patient or event rows. Discovery does not request it."
            ),
            probed=False,
        )
        for name, route in _PATIENT_ROUTES.items()
    ]
    errors: list[str] = []
    truncated: list[str] = []

    system = _probe_object(
        "system_info", "/api/system/info", client.system_info, capabilities, errors
    )
    user = _probe_object("current_user", "/api/me", client.current_user, capabilities, errors)
    auth = _probe_object(
        "current_user_authorization",
        "/api/me/authorization",
        client.current_user_authorization,
        capabilities,
        errors,
    )

    collections: dict[str, list[dict[str, Any]]] = {}
    for name, path in METADATA_CAPABILITIES:
        if path in {"/api/system/info", "/api/me", "/api/me/authorization"}:
            continue
        records, was_truncated, status_record = _probe_collection(client, name, path)
        capabilities.append(status_record)
        collections[name] = records
        if was_truncated:
            truncated.append(name)
        if (
            status_record.status
            not in {
                CapabilityStatus.SUPPORTED_AND_AUTHORIZED,
                CapabilityStatus.NOT_PROBED_TO_PROTECT_PATIENT_DATA,
            }
            and status_record.detail
        ):
            errors.append(f"{name}: {status_record.detail}")

    capture = [_unit(item) for item in _as_units(user.get("organisationUnits"))]
    data_view = [_unit(item) for item in _as_units(user.get("dataViewOrganisationUnits"))]
    tracker_search = [_unit(item) for item in _as_units(user.get("teiSearchOrganisationUnits"))]
    hierarchy = [_unit(item) for item in collections.get("organisation_units", [])]
    all_units = _unique_units([*capture, *data_view, *tracker_search, *hierarchy])

    authorities = _authorities(user, auth)
    programmes = collections.get("programs", [])
    data_elements = collections.get("data_elements", [])
    attributes = collections.get("tracked_entity_attributes", [])

    report = DiscoveryReport(
        generated_at=generated_at,
        client_version=DISCOVERY_CLIENT_VERSION,
        origin_host=origin_host,
        interpretation_limit=INTERPRETATION_LIMIT,
        system=system,
        current_user=_public_user(user),
        authorities=authorities,
        capture_organisation_units=capture,
        data_view_organisation_units=data_view,
        tracker_search_organisation_units=tracker_search,
        pader_candidates=[unit for unit in all_units if unit.classification == "pader_candidate"],
        confirmed_facility_candidates=[
            unit for unit in all_units if unit.classification == "candidate_facility"
        ],
        programmes=programmes,
        program_stages=collections.get("program_stages", []),
        tracked_entity_types=collections.get("tracked_entity_types", []),
        tracked_entity_attributes=attributes,
        data_elements=data_elements,
        option_sets=collections.get("option_sets", []),
        data_sets=collections.get("data_sets", []),
        category_combos=collections.get("category_combos", []),
        candidate_mappings=candidate_mappings(
            units=all_units,
            programmes=programmes,
            data_elements=data_elements,
            attributes=attributes,
        ),
        capabilities=_sorted_capabilities(capabilities),
        truncated_collections=truncated,
        errors=errors,
    )
    logger.info(
        "dhis2_discovery_completed",
        origin_host=origin_host,
        capabilities=len(report.capabilities),
        proposals=len(report.candidate_mappings),
        stop_before_patient_data=True,
    )
    return report


def assert_no_patient_requests(requested_paths: list[str]) -> None:
    """Test helper: fail if a patient collection was requested."""
    offenders = [path for path in requested_paths if _is_patient_path(path)]
    if offenders:
        raise AssertionError(f"patient collections were requested: {offenders}")


def _is_patient_path(path: str) -> bool:
    normalised = path.split("?")[0].rstrip("/")
    if normalised in PATIENT_COLLECTION_PATHS:
        return True
    return normalised.startswith("/api/tracker/") or normalised.startswith("/api/analytics")


def _probe_object(
    name: str,
    route: str,
    fetch: Any,
    capabilities: list[CapabilityRecord],
    errors: list[str],
) -> dict[str, Any]:
    try:
        payload = fetch()
    except DiscoveryError as error:
        capabilities.append(_capability_from_error(name, route, error, probed=True))
        errors.append(f"{name}: {error}")
        return {}
    capabilities.append(
        CapabilityRecord(
            name=name,
            route=route,
            status=CapabilityStatus.SUPPORTED_AND_AUTHORIZED,
            http_status=200,
            detail="Metadata endpoint responded and was projected to the field allowlist.",
            probed=True,
        )
    )
    return payload if isinstance(payload, dict) else {}


def _probe_collection(
    client: DiscoveryClient, name: str, path: str
) -> tuple[list[dict[str, Any]], bool, CapabilityRecord]:
    try:
        records, truncated = client.collect(path)
    except DiscoveryError as error:
        return [], False, _capability_from_error(name, path, error, probed=True)
    detail = "Metadata collection responded."
    if truncated:
        detail = "Metadata collection responded but stopped at the configured page cap."
    return (
        records,
        truncated,
        CapabilityRecord(
            name=name,
            route=path,
            status=CapabilityStatus.SUPPORTED_AND_AUTHORIZED,
            http_status=200,
            detail=detail,
            probed=True,
        ),
    )


def _capability_from_error(
    name: str, route: str, error: DiscoveryError, *, probed: bool
) -> CapabilityRecord:
    status = {
        IntegrationErrorCategory.AUTHENTICATION: CapabilityStatus.AUTHENTICATION_FAILED,
        IntegrationErrorCategory.AUTHORISATION: CapabilityStatus.SUPPORTED_BUT_FORBIDDEN,
        IntegrationErrorCategory.NOT_FOUND: CapabilityStatus.NOT_SUPPORTED,
    }.get(error.category, CapabilityStatus.INDETERMINATE)
    return CapabilityRecord(
        name=name,
        route=route,
        status=status,
        http_status=error.status_code,
        detail=str(error),
        probed=probed,
    )


def _as_units(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    return []


def _unit(raw: dict[str, Any]) -> OrganisationUnitRecord:
    return compact_unit(raw)


def _unique_units(units: list[OrganisationUnitRecord]) -> list[OrganisationUnitRecord]:
    seen: dict[str, OrganisationUnitRecord] = {}
    for unit in units:
        if unit.id and unit.id not in seen:
            seen[unit.id] = unit
    return list(seen.values())


def _authorities(user: dict[str, Any], auth: dict[str, Any]) -> list[str]:
    values: list[str] = []
    for source in (user.get("authorities"), auth.get("authorities")):
        if isinstance(source, list):
            values.extend(item for item in source if isinstance(item, str))
    return sorted(set(values))


def _public_user(user: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": user.get("id"),
        "username": user.get("username"),
        "organisation_unit_count": len(_as_units(user.get("organisationUnits"))),
        "data_view_organisation_unit_count": len(_as_units(user.get("dataViewOrganisationUnits"))),
        "tracker_search_organisation_unit_count": len(
            _as_units(user.get("teiSearchOrganisationUnits"))
        ),
    }


def _sorted_capabilities(records: list[CapabilityRecord]) -> list[CapabilityRecord]:
    by_name = {record.name: record for record in records}
    ordered = list(by_name.values())
    expected = {name for name, _route in METADATA_CAPABILITIES} | set(
        PATIENT_COLLECTION_CAPABILITIES
    )
    missing = expected - {record.name for record in ordered}
    for name in sorted(missing):
        route = _PATIENT_ROUTES.get(name, "")
        ordered.append(
            CapabilityRecord(
                name=name,
                route=route or next((r for n, r in METADATA_CAPABILITIES if n == name), ""),
                status=(
                    CapabilityStatus.NOT_PROBED_TO_PROTECT_PATIENT_DATA
                    if name in PATIENT_COLLECTION_CAPABILITIES
                    else CapabilityStatus.INDETERMINATE
                ),
                detail="Capability was not observed during this run.",
                probed=False,
            )
        )
    return sorted(ordered, key=lambda record: record.name)


def _now() -> datetime:
    try:
        return utc_now()
    except Exception:
        return datetime.now(UTC)
