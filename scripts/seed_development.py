"""Seed the baseline roles and the synthetic development accounts.

``mars.security.dev_users`` has always described "the seeder" that attaches the
development scopes. It did not exist, so ``POST /auth/dev/login`` — which
deliberately refuses to create an account — could never find one, and a freshly
migrated database had no roles at all.

Two things are written, both from the definitions already in the source:

* the five system roles, their permission grants and their sensitivity
  ceilings, taken from ``ROLE_PERMISSIONS`` and ``ROLE_DEFAULT_SENSITIVITY``;
* the synthetic accounts in ``DEVELOPMENT_USERS``, each with its role, its
  geography scope, its sensitivity ceiling and — for the facility account — a
  single facility.

Nothing here invents a permission, a scope or a sensitivity level. If this file
and ``permissions.py`` ever disagree, ``permissions.py`` is right and this is a
bug.

    python scripts/seed_development.py            # seed
    python scripts/seed_development.py --dry-run  # report, write nothing

Refused outright when ``MARS_ENVIRONMENT`` is staging or production: every
account it writes is synthetic, and a synthetic operator in a real audit trail
is exactly the confusion the ``is_synthetic`` flag exists to prevent.

Re-running is safe. An account that already exists is left alone rather than
duplicated, so this can be run after a migration without checking first.

Exit codes: 0 seeded (or already present), 2 refused, 3 the geography has not
been imported.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "backend" / "src"))

from sqlalchemy import delete, select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from mars.core.settings import get_settings  # noqa: E402
from mars.db.session import session_scope  # noqa: E402
from mars.domain.geography import GeographyUnit  # noqa: E402
from mars.domain.organisation import Facility  # noqa: E402
from mars.domain.security import (  # noqa: E402
    Role,
    RolePermission,
    UserAccount,
    UserFacilityScope,
    UserGeographyScope,
    UserRole,
    UserSensitivityScope,
)
from mars.security.dev_users import DEVELOPMENT_USERS, DevelopmentUserSpec  # noqa: E402
from mars.security.permissions import (  # noqa: E402
    ROLE_DEFAULT_SENSITIVITY,
    ROLE_PERMISSIONS,
    SystemRole,
)
from mars.security.providers import DevelopmentTokenVerifier  # noqa: E402
from mars.services.auth_service import AuthService  # noqa: E402

#: Written to ``granted_by`` so the audit trail names the mechanism rather than
#: a person. No human granted these.
GRANTED_BY = "seed_development.py"

ROLE_LABELS: dict[SystemRole, str] = {
    SystemRole.NATIONAL_PROGRAMME: "National Programme",
    SystemRole.DISTRICT_HSD: "District / Health Sub-District",
    SystemRole.FACILITY: "Facility",
    SystemRole.ANALYST: "Surveillance Analyst",
    SystemRole.ADMINISTRATOR: "System Administrator",
}


def seed_roles(session: Session, *, dry_run: bool) -> tuple[int, int]:
    """Create any missing system role with its canonical permission set."""
    created = 0
    present = 0
    for role_code, permissions in ROLE_PERMISSIONS.items():
        existing = session.execute(
            select(Role).where(Role.code == role_code.value)
        ).scalar_one_or_none()
        if existing is not None:
            present += 1
            if dry_run:
                continue
            role = existing
            session.execute(delete(RolePermission).where(RolePermission.role_id == role.id))
        else:
            created += 1
            if dry_run:
                continue
            role = Role(code=role_code.value)
            session.add(role)
        role.label = ROLE_LABELS[role_code]
        role.description = f"Baseline {ROLE_LABELS[role_code]} role, seeded from ROLE_PERMISSIONS."
        role.is_system_role = True
        role.max_sensitivity = ROLE_DEFAULT_SENSITIVITY[role_code]
        session.flush()
        for permission in sorted(permissions, key=lambda p: p.value):
            session.add(RolePermission(role_id=role.id, permission=permission))
        session.flush()
    return created, present


def _geography_unit(session: Session, code: str) -> GeographyUnit | None:
    return session.execute(
        select(GeographyUnit).where(GeographyUnit.preferred_code == code)
    ).scalar_one_or_none()


def _country_unit(session: Session) -> GeographyUnit | None:
    return (
        session.execute(select(GeographyUnit).where(GeographyUnit.level == "country"))
        .scalars()
        .first()
    )


def _facility_in(session: Session, unit: GeographyUnit) -> Facility | None:
    """One facility inside the given unit, chosen by code so it is stable.

    The facility account must see exactly one facility. Which one does not
    matter, but it must be the same one on every run, or a scope test that
    passes today fails tomorrow for no reason anyone can find.
    """
    return (
        session.execute(
            select(Facility)
            .where(Facility.district_geography_unit_id == unit.id)
            .order_by(Facility.code)
        )
        .scalars()
        .first()
    )


def seed_user(session: Session, spec: DevelopmentUserSpec, *, dry_run: bool) -> str:
    """Create one synthetic account. Returns a one-line report."""
    subject = f"{DevelopmentTokenVerifier.SUBJECT_PREFIX}{spec.username}"
    existing = session.execute(
        select(UserAccount).where(UserAccount.subject == subject)
    ).scalar_one_or_none()
    if dry_run:
        action = "would reconcile" if existing is not None else "would create"
        return f"  {spec.username:<22} {action}"

    service = AuthService(session)
    if existing is None:
        user = service.create_user(
            subject=subject,
            username=spec.username,
            display_name=spec.display_name,
            issuer=DevelopmentTokenVerifier.ISSUER,
            is_synthetic=True,
            organisation_label="MARS development",
        )
        action = "created"
    else:
        if not existing.is_synthetic:
            raise RuntimeError(f"refusing to rewrite non-synthetic account {spec.username!r}")
        user = existing
        user.username = spec.username
        user.display_name = spec.display_name
        user.issuer = DevelopmentTokenVerifier.ISSUER
        user.organisation_label = "MARS development"
        user.is_active = True
        # These definitions are authoritative only for synthetic accounts.
        # Rebuilding them also removes stale facility IDs after a demo reset.
        session.execute(delete(UserRole).where(UserRole.user_id == user.id))
        session.execute(delete(UserGeographyScope).where(UserGeographyScope.user_id == user.id))
        session.execute(delete(UserFacilityScope).where(UserFacilityScope.user_id == user.id))
        session.execute(delete(UserSensitivityScope).where(UserSensitivityScope.user_id == user.id))
        session.flush()
        action = "reconciled"
    service.assign_role(user=user, role_code=spec.role.value, granted_by=GRANTED_BY)
    service.set_sensitivity_scope(
        user=user,
        level=spec.max_sensitivity,
        granted_by=GRANTED_BY,
        reason=spec.sensitivity_reason or "synthetic development account",
    )

    notes: list[str] = [spec.role.value]

    # A national account is scoped by attaching the country unit, not by a flag.
    # Scope is always a row, so there is one rule to reason about.
    unit = (
        _country_unit(session)
        if spec.geography_code is None
        else _geography_unit(session, spec.geography_code)
    )
    if unit is None:
        notes.append("NO GEOGRAPHY SCOPE (unit not imported)")
    else:
        service.grant_geography_scope(
            user=user,
            geography_unit_id=unit.id,
            granted_by=GRANTED_BY,
            reason=spec.scope_description,
        )
        notes.append(f"geography={unit.raw_name}")

    if spec.facility_scoped:
        facility = _facility_in(session, unit) if unit is not None else None
        if facility is None:
            notes.append("NO FACILITY SCOPE (none registered in that district)")
        else:
            service.grant_facility_scope(user=user, facility_id=facility.id, granted_by=GRANTED_BY)
            notes.append(f"facility={facility.code}")

    return f"  {spec.username:<22} {action:<10} {', '.join(notes)}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would be written, write nothing",
    )
    args = parser.parse_args(argv)

    settings = get_settings()
    if settings.environment.is_protected:
        print(
            f"REFUSED: environment is {settings.environment.value}. "
            "Every account this seeds is synthetic.",
            file=sys.stderr,
        )
        return 2

    with session_scope() as session:
        if _country_unit(session) is None:
            print(
                "ERROR: no geography has been imported, so no account can be scoped.\n"
                "  Run mars-import-geography first.",
                file=sys.stderr,
            )
            return 3

        created, present = seed_roles(session, dry_run=args.dry_run)
        print(f"roles: {created} created, {present} already present")

        print("accounts:")
        for spec in DEVELOPMENT_USERS:
            print(seed_user(session, spec, dry_run=args.dry_run))

        if args.dry_run:
            session.rollback()
            print("\ndry run: nothing was written")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
