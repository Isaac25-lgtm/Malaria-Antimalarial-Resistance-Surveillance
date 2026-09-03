"""DHIS2 run bookkeeping against live PostgreSQL.

The adapter's own behaviour is covered without a database. What needs a real
database is the part that survives a process restart: that a run is recorded as
it progresses, that an interrupted pull resumes from its cursor rather than
starting again, that re-pulling the same scope is recognisably the same
exchange, and that an unresolved identifier becomes one visible proposal rather
than a silent match.

Nothing here contacts a network. The DHIS2 side is scripted.

Requires ``MARS_TEST_DATABASE_URL``. Without it every test here skips.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine, func, select, text
from sqlalchemy.orm import Session, sessionmaker

from mars.domain.enums import (
    AliasMatchStatus,
    IntegrationErrorCategory,
    IntegrationResource,
    IntegrationRunStatus,
    MappingProposalStatus,
)
from mars.domain.integration import IntegrationMappingProposal, IntegrationRun
from mars.integrations.dhis2.client import Dhis2Error
from mars.integrations.dhis2.mapping import Dhis2Crosswalk
from mars.integrations.dhis2.service import Dhis2SyncService, SyncOptions
from mars.integrations.ports import (
    RemoteDataValue,
    RemoteOrganisationUnit,
    RemotePage,
    RemoteScope,
)

pytestmark = pytest.mark.integration

MIGRATIONS_ROOT = Path(__file__).resolve().parents[2]

BOUNDARY_VERSION_ID = uuid.UUID("aa000000-0000-4000-8000-0000000000ff")
COUNTRY_ID = uuid.UUID("aa000000-0000-4000-8000-000000000010")
DISTRICT_ID = uuid.UUID("aa000000-0000-4000-8000-000000000011")
ORG_UNIT_ID = uuid.UUID("aa000000-0000-4000-8000-000000000020")
FACILITY_ID = uuid.UUID("aa000000-0000-4000-8000-000000000001")

#: The DHIS2 UID a person has accepted as meaning our fixture facility.
MAPPED_UID = "OuMappedAAA"
#: A UID nobody has mapped. It must stay unresolved, not become the nearest name.
UNMAPPED_UID = "OuUnknownBB"


@pytest.fixture(scope="module")
def dhis2_engine(integration_database_url: str) -> Iterator[Engine]:
    engine = create_engine(integration_database_url, future=True)
    config = Config(str(MIGRATIONS_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(MIGRATIONS_ROOT / "migrations"))
    config.set_main_option("sqlalchemy.url", integration_database_url)
    command.upgrade(config, "head")
    yield engine
    command.downgrade(config, "base")
    engine.dispose()


@pytest.fixture(scope="module", autouse=True)
def reference_data(dhis2_engine: Engine) -> None:
    """A district, a facility, and one accepted DHIS2 mapping for each."""
    with dhis2_engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO mars_core.boundary_version
                    (id, code, label, source_name, storage_crs, import_status,
                     imported_at, imported_by, created_at, updated_at)
                VALUES (:id, 'TEST-DHIS2-0001', 'DHIS2 fixture', 'synthetic',
                        'EPSG:4326', 'published', now(), 'test', now(), now())
                ON CONFLICT DO NOTHING
                """
            ),
            {"id": BOUNDARY_VERSION_ID},
        )
        for unit_id, level, code, name, parent, depth, path in (
            (COUNTRY_ID, "country", "UG", "Testland", None, 0, "UG"),
            (DISTRICT_ID, "district", "701", "Exchangeville", COUNTRY_ID, 1, "UG/701"),
        ):
            connection.execute(
                text(
                    """
                    INSERT INTO mars_core.geography_unit
                        (id, boundary_version_id, level, unit_kind, preferred_code,
                         raw_name, normalised_name, parent_id, depth, path,
                         is_active, created_at, updated_at)
                    VALUES (:id, :version, :level, 'unspecified', :code, :name,
                            :normalised, :parent, :depth, :path, true, now(), now())
                    ON CONFLICT DO NOTHING
                    """
                ),
                {
                    "id": unit_id,
                    "version": BOUNDARY_VERSION_ID,
                    "level": level,
                    "code": code,
                    "name": name,
                    "normalised": name.lower(),
                    "parent": parent,
                    "depth": depth,
                    "path": path,
                },
            )
        connection.execute(
            text(
                """
                INSERT INTO mars_core.organisation_unit
                    (id, unit_type, code, raw_name, normalised_name, depth, path,
                     is_active, created_at, updated_at)
                VALUES (:id, 'district_health_office', 'DHO-701', 'Exchangeville DHO',
                        'exchangeville dho', 0, 'DHO-701', true, now(), now())
                ON CONFLICT DO NOTHING
                """
            ),
            {"id": ORG_UNIT_ID},
        )
        connection.execute(
            text(
                """
                INSERT INTO mars_core.facility
                    (id, organisation_unit_id, district_geography_unit_id, code,
                     raw_name, normalised_name, facility_level, ownership,
                     coordinate_validated, is_active, is_synthetic, created_at, updated_at)
                VALUES (:id, :org, :geo, 'HF-701', 'Exchangeville HC',
                        'exchangeville hc', 'hc_iii', 'government', false, true, true,
                        now(), now())
                ON CONFLICT DO NOTHING
                """
            ),
            {"id": FACILITY_ID, "org": ORG_UNIT_ID, "geo": DISTRICT_ID},
        )
        # One accepted mapping. Nothing else resolves.
        connection.execute(
            text(
                """
                INSERT INTO mars_core.geography_unit_alias
                    (id, geography_unit_id, source_system, source_code, source_name,
                     match_status, created_at, updated_at)
                VALUES (gen_random_uuid(), :geo, 'dhis2', :uid, 'Exchangeville',
                        :status, now(), now())
                ON CONFLICT DO NOTHING
                """
            ),
            {"geo": DISTRICT_ID, "uid": MAPPED_UID, "status": AliasMatchStatus.CONFIRMED.value},
        )
        connection.execute(
            text(
                """
                INSERT INTO mars_core.facility_identifier
                    (id, facility_id, source_system, external_id, external_name,
                     is_primary, created_at, updated_at)
                VALUES (gen_random_uuid(), :facility, 'dhis2', :uid, 'Exchangeville HC',
                        true, now(), now())
                ON CONFLICT DO NOTHING
                """
            ),
            {"facility": FACILITY_ID, "uid": MAPPED_UID},
        )


