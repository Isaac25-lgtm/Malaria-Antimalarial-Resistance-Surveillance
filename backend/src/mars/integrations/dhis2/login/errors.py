"""Login-adapter failures. None of these carry an upstream body."""

from mars.security.source_login import (
    INVALID_CREDENTIALS_DETAIL,
    UPSTREAM_UNAVAILABLE_DETAIL,
)
from mars.security.source_login import (
    SourceLoginError as LoginAdapterError,
)

__all__ = [
    "INVALID_CREDENTIALS_DETAIL",
    "UPSTREAM_UNAVAILABLE_DETAIL",
    "LoginAdapterError",
]
