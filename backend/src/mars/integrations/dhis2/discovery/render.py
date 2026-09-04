"""Markdown rendering and atomic report writing for discovery output."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from mars.integrations.dhis2.discovery.models import DiscoveryReport


def render_markdown(report: DiscoveryReport) -> str:
    lines = [
        "# MARS DHIS2 metadata discovery",
        "",
        report.interpretation_limit,
        "",
        f"- Generated at (UTC): `{report.generated_at.isoformat()}`",
        f"- Origin host: `{report.origin_host}`",
        f"- Client version: `{report.client_version}`",
        f"- Tracker API generation: `{report.api_generation}`",
        "- Patient data: **not retrieved**",
        "",
        "## System",
        "",
    ]
    if report.system:
        for key in (
            "systemName",
            "version",
            "revision",
            "serverDateTime",
            "calendar",
            "dateFormat",
        ):
            if key in report.system:
                lines.append(f"- {key}: `{report.system[key]}`")
    else:
        lines.append("System metadata was not available.")
    lines.extend(["", "## Current user scope", ""])
    user = report.current_user
    lines.append(f"- Username: `{user.get('username') or 'unavailable'}`")
    lines.append(f"- Capture organisation units: {len(report.capture_organisation_units)}")
    lines.append(f"- Data-view organisation units: {len(report.data_view_organisation_units)}")
    lines.append(
        f"- Tracker-search organisation units: {len(report.tracker_search_organisation_units)}"
    )
    facility_count = (
        str(report.accessible_facility_count)
        if report.accessible_facility_count is not None
        else "indeterminate"
    )
    lines.append(f"- Accessible Pader facility candidates: {facility_count}")
    for scope_name in ("capture", "data_view", "tracker_search"):
        count = report.facility_scope_counts.get(scope_name)
        value = str(count) if count is not None else "indeterminate"
        lines.append(f"- Pader facilities in {scope_name} scope: {value}")
    lines.append(f"- Authorities reported: {len(report.authorities)}")
    if report.authorities:
        authorities = ", ".join(f"`{item}`" for item in report.authorities)
        lines.append(f"- Authority identifiers: {authorities}")
    lines.extend(["", "## Pader candidates", ""])
    if report.pader_candidates:
        for unit in report.pader_candidates:
            lines.append(
                f"- `{unit.id}` {unit.name or '(unnamed)'} "
                f"(level={unit.level if unit.level is not None else 'unknown'})"
            )
    else:
        lines.append("No organisation unit name or code contained Pader.")
    lines.extend(["", "## Candidate mappings (proposals only)", ""])
    if report.candidate_mappings:
        for item in report.candidate_mappings:
            lines.append(f"- **{item.kind}** `{item.remote_id}` {item.name or ''} — {item.reason}")
    else:
        lines.append("No candidate mappings were proposed from the retrieved metadata.")
    lines.extend(["", "## Metadata inventory", ""])
    lines.append(f"- Programmes: {len(report.programmes)}")
    for programme in report.programmes:
        lines.append(_metadata_line(programme))
    lines.append(f"- Program stages: {len(report.program_stages)}")
    for stage in report.program_stages:
        lines.append(_metadata_line(stage))
    lines.append(f"- Tracked entity types: {len(report.tracked_entity_types)}")
    for entity_type in report.tracked_entity_types:
        lines.append(_metadata_line(entity_type))
    lines.append(
        f"- Data elements: {len(report.data_elements)} (full metadata is in the JSON report)"
    )
    lines.append(f"- Option sets: {len(report.option_sets)} (full metadata is in the JSON report)")
    lines.extend(["", "## Capability matrix", ""])
    lines.append("| Capability | Route | Status | HTTP | Probed |")
    lines.append("| --- | --- | --- | --- | --- |")
    for record in report.capabilities:
        http_status = record.http_status if record.http_status is not None else "—"
        probed = "yes" if record.probed else "no"
        row = (
            f"| {record.name} | `{record.route}` | {record.status.value} "
            f"| {http_status} | {probed} |"
        )
        lines.append(row)
    lines.extend(["", "## Analytical API summary", ""])
    if report.supported_analytical_apis:
        lines.append(
            "Metadata/version evidence indicates these routes exist; authorization "
            "was not probed and no data request was sent:"
        )
        for route in report.supported_analytical_apis:
            lines.append(f"- `{route}`")
    else:
        lines.append("No analytical route was safely confirmed from metadata/version evidence.")
    lines.extend(["", "## Access limitations", ""])
    for limitation in report.access_limitations:
        lines.append(f"- {limitation}")
    lines.extend(["", "## Unresolved questions", ""])
    for question in report.unresolved_questions:
        lines.append(f"- {question}")
    if report.truncated_collections:
        lines.extend(["", "## Truncated collections", ""])
        lines.append(
            "Page cap reached. The collection is incomplete and must not be treated as a census."
        )
        for name in report.truncated_collections:
            lines.append(f"- {name}")
    if report.errors:
        lines.extend(["", "## Errors", ""])
        for error in report.errors:
            lines.append(f"- {error}")
    lines.extend(
        [
            "",
            "## Mandatory stop",
            "",
            "Discovery has finished. Do not retrieve tracked entities, enrollments, "
            "events, relationships, or patient-level analytics without an explicit "
            "written approval that names the scope, period and high-water-mark rules.",
            "",
        ]
    )
    return "\n".join(lines)


def _metadata_line(item: dict[str, object]) -> str:
    remote_id = item.get("id") or "unavailable-id"
    name = item.get("name") or "(unnamed)"
    code = item.get("code")
    suffix = f"; code `{code}`" if code else ""
    return f"  - `{remote_id}` {name}{suffix}"


def write_reports(report: DiscoveryReport, output_dir: Path) -> tuple[Path, Path]:
    """Write JSON and Markdown atomically into a gitignored directory."""
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = report.generated_at.strftime("%Y%m%dT%H%M%SZ")
    host = report.origin_host.replace(".", "_")
    json_path = output_dir / f"{host}-{stamp}-discovery.json"
    markdown_path = output_dir / f"{host}-{stamp}-discovery.md"
    _atomic_write(json_path, json.dumps(report.sanitized_dict(), indent=2, sort_keys=True) + "\n")
    _atomic_write(markdown_path, render_markdown(report))
    return json_path, markdown_path


def _atomic_write(path: Path, content: str) -> None:
    directory = path.parent
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except Exception:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)
        raise