@pytest.fixture
def session(dhis2_engine: Engine) -> Iterator[Session]:
    factory = sessionmaker(bind=dhis2_engine, expire_on_commit=False, future=True)
    with factory() as db:
        yield db
        db.rollback()


@pytest.fixture(autouse=True)
def clean(dhis2_engine: Engine) -> Iterator[None]:
    yield
    with dhis2_engine.begin() as connection:
        connection.execute(text("DELETE FROM mars_core.integration_mapping_proposal"))
        connection.execute(text("DELETE FROM mars_core.integration_run"))


# ---------------------------------------------------------------------------
# A scripted DHIS2
# ---------------------------------------------------------------------------
class FakeDhis2:
    """Returns scripted pages and records what was asked for."""

    def __init__(
        self,
        org_unit_pages: list[list[RemoteOrganisationUnit]] | None = None,
        data_value_pages: list[list[RemoteDataValue]] | None = None,
        fail_on_page: int | None = None,
    ) -> None:
        self._org_pages = org_unit_pages or []
        self._value_pages = data_value_pages or []
        self._fail_on_page = fail_on_page
        self.requested_cursors: list[str | None] = []

    def _page(self, pages: list[list[Any]], cursor: str | None, label: str) -> RemotePage:
        self.requested_cursors.append(cursor)
        index = int(cursor) - 1 if cursor else 0
        if self._fail_on_page is not None and index + 1 == self._fail_on_page:
            raise Dhis2Error(
                IntegrationErrorCategory.REMOTE_SERVER_ERROR, "DHIS2 returned HTTP 503"
            )
        if index >= len(pages):
            return RemotePage(records=(), next_cursor=None)
        has_more = index + 1 < len(pages)
        return RemotePage(
            records=tuple(pages[index]),
            next_cursor=str(index + 2) if has_more else None,
            page_description=f"{label} page {index + 1}",
        )

    def fetch_organisation_units(self, cursor: str | None = None) -> RemotePage:
        return self._page(self._org_pages, cursor, "organisationUnits")

    def fetch_data_values(self, scope: RemoteScope, cursor: str | None = None) -> RemotePage:
        return self._page(self._value_pages, cursor, "dataValues")


