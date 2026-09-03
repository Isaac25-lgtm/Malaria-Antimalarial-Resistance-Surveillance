"""Governed reports and exports — Prompt 25.

A report is the form in which a MARS figure leaves the building. It gets read
in a meeting, pasted into a briefing, and quoted six months later by someone
who never saw the screen it came from. Everything here follows from that.

**Built from the same API objects as the dashboard.** A report and a screen
that disagree is a report nobody can defend, so both compose the same service
records rather than each running their own query.

**An unavailable figure stays unavailable.** The single most dangerous
transformation in this file would be writing 0 where the service returned
``not_configured``, because a spreadsheet cell has nowhere to put a caveat.
Absent figures are rendered as an empty cell with the reason in its own column.

**No direct identifier ever leaves.** Reports are composed of aggregates and
pseudonymous references. There is no code path here that reads the identity
vault, and a module-boundary test enforces it.

**Every export is audited** with the period, scope and product requested, and
the row count returned.

CSV additionally needs protecting from itself: a value beginning ``=``, ``+``,
``-`` or ``@`` is executed as a formula when a spreadsheet opens the file, so
those are prefixed before writing. A surveillance export that runs code on a
district officer's laptop would be a poor way to end this project.
"""

from __future__ import annotations

import csv
import io
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy.orm import Session

from mars.core.errors import ValidationFailedError
from mars.domain.enums import AuditAction
from mars.security.principal import AuthenticatedPrincipal
from mars.services.audit_service import AuditService
from mars.services.surveillance_summary import (
    INTERPRETATION_BOUNDARY,
    SurveillanceSummaryService,
)

#: The products this build can generate. Each maps to composed service records;
#: none introduces a figure the screens do not also show.
NATIONAL_BRIEF = "national_brief"
DISTRICT_BRIEF = "district_brief"

PRODUCTS: tuple[str, ...] = (NATIONAL_BRIEF, DISTRICT_BRIEF)

#: Characters a spreadsheet treats as the start of a formula. A cell beginning
#: with one of these is executed on open, so exported values are prefixed with
#: an apostrophe to keep them inert.
FORMULA_TRIGGERS = ("=", "+", "-", "@", "\t", "\r")


@dataclass(frozen=True, slots=True)
class Report:
    """A generated report, ready to serialise."""

    product: str
    generated_at: datetime
    period_start: date
    period_end: date
    geography_unit_id: uuid.UUID | None
    rows: list[dict[str, Any]]
    interpretation_limit: str
    provenance: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "product": self.product,
            "generated_at": self.generated_at,
            "period_start": self.period_start,
            "period_end": self.period_end,
            "geography_unit_id": self.geography_unit_id,
            "rows": self.rows,
            "interpretation_limit": self.interpretation_limit,
            "provenance": self.provenance,
        }


def sanitise_cell(value: object) -> str:
    """Render a value inert for a spreadsheet.

    A cell beginning ``=``, ``+``, ``-`` or ``@`` is a formula to Excel and
    LibreOffice, and a surveillance export is exactly the kind of file someone
    opens without thinking. Prefixing with an apostrophe keeps the text visible
    and stops it executing.
    """
    if value is None:
        return ""
    text = str(value)
    if text.startswith(FORMULA_TRIGGERS):
        return f"'{text}"
    return text


class ReportService:
    """Generates governed reports under the caller's own scope."""

    def __init__(self, session: Session, audit: AuditService | None = None) -> None:
        self._session = session
        self._summary = SurveillanceSummaryService(session)
        self._audit = audit

    def generate(
        self,
        principal: AuthenticatedPrincipal,
        *,
        product: str,
        period_start: date,
        period_end: date,
        geography_unit_id: uuid.UUID | None = None,
    ) -> Report:
        """Compose one report, applying the caller's scope in the services."""
        if product not in PRODUCTS:
            raise ValidationFailedError(f"Unknown report product: {product}")
        if product == DISTRICT_BRIEF and geography_unit_id is None:
            raise ValidationFailedError("A district brief requires the district it is about.")

        measures = self._summary.kpis(
            principal,
            period_start=period_start,
            period_end=period_end,
            geography_unit_id=geography_unit_id,
        )
        provenance = self._summary.provenance(
            principal, period_start=period_start, period_end=period_end
        )

        rows = [
            {
                "code": measure["code"],
                "label": measure["label"],
                # Absent stays absent. Writing 0 here would put a figure into a
                # briefing that MARS never computed.
                "value": measure["value"],
                "unit": measure["unit"],
                "numerator": measure["numerator"],
                "denominator": measure["denominator"],
                "status": measure["status"],
                "status_detail": measure["status_detail"],
                "period_start": measure["period"]["start"],
                "period_end": measure["period"]["end"],
                "source": measure["source"],
                "method_version_id": measure["method_version_id"],
            }
            for measure in measures
        ]

        report = Report(
            product=product,
            generated_at=datetime.now(UTC),
            period_start=period_start,
            period_end=period_end,
            geography_unit_id=geography_unit_id,
            rows=rows,
            interpretation_limit=INTERPRETATION_BOUNDARY,
            provenance=provenance,
        )

        if self._audit is not None:
            self._audit.record(
                action=AuditAction.REPORT_GENERATED,
                principal=principal,
                object_type="report",
                object_id=product,
                geography_unit_id=geography_unit_id,
                context={
                    "product": product,
                    "period_start": period_start.isoformat(),
                    "period_end": period_end.isoformat(),
                    "rows": len(rows),
                    # The figures themselves are not audited: the log records
                    # that a report was produced and over what, not its
                    # contents.
                },
            )

        return report

    def to_csv(self, report: Report) -> str:
        """Serialise a report as CSV, with every cell rendered inert.

        The interpretation limit is written as a leading comment row so it
        travels with the file. A spreadsheet that loses the caveat is how a
        surveillance figure becomes a claim.
        """
        buffer = io.StringIO()
        writer = csv.writer(buffer, lineterminator="\n")

        writer.writerow([sanitise_cell(f"MARS {report.product}")])
        writer.writerow([sanitise_cell(report.interpretation_limit)])
        writer.writerow(
            [
                sanitise_cell(
                    f"Period {report.period_start} to {report.period_end}; "
                    f"generated {report.generated_at.isoformat()}"
                )
            ]
        )
        writer.writerow([])

        headers = [
            "code",
            "label",
            "value",
            "unit",
            "numerator",
            "denominator",
            "status",
            "status_detail",
            "period_start",
            "period_end",
            "source",
            "method_version_id",
        ]
        writer.writerow(headers)
        for row in report.rows:
            writer.writerow([sanitise_cell(row.get(header)) for header in headers])

        return buffer.getvalue()


__all__ = [
    "DISTRICT_BRIEF",
    "FORMULA_TRIGGERS",
    "NATIONAL_BRIEF",
    "PRODUCTS",
    "Report",
    "ReportService",
    "sanitise_cell",
]
