"""Route, query and response-field allowlists for metadata-only DHIS2 discovery.

Nothing in this module is a mapping. Candidate classifications live in
:mod:`mars.integrations.dhis2.discovery.classify` and remain proposals.

Patient collections are listed here solely so they can be refused and reported
as ``not_probed_to_protect_patient_data`` without a request being issued.
"""

from __future__ import annotations

from typing import Final

#: Hosts this utility will contact. Discovery is same-origin to the configured
#: URL; this set is the additional hostname gate so a mistyped URL cannot aim
#: the client at an arbitrary HTTPS server.
ALLOWED_HOSTS: Final[frozenset[str]] = frozenset(
    {
        "hmis.health.go.ug",
        "eregisters.health.go.ug",
        # Contract tests only. Never a live Ministry host.
        "dhis2.example.org",
    }
)

#: Compact ``fields=`` projections. Requesting ``*`` would pull attributes this
#: utility has no business seeing, including contact details on ``/api/me``.
SYSTEM_INFO_FIELDS: Final[str] = ",".join(
    (
        "version",
        "revision",
        "buildTime",
        "serverDate",
        "serverDateTime",
        "systemId",
        "systemName",
        "contextPath",
        "calendar",
        "dateFormat",
    )
)

CURRENT_USER_FIELDS: Final[str] = ",".join(
    (
        "id",
        "username",
        "authorities",
        "organisationUnits[id,name,code,level,path]",
        "dataViewOrganisationUnits[id,name,code,level,path]",
        "teiSearchOrganisationUnits[id,name,code,level,path]",
    )
)

AUTHORISATION_FIELDS: Final[str] = "authorities"

ORGANISATION_UNIT_FIELDS: Final[str] = ",".join(
    (
        "id",
        "name",
        "code",
        "level",
        "path",
        "leaf",
        "openingDate",
        "closedDate",
        "parent[id]",
        "organisationUnitGroups[id,name,code]",
    )
)

PROGRAMME_FIELDS: Final[str] = ",".join(
    (
        "id",
        "name",
        "code",
        "programType",
        "trackedEntityType[id,name]",
        "programStages[id,name,code]",
    )
)

PROGRAM_STAGE_FIELDS: Final[str] = ",".join(
    (
        "id",
        "name",
        "code",
        "program[id,name]",
        "programStageDataElements[dataElement[id,name,code]]",
    )
)

TRACKED_ENTITY_TYPE_FIELDS: Final[str] = ",".join(
    (
        "id",
        "name",
        "trackedEntityTypeAttributes[trackedEntityAttribute[id,name,code,valueType]]",
    )
)

TRACKED_ENTITY_ATTRIBUTE_FIELDS: Final[str] = "id,name,code,valueType,unique,confidential"

DATA_ELEMENT_FIELDS: Final[str] = "id,name,code,valueType,domainType,categoryCombo[id,name]"

OPTION_SET_FIELDS: Final[str] = "id,name,code,valueType,options[id,name,code]"

DATASET_FIELDS: Final[str] = "id,name,code,periodType,dataSetElements[dataElement[id,name,code]]"

CATEGORY_COMBO_FIELDS: Final[str] = "id,name,code,categories[id,name,code]"

RESOURCES_FIELDS: Final[str] = "resources[name,plural,singular,relativeApiEndpoint]"

DEFAULT_QUERY_KEYS: Final[frozenset[str]] = frozenset({"fields", "paging", "pageSize", "page"})

ORGANISATION_UNIT_QUERY_KEYS: Final[frozenset[str]] = DEFAULT_QUERY_KEYS | frozenset(
    {"filter", "level"}
)

#: Exact paths this client may GET. A path not in this set is refused before
#: a socket is opened.
ALLOWED_ROUTES: Final[dict[str, frozenset[str]]] = {
    "/api/system/info": frozenset({"fields"}),
    "/api/resources": DEFAULT_QUERY_KEYS,
    "/api/me": frozenset({"fields"}),
    "/api/me/authorization": frozenset({"fields"}),
    "/api/organisationUnits": ORGANISATION_UNIT_QUERY_KEYS,
    "/api/programs": DEFAULT_QUERY_KEYS,
    "/api/programStages": DEFAULT_QUERY_KEYS,
    "/api/trackedEntityTypes": DEFAULT_QUERY_KEYS,
    "/api/trackedEntityAttributes": DEFAULT_QUERY_KEYS,
    "/api/dataElements": DEFAULT_QUERY_KEYS,
    "/api/optionSets": DEFAULT_QUERY_KEYS,
    "/api/dataSets": DEFAULT_QUERY_KEYS,
    "/api/categoryCombos": DEFAULT_QUERY_KEYS,
}

