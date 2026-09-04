"""DHIS2/eRegisters authentication for the MARS live-pilot login.

This package is deliberately separate from metadata discovery and from the
exchange adapter. Login may request only the identity and organisation-unit
metadata needed to build a MARS principal. It never retrieves tracked
entities, enrollments, events, relationships, patient analytics or data
value sets.

The authentication provider interface is what later Ministry OAuth, a PAT or
a dedicated read-only service account will implement. The pilot uses Basic
authentication over verified HTTPS to an allowlisted host.
"""

from __future__ import annotations

from mars.integrations.dhis2.login.models import LoginSnapshot, RemoteOrgUnit
from mars.integrations.dhis2.login.provider import Dhis2BasicAuthProvider
from mars.security.source_login import AuthenticationProvider

__all__ = [
    "AuthenticationProvider",
    "Dhis2BasicAuthProvider",
    "LoginSnapshot",
    "RemoteOrgUnit",
]
