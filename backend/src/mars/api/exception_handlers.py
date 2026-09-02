"""Exception handlers producing RFC 7807 problem documents.

Every error path returns the same shape. A client can therefore branch on
``code`` rather than parsing prose, and the request identifier always makes it
back to the caller so a support conversation can start from a log line.
"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from mars.core.context import get_request_id
from mars.core.errors import (
    PROBLEM_BASE_URI,
    PROBLEM_CONTENT_TYPE,
    FieldError,
    MarsError,
    ProblemDetail,
)
from mars.core.logging import get_logger

logger = get_logger("mars.errors")

#: Titles for status codes raised outside the MarsError family.
_HTTP_TITLES: dict[int, tuple[str, str]] = {
    400: ("bad_request", "Bad request"),
    401: ("unauthenticated", "Authentication required"),
    403: ("permission_denied", "Permission denied"),
    404: ("not_found", "Resource not found"),
    405: ("method_not_allowed", "Method not allowed"),
    409: ("conflict", "Conflicting state"),
    422: ("validation_failed", "Request validation failed"),
    429: ("rate_limited", "Too many requests"),
    500: ("internal_error", "Internal server error"),
    503: ("service_unavailable", "Service temporarily unavailable"),
}


def _respond(problem: ProblemDetail) -> JSONResponse:
    return JSONResponse(
        status_code=problem.status,
        content=problem.model_dump(exclude_none=True),
        media_type=PROBLEM_CONTENT_TYPE,
    )


async def handle_mars_error(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, MarsError)
    problem = exc.to_problem(instance=request.url.path, request_id=get_request_id())
    if problem.status >= 500:
        logger.error("mars_error", code=exc.code, status=problem.status, detail=exc.detail)
    else:
        logger.info("mars_error", code=exc.code, status=problem.status)
    return _respond(problem)


async def handle_validation_error(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, RequestValidationError)
    field_errors = [
        FieldError(
            field=".".join(str(part) for part in error.get("loc", ()) if part != "body"),
            message=str(error.get("msg", "invalid value")),
            code=str(error.get("type")) if error.get("type") else None,
        )
        for error in exc.errors()
    ]
    problem = ProblemDetail(
        type=f"{PROBLEM_BASE_URI}/validation_failed",
        title="Request validation failed",
        status=422,
        code="validation_failed",
        detail="One or more values in the request are not valid.",
        instance=request.url.path,
        request_id=get_request_id(),
        errors=field_errors,
    )
    return _respond(problem)


async def handle_http_exception(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, StarletteHTTPException)
    code, title = _HTTP_TITLES.get(exc.status_code, ("http_error", "Request failed"))
    problem = ProblemDetail(
        type=f"{PROBLEM_BASE_URI}/{code}",
        title=title,
        status=exc.status_code,
        code=code,
        detail=str(exc.detail) if exc.detail else None,
        instance=request.url.path,
        request_id=get_request_id(),
    )
    return _respond(problem)


async def handle_unexpected_error(request: Request, exc: Exception) -> JSONResponse:
    """Last resort.

    The exception is logged in full server-side; the client receives only the
    request identifier. Never return a stack trace or a source payload.
    """
    logger.exception("unhandled_exception", path=request.url.path, error_type=type(exc).__name__)
    problem = ProblemDetail(
        type=f"{PROBLEM_BASE_URI}/internal_error",
        title="Internal server error",
        status=500,
        code="internal_error",
        detail="An unexpected error occurred. Quote the request ID when reporting this.",
        instance=request.url.path,
        request_id=get_request_id(),
    )
    return _respond(problem)


def register_exception_handlers(app: FastAPI) -> None:
    app.add_exception_handler(MarsError, handle_mars_error)
    app.add_exception_handler(RequestValidationError, handle_validation_error)
    app.add_exception_handler(StarletteHTTPException, handle_http_exception)
    app.add_exception_handler(Exception, handle_unexpected_error)
