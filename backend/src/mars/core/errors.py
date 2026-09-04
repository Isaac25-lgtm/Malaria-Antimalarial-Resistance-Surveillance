"""Error model.

MARS returns RFC 7807 ``application/problem+json`` responses with a stable
machine-readable ``code``, a human message, optional field errors and the
request identifier. Stack traces and source payloads never reach a client
(blueprint appendix 134).
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

PROBLEM_CONTENT_TYPE = "application/problem+json"
PROBLEM_BASE_URI = "https://mars.health/problems"


class FieldError(BaseModel):
    """A single field-level validation failure."""

    field: str
    message: str
    code: str | None = None


class ProblemDetail(BaseModel):
    """RFC 7807 problem document, extended with MARS diagnostics."""

    type: str = Field(description="URI reference identifying the problem type.")
    title: str = Field(description="Short, human-readable summary.")
    status: int = Field(description="HTTP status code.")
    code: str = Field(description="Stable machine-readable MARS error code.")
    detail: str | None = Field(default=None, description="Human-readable explanation.")
    instance: str | None = Field(default=None, description="Request path.")
    request_id: str | None = Field(default=None, description="Correlates with server logs.")
    errors: list[FieldError] | None = Field(default=None, description="Field-level failures.")
    documentation: str | None = Field(default=None, description="Where to read more.")


class MarsError(Exception):
    """Base class for errors that map onto a problem document."""

    status_code: int = 500
    code: str = "internal_error"
    title: str = "Internal server error"
    documentation: str | None = None

    def __init__(
        self,
        detail: str | None = None,
        *,
        errors: list[FieldError] | None = None,
        context: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(detail or self.title)
        self.detail = detail
        self.errors = errors
        self.context = context or {}

    def to_problem(self, *, instance: str | None, request_id: str | None) -> ProblemDetail:
        return ProblemDetail(
            type=f"{PROBLEM_BASE_URI}/{self.code}",
            title=self.title,
            status=self.status_code,
            code=self.code,
            detail=self.detail,
            instance=instance,
            request_id=request_id,
            errors=self.errors,
            documentation=self.documentation,
        )


class ValidationFailedError(MarsError):
    status_code = 422
    code = "validation_failed"
    title = "Request validation failed"


class NotFoundError(MarsError):
    status_code = 404
    code = "not_found"
    title = "Resource not found"


class ConflictError(MarsError):
    status_code = 409
    code = "conflict"
    title = "Conflicting state"


class UnauthenticatedError(MarsError):
    status_code = 401
    code = "unauthenticated"
    title = "Authentication required"


class PermissionDeniedError(MarsError):
    """The caller is authenticated but lacks the required permission.

    The message deliberately names the missing permission rather than the
    resource, so a denial never confirms that a resource exists.
    """

    status_code = 403
    code = "permission_denied"
    title = "Permission denied"


class GeographyScopeDeniedError(MarsError):
    """The caller's geography scope does not cover the requested area."""

    status_code = 403
    code = "geography_scope_denied"
    title = "Outside your geography scope"


class SensitivityScopeDeniedError(MarsError):
    """The caller may view aggregates but not this level of patient detail."""

    status_code = 403
    code = "sensitivity_scope_denied"
    title = "Outside your data sensitivity scope"


class ServiceUnavailableError(MarsError):
    status_code = 503
    code = "service_unavailable"
    title = "Service temporarily unavailable"


class DependencyUnavailableError(ServiceUnavailableError):
    """A required backing service (database, cache) is not reachable."""

    code = "dependency_unavailable"
    title = "A required dependency is unavailable"


class FeatureDisabledError(MarsError):
    """The requested capability is switched off in this deployment."""

    status_code = 503
    code = "feature_disabled"
    title = "Feature is disabled in this deployment"


class RateLimitedError(MarsError):
    """The caller has exceeded a conservative request budget."""

    status_code = 429
    code = "rate_limited"
    title = "Too many requests"


class OriginRejectedError(MarsError):
    """The request did not come from an approved MARS frontend origin."""

    status_code = 403
    code = "origin_rejected"
    title = "Request rejected"


class CsrfRejectedError(MarsError):
    """An unsafe request was missing or mismatched a CSRF token."""

    status_code = 403
    code = "csrf_rejected"
    title = "Request rejected"


class UpstreamUnavailableError(ServiceUnavailableError):
    """An approved upstream system could not be reached.

    The detail is a sanitised operator message. Upstream bodies, stack traces
    and implementation strings never travel with this error.
    """

    code = "upstream_unavailable"
    title = "Upstream service unavailable"
