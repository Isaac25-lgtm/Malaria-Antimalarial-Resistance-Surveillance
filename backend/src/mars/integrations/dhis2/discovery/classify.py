"""Proposal-only classification of discovered DHIS2 metadata.

Nothing here is an accepted mapping. A name match is a candidate for a person
to review, not a reason to retrieve a patient record or to label a figure.
"""

from __future__ import annotations

import re
from typing import Any

from mars.integrations.dhis2.discovery.models import (
    CandidateMapping,
    OrganisationUnitRecord,
)

_PADER = re.compile(r"\bpader\b", re.IGNORECASE)

_OPD = re.compile(
    r"\b(opd|out-?patient|hmis\s*opd|opd\s*002)\b",
    re.IGNORECASE,
)

_MALARIA = re.compile(
    r"\b(malaria|plasmodium|rdt|microscopy|artemether|lumefantrine|"
    r"artesunate|amodiaquine|fansidar|iptp|act)\b",
    re.IGNORECASE,
)

_FACILITY = re.compile(
    r"\b(health\s+cent(?:re|er)|hc\s*ii{0,3}v?|hospital|clinic|dispensary)\b",
    re.IGNORECASE,
)


def compact_unit(raw: dict[str, Any]) -> OrganisationUnitRecord:
    parent_raw = raw.get("parent")
    parent: dict[str, Any] = parent_raw if isinstance(parent_raw, dict) else {}
    groups = raw.get("organisationUnitGroups")
    names: list[str] = []
    if isinstance(groups, list):
        for group in groups:
            if isinstance(group, dict) and isinstance(group.get("name"), str):
                names.append(group["name"])
    record = OrganisationUnitRecord(
        id=str(raw.get("id") or ""),
        name=raw.get("name") if isinstance(raw.get("name"), str) else None,
        code=raw.get("code") if isinstance(raw.get("code"), str) else None,
        level=raw.get("level") if isinstance(raw.get("level"), int) else None,
        path=raw.get("path") if isinstance(raw.get("path"), str) else None,
        leaf=raw.get("leaf") if isinstance(raw.get("leaf"), bool) else None,
        parent_id=parent.get("id") if isinstance(parent.get("id"), str) else None,
        group_names=names,
    )
    return record.model_copy(update={"classification": classify_unit(record)})


def classify_unit(
    unit: OrganisationUnitRecord,
) -> str:
    haystack = " ".join(part for part in (unit.name, unit.code, *unit.group_names) if part)
    looks_like_facility = bool(unit.leaf or _FACILITY.search(haystack))
    if _PADER.search(haystack) and not looks_like_facility:
        return "pader_candidate"
    if looks_like_facility:
        return "candidate_facility"
    return "organisation_unit"


def candidate_mappings(
    *,
    units: list[OrganisationUnitRecord],
    programmes: list[dict[str, Any]],
    data_elements: list[dict[str, Any]],
    attributes: list[dict[str, Any]],
) -> list[CandidateMapping]:
    proposals: list[CandidateMapping] = []
    for unit in units:
        if unit.classification == "pader_candidate":
            proposals.append(
                CandidateMapping(
                    kind="pader_organisation_unit",
                    remote_id=unit.id,
                    name=unit.name,
                    code=unit.code,
                    reason="Name or code contains Pader. Proposal only; not an accepted mapping.",
                )
            )
    for programme in programmes:
        haystack = _haystack(programme)
        if _OPD.search(haystack):
            proposals.append(
                CandidateMapping(
                    kind="opd_programme",
                    remote_id=str(programme.get("id") or ""),
                    name=programme.get("name") if isinstance(programme.get("name"), str) else None,
                    code=programme.get("code") if isinstance(programme.get("code"), str) else None,
                    reason=(
                        "Programme name or code resembles OPD / outpatient. "
                        "Proposal only; not an accepted mapping."
                    ),
                )
            )
    for element in (*data_elements, *attributes):
        haystack = _haystack(element)
        if _MALARIA.search(haystack):
            proposals.append(
                CandidateMapping(
                    kind="malaria_variable",
                    remote_id=str(element.get("id") or ""),
                    name=element.get("name") if isinstance(element.get("name"), str) else None,
                    code=element.get("code") if isinstance(element.get("code"), str) else None,
                    reason=(
                        "Name or code matches a malaria-related token. "
                        "Proposal only; not an indicator definition."
                    ),
                )
            )
    return [item for item in proposals if item.remote_id]


def _haystack(item: dict[str, Any]) -> str:
    parts = [item.get("name"), item.get("code"), item.get("programType")]
    return " ".join(part for part in parts if isinstance(part, str))
