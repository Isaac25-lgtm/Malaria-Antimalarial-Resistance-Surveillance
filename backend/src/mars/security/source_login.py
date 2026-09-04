"""Source-login metadata and the replaceable authentication provider.

These types are MARS's own words for a successful identity round-trip. The
DHIS2 adapter fills them; a later Ministry OAuth or PAT provider can fill
the same shapes without the rest of MARS importing an external system.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from mars.domain.enums import IntegrationErrorCategory

INVALID_CREDENTIALS_DETAIL = "Invalid username or password"
UPSTREAM_UNAVAILABLE_DETAIL = "Unable to connect to eRegisters"


@dataclass(frozen=True, slots=True)
class RemoteOrgUnit:
    """One organisation unit as the source described it at login."""

    uid: str
    name: str | None
    code: str | None
    level: int | None
    path: str | None
    group_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class RemoteOrgUnitLevel:
    number: int
    name: str
    uid: str | None = None


@dataclass(frozen=True, slots=True)
class RemoteOrgUnitGroup:
    uid: str
    name: str | None
    code: str | None
    member_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class LoginSnapshot:
    """Everything MARS may keep from a successful login-metadata round-trip.

    Credentials are not a field. The caller that still holds the password is
    responsible for handing it to the in-memory credential holder, or dropping
    it, immediately after this snapshot is built.
    """

    remote_user_id: str
    username: str
    display_name: str
    authorities: tuple[str, ...]
    organisation_units: tuple[RemoteOrgUnit, ...]
    data_view_organisation_units: tuple[RemoteOrgUnit, ...]
    tei_search_organisation_units: tuple[RemoteOrgUnit, ...]
    organisation_unit_levels: tuple[RemoteOrgUnitLevel, ...]
    organisation_unit_groups: tuple[RemoteOrgUnitGroup, ...]
    system_name: str | None
    system_version: str | None
    requested_paths: tuple[str, ...]
    extra: dict[str, Any] = field(default_factory=dict)

    def all_assigned_units(self) -> tuple[RemoteOrgUnit, ...]:
        """Unique organisation units attached to the account, in first-seen order."""
        seen: set[str] = set()
        ordered: list[RemoteOrgUnit] = []
        for unit in (
            *self.organisation_units,
            *self.data_view_organisation_units,
            *self.tei_search_organisation_units,
        ):
            if unit.uid in seen:
                continue
            seen.add(unit.uid)
            ordered.append(unit)
        return tuple(ordered)


class SourceLoginError(RuntimeError):
    """A source-login failure that must not carry an upstream body."""

    def __init__(
        self,
        category: IntegrationErrorCategory,
        message: str,
        *,
        status_code: int | None = None,
        requested_path: str | None = None,
    ) -> None:
        super().__init__(message)
        self.category = category
        self.status_code = status_code
        self.requested_path = requested_path

    @property
    def is_invalid_credentials(self) -> bool:
        return self.category in {
            IntegrationErrorCategory.AUTHENTICATION,
            IntegrationErrorCategory.AUTHORISATION,
        }

    @property
    def is_unavailable(self) -> bool:
        return self.category in {
            IntegrationErrorCategory.TIMEOUT,
            IntegrationErrorCategory.TRANSPORT,
            IntegrationErrorCategory.REMOTE_SERVER_ERROR,
            IntegrationErrorCategory.MALFORMED_RESPONSE,
            IntegrationErrorCategory.RESPONSE_TOO_LARGE,
            IntegrationErrorCategory.RATE_LIMITED,
            IntegrationErrorCategory.NOT_FOUND,
            IntegrationErrorCategory.MAPPING_INCOMPLETE,
        }


class AuthenticationProvider(ABC):
    """Verifies a username and password against an upstream identity source."""

    method: str

    @abstractmethod
    def authenticate(self, username: str, password: str) -> LoginSnapshot:
        """Return login metadata or raise :class:`SourceLoginError`."""


__all__ = [
    "INVALID_CREDENTIALS_DETAIL",
    "UPSTREAM_UNAVAILABLE_DETAIL",
    "AuthenticationProvider",
    "LoginSnapshot",
    "RemoteOrgUnit",
    "RemoteOrgUnitGroup",
    "RemoteOrgUnitLevel",
    "SourceLoginError",
]