def unit(remote_id: str, name: str = "Somewhere") -> RemoteOrganisationUnit:
    return RemoteOrganisationUnit(remote_id=remote_id, name=name, level=2)


def value(org_unit: str, element: str = "DE1", raw: str | None = "5") -> RemoteDataValue:
    return RemoteDataValue(
        data_element_remote_id=element,
        organisation_unit_remote_id=org_unit,
        period="202603",
        value=raw,
    )


def service(session: Session, client: Any) -> Dhis2SyncService:
    return Dhis2SyncService(session, client, crosswalk=Dhis2Crosswalk(session))


MARCH = RemoteScope(
    organisation_unit_remote_ids=(MAPPED_UID,),
    period_start=date(2026, 3, 1),
    period_end=date(2026, 3, 31),
)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------
class TestRunsAreRecorded:
    def test_a_metadata_sync_writes_a_run_with_its_counts(self, session: Session) -> None:
        client = FakeDhis2(org_unit_pages=[[unit(MAPPED_UID)], [unit(MAPPED_UID)]])
        report = service(session, client).sync_organisation_units()
        session.commit()

        run = session.execute(select(IntegrationRun)).scalars().one()
        assert run.resource is IntegrationResource.ORGANISATION_UNIT_METADATA
        assert run.run_status is IntegrationRunStatus.COMPLETED
        assert run.pages_fetched == 2
        assert run.records_received == 2
        assert run.finished_at is not None
        assert run.payload_checksum is not None
        assert report.run_id == run.id

    def test_every_run_carries_a_correlation_id(self, session: Session) -> None:
        """It is what lets a MARS operator and a DHIS2 operator look at the same
        exchange."""
        service(session, FakeDhis2(org_unit_pages=[[unit(MAPPED_UID)]])).sync_organisation_units()
        session.commit()
        run = session.execute(select(IntegrationRun)).scalars().one()
        assert run.correlation_id
        assert run.adapter_version

    def test_no_credential_reaches_the_run_row(self, session: Session) -> None:
        client = FakeDhis2(org_unit_pages=[[unit(MAPPED_UID)]])
        service(session, client).sync_organisation_units(SyncOptions(initiated_by="ops:test"))
        session.commit()

        run = session.execute(select(IntegrationRun)).scalars().one()
        rendered = " ".join(
            str(part)
            for part in (
                run.scope_description,
                run.error_summary,
                run.initiated_by,
                run.correlation_id,
            )
        )
        for secret in ("password", "ApiToken", "@", "Authorization"):
            assert secret not in rendered

    def test_a_dry_run_records_nothing(self, session: Session) -> None:
        client = FakeDhis2(org_unit_pages=[[unit(UNMAPPED_UID)]])
        report = service(session, client).sync_organisation_units(SyncOptions(dry_run=True))
        session.commit()

        assert report.records_received == 1
        assert report.mappings_unresolved == 1
        assert _count(session, IntegrationRun) == 0
        assert _count(session, IntegrationMappingProposal) == 0


