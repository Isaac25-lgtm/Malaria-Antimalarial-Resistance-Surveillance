"""Liveness and readiness endpoints.

Liveness answers "is this process running". Readiness answers "can it serve
traffic", which means the database must be reachable. Readiness fails loudly
when it is not: a surveillance system that silently serves an empty dashboard is
worse than one that reports itself unavailable.
"""

from __future__ import annotations

from fastapi import APIRouter, Response, status
from sqlalchemy import text

from mars.api.dependencies import SettingsDep
from mars.api.v1.schemas import DependencyStatus, LivenessResponse, ReadinessResponse
from mars.core.logging import get_logger
from mars.core.timeutils import utc_now
from mars.db.session import check_database, get_engine

router = APIRouter(tags=["health"])
logger = get_logger(__name__)


@router.get(
    "/health/live",
    response_model=LivenessResponse,
    summary="Liveness probe",
)
def liveness(settings: SettingsDep) -> LivenessResponse:
    """Return 200 whenever the process is able to serve a request."""
    return LivenessResponse(status="alive", service=settings.app_name)


@router.get(
    "/health/ready",
    response_model=ReadinessResponse,
    summary="Readiness probe",
    responses={503: {"description": "A required dependency is unavailable."}},
)
def readiness(settings: SettingsDep, response: Response) -> ReadinessResponse:
    """Check backing services and report each one individually.

    PostGIS absence is reported as ``not_installed`` rather than as a failure.
    The schema work of phases 1-2 does not need it; the geography importer
    (Prompt 5) checks for it explicitly and refuses to run without it.
    """
    dependencies: list[DependencyStatus] = []
    overall = "ready"

    try:
        with get_engine().connect() as connection:
            info = check_database(connection)
            dependencies.append(
                DependencyStatus(
                    name="postgresql",
                    status="ok",
                    version=str(info["server_version"]),
                )
            )
            if info["postgis_available"]:
                dependencies.append(
                    DependencyStatus(
                        name="postgis",
                        status="ok",
                        version=str(info["postgis_version"]),
                    )
                )
            else:
                dependencies.append(
                    DependencyStatus(
                        name="postgis",
                        status="not_installed",
                        detail=(
                            "The PostGIS extension is not installed. Schema operations "
                            "work without it; geography import requires it."
                        ),
                    )
                )
                overall = "degraded"
    except Exception as exc:
        logger.error("readiness_database_unavailable", error_type=type(exc).__name__)
        dependencies.append(
            DependencyStatus(
                name="postgresql",
                status="unavailable",
                detail="The database could not be reached.",
            )
        )
        overall = "unavailable"

    if settings.redis_url:
        dependencies.append(
            DependencyStatus(
                name="redis",
                status="not_checked",
                detail="Not on the critical path in phases 1-2.",
            )
        )

    if overall == "unavailable":
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE

    return ReadinessResponse(
        status=overall,
        checked_at=utc_now(),
        dependencies=dependencies,
    )


@router.get(
    "/health/schema",
    summary="Report which MARS schemas exist",
    responses={503: {"description": "The database could not be reached."}},
)
def schema_state(response: Response) -> dict[str, object]:
    """List the MARS schemas present in the connected database.

    Used by the development shell and by deployment smoke checks to confirm that
    migrations have been applied.
    """
    from mars.db.schemas import ALL_SCHEMAS, SCHEMA_PURPOSE

    try:
        with get_engine().connect() as connection:
            rows = (
                connection.execute(
                    text(
                        "SELECT schema_name FROM information_schema.schemata "
                        "WHERE schema_name = ANY(:names)"
                    ),
                    {"names": list(ALL_SCHEMAS)},
                )
                .scalars()
                .all()
            )
        present = set(rows)
    except Exception:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {"status": "unavailable", "detail": "The database could not be reached."}

    return {
        "status": "ok" if present == set(ALL_SCHEMAS) else "incomplete",
        "schemas": [
            {
                "name": name,
                "present": name in present,
                "purpose": SCHEMA_PURPOSE[name],
            }
            for name in ALL_SCHEMAS
        ],
    }
