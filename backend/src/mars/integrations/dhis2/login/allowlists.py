"""Allowlists for login-time DHIS2 metadata.

Narrower than discovery. Login is not a catalogue of the instance: it asks
only for identity, authorities and the organisation-unit metadata required
to resolve geographic scope.
"""

from __future__ import annotations

from typing import Final

from mars.integrations.dhis2.discovery.allowlists import (
    PATIENT_COLLECTION_PATHS,
    SYSTEM_INFO_FIELDS,
)

LOGIN_USER_FIELDS: Final[str] = ",".join(
    (
        "id",
        "username",
        "displayName",
        "authorities",
        "organisationUnits[id,name,code,level,path,organisationUnitGroups[id]]",
        "dataViewOrganisationUnits[id,name,code,level,path,organisationUnitGroups[id]]",
        "teiSearchOrganisationUnits[id,name,code,level,path,organisationUnitGroups[id]]",
    )
)

ALLOWED_HOSTS: Final[frozenset[str]] = frozenset(
    {
        "eregisters.health.go.ug",
        "hmis.health.go.ug",
        # Contract tests only. Never a live Ministry host.
        "dhis2.example.org",
    }
)

ORGANISATION_UNIT_LEVEL_FIELDS: Final[str] = "id,name,level"
ORGANISATION_UNIT_GROUP_FIELDS: Final[str] = "id,name,code"
ORGANISATION_UNIT_GROUP_SET_FIELDS: Final[str] = "id,name,code,organisationUnitGroups[id,name,code]"

DEFAULT_QUERY_KEYS: Final[frozenset[str]] = frozenset({"fields", "paging", "pageSize", "page"})

ALLOWED_ROUTES: Final[dict[str, frozenset[str]]] = {
    "/api/system/info": frozenset({"fields"}),
    "/api/me": frozenset({"fields"}),
    "/api/me/authorization": frozenset(),
    "/api/me/authorities": frozenset(),
    "/api/organisationUnitLevels": DEFAULT_QUERY_KEYS,
    "/api/organisationUnitGroups": DEFAULT_QUERY_KEYS,
    "/api/organisationUnitGroupSets": DEFAULT_QUERY_KEYS,
}

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
    "/api/me": frozenset(
        {
            "id",
            "username",
            "displayName",
            "name",
            "firstName",
            "surname",
            "authorities",
            "organisationUnits",
            "dataViewOrganisationUnits",
            "teiSearchOrganisationUnits",
        }
    ),
    "/api/me/authorization": frozenset({"authorities"}),
    "/api/me/authorities": frozenset({"authorities"}),
    "/api/organisationUnitLevels": frozenset({"organisationUnitLevels", "pager"}),
    "/api/organisationUnitGroups": frozenset({"organisationUnitGroups", "pager"}),
    "/api/organisationUnitGroupSets": frozenset({"organisationUnitGroupSets", "pager"}),
}

PAGER_KEYS: Final[frozenset[str]] = frozenset({"page", "pageCount", "pageSize", "total"})

ORG_UNIT_ITEM_KEYS: Final[frozenset[str]] = frozenset(
    {"id", "name", "code", "level", "path", "leaf", "parent", "organisationUnitGroups"}
)

LEVEL_ITEM_KEYS: Final[frozenset[str]] = frozenset({"id", "name", "level"})
GROUP_ITEM_KEYS: Final[frozenset[str]] = frozenset({"id", "name", "code", "organisationUnitGroups"})

SAFE_METADATA_KEYS: Final[frozenset[str]] = (
    ORG_UNIT_ITEM_KEYS
    | LEVEL_ITEM_KEYS
    | GROUP_ITEM_KEYS
    | frozenset(
        {
            "username",
            "displayName",
            "firstName",
            "surname",
            "authorities",
            "organisationUnits",
            "dataViewOrganisationUnits",
            "teiSearchOrganisationUnits",
        }
    )
)

LOGIN_METADATA_PATHS: Final[tuple[str, ...]] = (
    "/api/system/info",
    "/api/me",
    "/api/me/authorization",
    "/api/organisationUnitLevels",
    "/api/organisationUnitGroups",
    "/api/organisationUnitGroupSets",
)

__all__ = [
    "ALLOWED_HOSTS",
    "ALLOWED_ROUTES",
    "DEFAULT_QUERY_KEYS",
    "GROUP_ITEM_KEYS",
    "LEVEL_ITEM_KEYS",
    "LOGIN_METADATA_PATHS",
    "LOGIN_USER_FIELDS",
    "ORGANISATION_UNIT_GROUP_FIELDS",
    "ORGANISATION_UNIT_GROUP_SET_FIELDS",
    "ORGANISATION_UNIT_LEVEL_FIELDS",
    "ORG_UNIT_ITEM_KEYS",
    "PAGER_KEYS",
    "PATIENT_COLLECTION_PATHS",
    "RESPONSE_KEYS",
    "SAFE_METADATA_KEYS",
    "SYSTEM_INFO_FIELDS",
]