class TestUnresolvedIdentifiersStayVisible:
    def test_an_unmapped_uid_becomes_a_proposal(self, session: Session) -> None:
        client = FakeDhis2(org_unit_pages=[[unit(UNMAPPED_UID, "Nearlyville")]])
        report = service(session, client).sync_organisation_units()
        session.commit()

        assert report.mappings_unresolved == 1
        proposal = session.execute(select(IntegrationMappingProposal)).scalars().one()
        assert proposal.remote_id == UNMAPPED_UID
        assert proposal.proposal_status is MappingProposalStatus.PROPOSED
        assert proposal.occurrences == 1

    def test_a_mapped_uid_resolves_and_produces_no_proposal(self, session: Session) -> None:
        client = FakeDhis2(org_unit_pages=[[unit(MAPPED_UID)]])
        report = service(session, client).sync_organisation_units()
        session.commit()

        assert report.mappings_unresolved == 0
        assert report.records_accepted == 1
        assert _count(session, IntegrationMappingProposal) == 0

    def test_the_same_gap_seen_repeatedly_is_one_proposal_with_a_count(
        self, session: Session
    ) -> None:
        """An operator reading a list of thousands stops reading."""
        for _ in range(3):
            client = FakeDhis2(org_unit_pages=[[unit(UNMAPPED_UID)]])
            service(session, client).sync_organisation_units(SyncOptions(force=True))
            session.commit()

        proposal = session.execute(select(IntegrationMappingProposal)).scalars().one()
        assert proposal.occurrences == 3
        assert proposal.last_seen_at >= proposal.first_seen_at

    def test_a_run_with_gaps_is_partial_not_completed(self, session: Session) -> None:
        """Partial is a real outcome: the exchange worked, the configuration is
        incomplete, and those need different responses."""
        client = FakeDhis2(org_unit_pages=[[unit(MAPPED_UID), unit(UNMAPPED_UID)]])
        report = service(session, client).sync_organisation_units()
        session.commit()
        assert report.status is IntegrationRunStatus.PARTIAL


class TestIdempotencyAndResume:
    def test_re_running_a_completed_scope_reports_it_rather_than_repeating(
        self, session: Session
    ) -> None:
        first = service(
            session, FakeDhis2(org_unit_pages=[[unit(MAPPED_UID)]])
        ).sync_organisation_units()
        session.commit()

        second = service(
            session, FakeDhis2(org_unit_pages=[[unit(MAPPED_UID)]])
        ).sync_organisation_units()
        session.commit()

        assert second.run_id == first.run_id
        assert _count(session, IntegrationRun) == 1

    def test_force_creates_a_new_attempt_rather_than_overwriting(self, session: Session) -> None:
        """The first exchange happened. A record that shows only the latest one
        cannot explain what an operator saw yesterday."""
        service(session, FakeDhis2(org_unit_pages=[[unit(MAPPED_UID)]])).sync_organisation_units()
        session.commit()
        service(session, FakeDhis2(org_unit_pages=[[unit(MAPPED_UID)]])).sync_organisation_units(
            SyncOptions(force=True)
        )
        session.commit()

        runs = (
            session.execute(select(IntegrationRun).order_by(IntegrationRun.attempt)).scalars().all()
        )
        assert [run.attempt for run in runs] == [1, 2]

    def test_an_identical_payload_produces_an_identical_checksum(self, session: Session) -> None:
        first = service(
            session, FakeDhis2(org_unit_pages=[[unit(MAPPED_UID)]])
        ).sync_organisation_units()
        session.commit()
        second = service(
            session, FakeDhis2(org_unit_pages=[[unit(MAPPED_UID)]])
        ).sync_organisation_units(SyncOptions(force=True))
        session.commit()

        assert first.payload_checksum == second.payload_checksum

    def test_a_changed_payload_produces_a_different_checksum(self, session: Session) -> None:
        """A changed remote payload must not silently keep the previous meaning."""
        first = service(
            session, FakeDhis2(org_unit_pages=[[unit(MAPPED_UID, "Exchangeville")]])
        ).sync_organisation_units()
        session.commit()
        second = service(
            session, FakeDhis2(org_unit_pages=[[unit(MAPPED_UID, "Exchangeville East")]])
        ).sync_organisation_units(SyncOptions(force=True))
        session.commit()

        assert first.payload_checksum != second.payload_checksum

    def test_a_failure_partway_keeps_the_pages_already_read(self, session: Session) -> None:
        """Resuming from page three is cheaper and more honest than discarding
        two good pages."""
        client = FakeDhis2(
            org_unit_pages=[[unit(MAPPED_UID)], [unit(MAPPED_UID)], [unit(MAPPED_UID)]],
            fail_on_page=3,
        )
        report = service(session, client).sync_organisation_units()
        session.commit()

        assert report.status is IntegrationRunStatus.PARTIAL
        assert report.pages_fetched == 2
        run = session.execute(select(IntegrationRun)).scalars().one()
        assert run.error_category == IntegrationErrorCategory.REMOTE_SERVER_ERROR.value
        assert run.cursor == "3"

    def test_resume_continues_from_the_recorded_cursor(self, session: Session) -> None:
        failing = FakeDhis2(
            org_unit_pages=[[unit(MAPPED_UID)], [unit(MAPPED_UID)], [unit(MAPPED_UID)]],
            fail_on_page=3,
        )
        service(session, failing).sync_organisation_units()
        session.commit()

        resuming = FakeDhis2(
            org_unit_pages=[[unit(MAPPED_UID)], [unit(MAPPED_UID)], [unit(MAPPED_UID)]]
        )
        service(session, resuming).sync_organisation_units(SyncOptions(resume=True))
        session.commit()

        assert resuming.requested_cursors[0] == "3", "resume restarted from the beginning"
        assert _count(session, IntegrationRun) == 1, "resume created a second run"


