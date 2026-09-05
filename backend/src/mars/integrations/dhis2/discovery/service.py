"""Run metadata-only DHIS2 discovery and assemble a sanitized report."""

from __future__ import annotations

import re
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

_PROTECTED_DATA_ROUTES: dict[str, str] = {
    "tracked_entity_instances": "/api/trackedEntityInstances",
    "enrollments": "/api/enrollments",
    "events": "/api/events",
    "relationships": "/api/relationships",
    "tracker_tracked_entities": "/api/tracker/trackedEntities",
    "tracker_enrollments": "/api/tracker/enrollments",
    "tracker_events": "/api/tracker/events",
    "tracker_relationships": "/api/tracker/relationships",
    "tracked_entity_analytics_query": "/api/analytics/trackedEntities/query",
    "enrollment_analytics_query": "/api/analytics/enrollments/query",
    "event_analytics_query": "/api/analytics/events/query",
    "event_analytics_aggregate": "/api/analytics/events/aggregate",
    "aggregate_data_values": "/api/dataValueSets",
}

_MODERN_TRACKER = frozenset(
    {
        "tracker_tracked_entities",
        "tracker_enrollments",
        "tracker_events",
        "tracker_relationships",
    }
)
_LEGACY_TRACKER = frozenset({"tracked_entity_instances", "enrollments", "events", "relationships"})
_ANALYTICAL_APIS = frozenset(
    {
        "tracked_entity_analytics_query",
        "enrollment_analytics_query",
        "event_analytics_query",
        "event_analytics_aggregate",
        "aggregate_data_values",
    }
)