#: Response keys retained after a successful GET. Anything else is dropped
#: before it can reach a report, a log or a traceback.
RESPONSE_KEYS: Final[dict[str, frozenset[str]]] = {
    "/api/system/info": frozenset(
        {
            "version",
            "revision",
            "buildTime",
            "serverDate",
            "serverDateTime",
            "systemId",
            "systemName",
            "contextPath",
            "calendar",
            "dateFormat",
        }
    ),
    "/api/resources": frozenset({"resources", "pager"}),
    "/api/me": frozenset(
        {
            "id",
            "username",
            "authorities",
            "organisationUnits",
            "dataViewOrganisationUnits",
            "teiSearchOrganisationUnits",
        }
    ),
    "/api/me/authorization": frozenset({"authorities"}),
    "/api/organisationUnits": frozenset({"organisationUnits", "pager"}),
    "/api/programs": frozenset({"programs", "pager"}),
    "/api/programStages": frozenset({"programStages", "pager"}),
    "/api/trackedEntityTypes": frozenset({"trackedEntityTypes", "pager"}),
    "/api/trackedEntityAttributes": frozenset({"trackedEntityAttributes", "pager"}),
    "/api/dataElements": frozenset({"dataElements", "pager"}),
    "/api/optionSets": frozenset({"optionSets", "pager"}),
    "/api/dataSets": frozenset({"dataSets", "pager"}),
    "/api/categoryCombos": frozenset({"categoryCombos", "pager"}),
}

PAGER_KEYS: Final[frozenset[str]] = frozenset({"page", "pageCount", "pageSize", "total"})

ORGANISATION_UNIT_ITEM_KEYS: Final[frozenset[str]] = frozenset(
    {
        "id",
        "name",
        "code",
        "level",
        "path",
        "leaf",
        "openingDate",
        "closedDate",
        "parent",
        "organisationUnitGroups",
    }
)

#: Collections that would retrieve patient or event rows. Discovery classifies
#: them without requesting them.
PATIENT_COLLECTION_PATHS: Final[frozenset[str]] = frozenset(
    {
        "/api/trackedEntities",
        "/api/trackedEntityInstances",
        "/api/enrollments",
        "/api/events",
        "/api/relationships",
        "/api/analytics",
        "/api/analytics/events/query",
        "/api/analytics/trackedEntities/query",
        "/api/dataValueSets",
        "/api/tracker",
        "/api/tracker/trackedEntities",
        "/api/tracker/enrollments",
        "/api/tracker/events",
        "/api/tracker/relationships",
    }
)

PATIENT_COLLECTION_CAPABILITIES: Final[tuple[str, ...]] = (
    "tracked_entities",
    "tracked_entity_instances",
    "enrollments",
    "events",
    "relationships",
    "tracker_tracked_entities",
    "tracker_enrollments",
    "tracker_events",
    "tracker_relationships",
    "patient_analytics",
    "event_analytics",
    "aggregate_data_values",
)

METADATA_CAPABILITIES: Final[tuple[tuple[str, str], ...]] = (
    ("system_info", "/api/system/info"),
    ("resources", "/api/resources"),
    ("current_user", "/api/me"),
    ("current_user_authorization", "/api/me/authorization"),
    ("organisation_units", "/api/organisationUnits"),
    ("programs", "/api/programs"),
    ("program_stages", "/api/programStages"),
    ("tracked_entity_types", "/api/trackedEntityTypes"),
    ("tracked_entity_attributes", "/api/trackedEntityAttributes"),
    ("data_elements", "/api/dataElements"),
    ("option_sets", "/api/optionSets"),
    ("data_sets", "/api/dataSets"),
    ("category_combos", "/api/categoryCombos"),
)