class TestAggregateValuesGoThroughTheCanonicalPath:
    def test_values_for_a_mapped_facility_are_grouped_for_ingestion(self, session: Session) -> None:
        client = FakeDhis2(data_value_pages=[[value(MAPPED_UID), value(MAPPED_UID, "DE2")]])
        report, groups = service(session, client).pull_aggregate_values(MARCH)
        session.commit()

        assert report.records_received == 2
        assert report.records_accepted == 2
        assert len(groups) == 1
        assert groups[0]["facility_id"] == str(FACILITY_ID)
        assert len(groups[0]["values"]) == 2

    def test_values_for_an_unmapped_unit_are_rejected_not_reassigned(
        self, session: Session
    ) -> None:
        """Loading them somewhere plausible is how a district acquires
        attendance it never had."""
        client = FakeDhis2(data_value_pages=[[value(MAPPED_UID), value(UNMAPPED_UID)]])
        report, groups = service(session, client).pull_aggregate_values(MARCH)
        session.commit()

        assert report.records_accepted == 1
        assert report.records_rejected == 1
        assert report.mappings_unresolved == 1
        assert all(group["facility_id"] == str(FACILITY_ID) for group in groups)

        proposal = session.execute(select(IntegrationMappingProposal)).scalars().one()
        assert proposal.remote_id == UNMAPPED_UID

    def test_a_blank_value_survives_the_exchange_as_blank(self, session: Session) -> None:
        """Blank and zero are opposite facts, and this is a seam where the
        distinction is easy to lose."""
        client = FakeDhis2(
            data_value_pages=[[value(MAPPED_UID, "DE1", ""), value(MAPPED_UID, "DE2", "0")]]
        )
        _report, groups = service(session, client).pull_aggregate_values(MARCH)
        session.commit()

        carried = {row["data_element"]: row["value"] for row in groups[0]["values"]}
        assert carried["DE1"] == ""
        assert carried["DE2"] == "0"
        assert carried["DE1"] != carried["DE2"]

    def test_the_service_writes_no_aggregate_submission_itself(self, session: Session) -> None:
        """Aggregate figures have exactly one writer: the Prompt 11 pipeline.
        A second path would drift, and the first sign would be two different
        numbers for one month."""
        client = FakeDhis2(data_value_pages=[[value(MAPPED_UID)]])
        service(session, client).pull_aggregate_values(MARCH)
        session.commit()

        submissions = session.execute(
            text("SELECT count(*) FROM mars_core.aggregate_submission")
        ).scalar_one()
        assert submissions == 0


def _count(session: Session, model: Any) -> int:
    return int(session.execute(select(func.count()).select_from(model)).scalar_one())