def run_discovery(client: DiscoveryClient, *, origin_host: str) -> DiscoveryReport:
    """Probe allowlisted metadata, never patient collections."""
    generated_at = _now()
    capabilities: list[CapabilityRecord] = []
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
    legacy_auth = _probe_object(
        "current_user_authorities_legacy",
        "/api/me/authorities",
        client.current_user_authorities_legacy,
        capabilities,
        errors,
    )

    collections: dict[str, list[dict[str, Any]]] = {}
    for name, path in METADATA_CAPABILITIES:
        if path in {
            "/api/system/info",
            "/api/me",
            "/api/me/authorization",
            "/api/me/authorities",
        }:
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
    pader_candidates = [unit for unit in all_units if unit.classification == "pader_candidate"]
    accessible_facilities = _accessible_pader_facilities(
        hierarchy=hierarchy,
        pader_candidates=pader_candidates,
        scope_roots=_unique_units([*capture, *data_view, *tracker_search]),
    )
    accessible_facilities = _with_ancestor_names(accessible_facilities, all_units)
    facility_scope_sets = {
        "capture": _accessible_pader_facilities(
            hierarchy=hierarchy,
            pader_candidates=pader_candidates,
            scope_roots=capture,
        ),
        "data_view": _accessible_pader_facilities(
            hierarchy=hierarchy,
            pader_candidates=pader_candidates,
            scope_roots=data_view,
        ),
        "tracker_search": _accessible_pader_facilities(
            hierarchy=hierarchy,
            pader_candidates=pader_candidates,
            scope_roots=tracker_search,
        ),
    }

    authorities = _authorities(user, auth, legacy_auth)
    programmes = collections.get("programs", [])
    data_elements = collections.get("data_elements", [])
    attributes = collections.get("tracked_entity_attributes", [])
    api_generation = _api_generation(system)
    protected = _protected_capabilities(
        system=system,
        resources=collections.get("resources", []),
    )
    capabilities.extend(protected)
    supported_analytical_apis = sorted(
        record.route
        for record in protected
        if record.name in _ANALYTICAL_APIS
        and record.status is CapabilityStatus.SUPPORTED_BY_VERSION_AUTHORIZATION_NOT_PROBED
    )
    access_limitations = _access_limitations(
        pader_candidates=pader_candidates,
        truncated=truncated,
        capabilities=capabilities,
    )
    unresolved_questions = _unresolved_questions(
        pader_candidates=pader_candidates,
        programmes=programmes,
        accessible_facilities=accessible_facilities,
    )

    hierarchy_available = any(
        record.name == "organisation_units"
        and record.status is CapabilityStatus.SUPPORTED_AND_AUTHORIZED
        for record in capabilities
    )
    report = DiscoveryReport(
        generated_at=generated_at,
        client_version=DISCOVERY_CLIENT_VERSION,
        origin_host=origin_host,
        interpretation_limit=INTERPRETATION_LIMIT,
        api_generation=api_generation,
        system=system,
        current_user=_public_user(user),
        authorities=authorities,
        capture_organisation_units=capture,
        data_view_organisation_units=data_view,
        tracker_search_organisation_units=tracker_search,
        pader_candidates=pader_candidates,
        accessible_facilities=accessible_facilities,
        accessible_facility_count=len(accessible_facilities) if hierarchy_available else None,
        facility_scope_counts={
            name: len(units) if hierarchy_available else None
            for name, units in facility_scope_sets.items()
        },
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
        supported_analytical_apis=supported_analytical_apis,
        access_limitations=access_limitations,
        unresolved_questions=unresolved_questions,
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


def _accessible_pader_facilities(
    *,
    hierarchy: list[OrganisationUnitRecord],
    pader_candidates: list[OrganisationUnitRecord],
    scope_roots: list[OrganisationUnitRecord],
) -> list[OrganisationUnitRecord]:
    """Return leaf/facility units below Pader that intersect an assigned scope.

    Organisation-unit metadata sharing is not itself data access. Requiring a
    unit to sit below both a Pader candidate and one of the user's capture,
    data-view or tracker-search roots avoids presenting every visible metadata
    object as an accessible facility.
    """
    if not pader_candidates or not scope_roots:
        return []
    facilities: list[OrganisationUnitRecord] = []
    for unit in hierarchy:
        if unit.classification != "candidate_facility":
            continue
        below_pader = any(_unit_is_within(unit, root) for root in pader_candidates)
        inside_scope = any(_unit_is_within(unit, root) for root in scope_roots)
        if below_pader and inside_scope:
            facilities.append(unit)
    return sorted(facilities, key=lambda unit: ((unit.name or "").casefold(), unit.id))


def _with_ancestor_names(
    facilities: list[OrganisationUnitRecord],
    all_units: list[OrganisationUnitRecord],
) -> list[OrganisationUnitRecord]:
    """Attach public hierarchy labels so GeoJSON areas can be joined without GPS."""
    by_id = {unit.id: unit for unit in all_units}
    enriched: list[OrganisationUnitRecord] = []
    for facility in facilities:
        path_ids = [part for part in (facility.path or "").split("/") if part]
        names = [
            unit.name
            for uid in path_ids
            if uid != facility.id and (unit := by_id.get(uid)) is not None and unit.name
        ]
        enriched.append(facility.model_copy(update={"ancestor_names": names}))
    return enriched


def _unit_is_within(unit: OrganisationUnitRecord, root: OrganisationUnitRecord) -> bool:
    if unit.id == root.id:
        return True
    if unit.path:
        return root.id in {part for part in unit.path.split("/") if part}
    return unit.parent_id == root.id


def _api_generation(system: dict[str, Any]) -> str:
    minor = _dhis2_minor(system)
    if minor is None:
        return "indeterminate_until_system_version_is_available"
    if minor >= 42:
        return "modern_tracker_only"
    if minor >= 36:
        return "modern_tracker_preferred_legacy_deprecated"
    return "legacy_tracker"


def _dhis2_minor(system: dict[str, Any]) -> int | None:
    value = system.get("version")
    if not isinstance(value, str):
        return None
    match = re.search(r"(?:^|\D)2\.(\d+)", value)
    return int(match.group(1)) if match else None


def _protected_capabilities(
    *, system: dict[str, Any], resources: list[dict[str, Any]]
) -> list[CapabilityRecord]:
    minor = _dhis2_minor(system)
    advertised = _advertised_routes(resources)
    records: list[CapabilityRecord] = []
    for name, route in _PROTECTED_DATA_ROUTES.items():
        if _route_is_advertised(route, advertised):
            status = CapabilityStatus.SUPPORTED_BY_VERSION_AUTHORIZATION_NOT_PROBED
            detail = (
                "Advertised by the metadata resource catalogue. Authorization and data "
                "access were not probed because this discovery run retrieves no data rows."
            )
        elif name in _MODERN_TRACKER and minor is not None:
            status = (
                CapabilityStatus.SUPPORTED_BY_VERSION_AUTHORIZATION_NOT_PROBED
                if minor >= 36
                else CapabilityStatus.NOT_SUPPORTED
            )
            detail = (
                f"Inferred from DHIS2 2.{minor}: the modern Tracker API was introduced "
                "in 2.36. Authorization was not probed."
            )
        elif name in _LEGACY_TRACKER and minor is not None:
            status = (
                CapabilityStatus.NOT_SUPPORTED
                if minor >= 42
                else CapabilityStatus.SUPPORTED_BY_VERSION_AUTHORIZATION_NOT_PROBED
            )
            detail = (
                f"Inferred from DHIS2 2.{minor}: legacy Tracker endpoints are removed "
                "from 2.42. Authorization was not probed."
            )
        elif name == "aggregate_data_values" and minor is not None:
            status = CapabilityStatus.SUPPORTED_BY_VERSION_AUTHORIZATION_NOT_PROBED
            detail = (
                "The aggregate data-value exchange API is expected for this DHIS2 version. "
                "It was not called because metadata discovery retrieves no data values."
            )
        else:
            status = CapabilityStatus.INDETERMINATE
            detail = (
                "The exact route cannot be established from safe metadata/version evidence. "
                "It was not called because it could return row-level or small-cell data."
            )
        records.append(
            CapabilityRecord(
                name=name,
                route=route,
                status=status,
                detail=detail,
                probed=False,
            )
        )
    return records


def _advertised_routes(resources: list[dict[str, Any]]) -> set[str]:
    advertised: set[str] = set()
    for item in resources:
        endpoint = item.get("relativeApiEndpoint")
        if not isinstance(endpoint, str) or not endpoint.strip():
            continue
        normalised = "/" + endpoint.strip().lstrip("/")
        if not normalised.startswith("/api/"):
            normalised = "/api/" + normalised.removeprefix("/")
        advertised.add(normalised.rstrip("/"))
    return advertised


def _route_is_advertised(route: str, advertised: set[str]) -> bool:
    normalised = route.rstrip("/")
    return normalised in advertised or any(normalised.startswith(f"{base}/") for base in advertised)


def _access_limitations(
    *,
    pader_candidates: list[OrganisationUnitRecord],
    truncated: list[str],
    capabilities: list[CapabilityRecord],
) -> list[str]:
    limitations = [
        "No patient, tracked-entity, enrollment, event or relationship records were retrieved.",
        "No source-data API authorization was tested; supported routes are "
        "metadata/version inferences.",
    ]
    if pader_candidates:
        limitations.append(
            "The discovered account scope appears Pader-specific and must not be "
            "presented as national."
        )
    if truncated:
        limitations.append(
            "One or more metadata collections reached the configured page cap and are incomplete."
        )
    if any(
        item.status
        in {
            CapabilityStatus.SUPPORTED_BUT_FORBIDDEN,
            CapabilityStatus.AUTHENTICATION_FAILED,
        }
        for item in capabilities
    ):
        limitations.append("At least one metadata endpoint was unavailable to this credential.")
    return limitations


def _unresolved_questions(
    *,
    pader_candidates: list[OrganisationUnitRecord],
    programmes: list[dict[str, Any]],
    accessible_facilities: list[OrganisationUnitRecord],
) -> list[str]:
    questions: list[str] = []
    if len(pader_candidates) != 1:
        questions.append("Which organisation-unit UID is the authoritative Pader District?")
    if not programmes:
        questions.append("Which programme is the authoritative OPD/eRegister source?")
    if not accessible_facilities:
        questions.append("Which facilities below Pader are in the approved pilot data-view scope?")
    questions.extend(
        [
            "Which candidate malaria variables and option codes are approved for "
            "each MARS indicator?",
            "What bounded date range and maximum row count may the first controlled test use?",
            "Who is the accountable Ministry data owner approving the first data-bearing request?",
        ]
    )
    return questions


def _authorities(user: dict[str, Any], *authority_payloads: dict[str, Any]) -> list[str]:
    values: list[str] = []
    sources = [user.get("authorities")]
    sources.extend(payload.get("authorities") for payload in authority_payloads)
    for source in sources:
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
        route = _PROTECTED_DATA_ROUTES.get(name, "")
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
