"""Pseudonymous patient surveillance reads."""

from __future__ import annotations

import uuid
from datetime import date
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query

from mars.api.dependencies import PatientSurveillanceDep, require_permissions, require_sensitivity
from mars.api.v1.schemas import PatientOfInterestPage, PatientTimeline
from mars.security.permissions import Permission, SensitivityLevel
from mars.security.principal import AuthenticatedPrincipal

router = APIRouter(prefix="/patients", tags=["patient surveillance"])
PatientReader = Annotated[
    AuthenticatedPrincipal,
    Depends(require_permissions(Permission.CASE_EVIDENCE_VIEW)),
]


@router.get(
    "",
    response_model=PatientOfInterestPage,
    dependencies=[Depends(require_sensitivity(SensitivityLevel.PSEUDONYMOUS_CASE))],
)
def patients_of_interest(
    principal: PatientReader,
    service: PatientSurveillanceDep,
    period_from: date | None = None,
    period_to: date | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    cursor: Annotated[str | None, Query(max_length=512)] = None,
    determination: Literal["qualifies", "does_not_qualify", "indeterminate"] | None = None,
) -> PatientOfInterestPage:
    """One page of positive encounter histories under stable MARS aliases.

    The whole scoped cohort is evaluated first; ``total`` is independent of
    ``limit``, and the cursor refuses to continue over changed evidence.
    """
    return PatientOfInterestPage.model_validate(
        service.patients_page(
            principal,
            period_from=period_from,
            period_to=period_to,
            limit=limit,
            cursor=cursor,
            determination=determination,
        )
    )


@router.get(
    "/{patient_reference_id}",
    response_model=PatientTimeline,
    dependencies=[Depends(require_sensitivity(SensitivityLevel.PSEUDONYMOUS_CASE))],
)
def patient_timeline(
    patient_reference_id: uuid.UUID,
    principal: PatientReader,
    service: PatientSurveillanceDep,
) -> PatientTimeline:
    """An authorised longitudinal encounter timeline, still pseudonymous."""
    return PatientTimeline.model_validate(
        service.timeline(principal, patient_reference_id=patient_reference_id)
    )


__all__ = ["router"]
