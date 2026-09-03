"""Governed report generation — Prompt 25.

Server-authorised, scope-applied, audited. A report is the form in which a MARS
figure leaves the building, so the same rules that govern a screen govern this:
an unavailable measure stays unavailable, the interpretation limit travels with
the file, and no direct identifier is ever included.
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Response

from mars.api.dependencies import ReportServiceDep, require_permissions
from mars.api.v1.schemas import GeneratedReport
from mars.security.permissions import Permission
from mars.security.principal import AuthenticatedPrincipal

router = APIRouter(prefix="/reports", tags=["reports"])

ReportAuthor = Annotated[
    AuthenticatedPrincipal,
    Depends(
        require_permissions(Permission.REPORT_GENERATE, Permission.SURVEILLANCE_VIEW_AGGREGATE)
    ),
]
Exporter = Annotated[
    AuthenticatedPrincipal,
    Depends(require_permissions(Permission.DATA_EXPORT, Permission.SURVEILLANCE_VIEW_AGGREGATE)),
]

Product = Literal["national_brief", "district_brief"]


@router.get("/{product}", response_model=GeneratedReport)
def generate_report(
    product: Product,
    principal: ReportAuthor,
    service: ReportServiceDep,
    period_start: date,
    period_end: date,
    geography_unit_id: uuid.UUID | None = None,
) -> GeneratedReport:
    """One governed report, composed from the same records the screens show."""
    report = service.generate(
        principal,
        product=product,
        period_start=period_start,
        period_end=period_end,
        geography_unit_id=geography_unit_id,
    )
    return GeneratedReport.model_validate(report.as_dict())


@router.get(
    "/{product}/export.csv",
    response_class=Response,
    responses={200: {"content": {"text/csv": {}}, "description": "CSV export"}},
)
def export_report(
    product: Product,
    principal: Exporter,
    service: ReportServiceDep,
    period_start: date,
    period_end: date,
    geography_unit_id: uuid.UUID | None = None,
) -> Response:
    """The same report as CSV.

    Requires the export permission separately from report generation: reading
    a figure on screen and carrying it out of the system in a file are
    different acts with different risks.
    """
    report = service.generate(
        principal,
        product=product,
        period_start=period_start,
        period_end=period_end,
        geography_unit_id=geography_unit_id,
    )
    body = service.to_csv(report)
    filename = f"mars-{product}-{period_start}-{period_end}.csv"
    return Response(
        content=body,
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            # An export is per-principal and scope-dependent. A shared cache
            # holding one district officer's file for the next is exactly the
            # leak this header exists to prevent.
            "Cache-Control": "no-store",
        },
    )


__all__ = ["router"]
