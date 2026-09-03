"""The episode engine against live PostgreSQL.

The single most important test in this file is the first one: with no approved
episode rule, the engine builds **nothing** and records why. Whether two
positive results forty days apart are one illness or two is a clinical
judgement that depends on the drug, the setting and the programme's guidance.
MARS has no defensible universal answer and must not invent one.

Everything after that uses an explicitly named **test-only** rule version. Its
window is a fixture value and must never become a production default; the test
that asserts the engine refuses to run without a rule is what keeps that true.

Requires ``MARS_TEST_DATABASE_URL``. Without it every test here skips.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine, func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from mars.analytics.episodes import (
    EPISODE_RULE_CODE,
    REQUIRED_PARAMETER,
    EpisodeEngine,
)
from mars.domain.encounter import (
    OpdEncounter,
    OpdEncounterPrescription,
    OpdEncounterTest,
    PatientReference,
)
from mars.domain.enums import (
    AttendanceType,
    DateAssignmentMethod,
    EpisodeBuildStatus,
    EpisodeEncounterRole,
    EpisodeStatus,
    FeverStatus,
    LifecycleStatus,
    MalariaTestMethod,
    MalariaTestResult,
    MethodKind,
    PatientCategory,
    Sex,
)
from mars.domain.episode import EpisodeBuild, EpisodeCandidate, EpisodeMember
from mars.domain.governance import MethodDefinition, MethodVersion

pytestmark = pytest.mark.integration

MIGRATIONS_ROOT = Path(__file__).resolve().parents[2]

BOUNDARY_VERSION_ID = uuid.UUID("cc100000-0000-4000-8000-0000000000ff")
COUNTRY_ID = uuid.UUID("cc100000-0000-4000-8000-000000000010")
DISTRICT_ID = uuid.UUID("cc100000-0000-4000-8000-000000000011")
ORG_UNIT_ID = uuid.UUID("cc100000-0000-4000-8000-000000000020")
FACILITY_A = uuid.UUID("cc100000-0000-4000-8000-000000000001")
FACILITY_B = uuid.UUID("cc100000-0000-4000-8000-000000000002")

PATIENT_ONE = uuid.UUID("cc100000-0000-4000-8000-0000000000a1")
PATIENT_TWO = uuid.UUID("cc100000-0000-4000-8000-0000000000a2")

PERIOD_START = date(2026, 1, 1)
PERIOD_END = date(2026, 6, 30)

#: A test-only window. **Not** a production default, and the first test in this
#: file is what stops it becoming one.
TEST_WINDOW_DAYS = 28


@pytest.fixture(scope="module")
def episode_engine_db(integration_database_url: str) -> Iterator[Engine]:
    engine = create_engine(integration_database_url, future=True)
    config = Config(str(MIGRATIONS_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(MIGRATIONS_ROOT / "migrations"))
    config.set_main_option("sqlalchemy.url", integration_database_url)
    command.upgrade(config, "head")
    yield engine
    command.downgrade(config, "base")
    engine.dispose()


@pytest.fixture(scope="module", autouse=True)
def reference_data(episode_engine_db: Engine) -> None:
    with episode_engine_db.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO mars_core.boundary_version
                    (id, code, label, source_name, storage_crs, import_status,
                     imported_at, imported_by, created_at, updated_at)
                VALUES (:id, 'TEST-EPI-0001', 'Episode fixture', 'synthetic',
                        'EPSG:4326', 'published', now(), 'test', now(), now())
                ON CONFLICT DO NOTHING
                """
            ),
            {"id": BOUNDARY_VERSION_ID},
        )
        for unit_id, level, code, name, parent, depth, path in (
            (COUNTRY_ID, "country", "UG", "Testland", None, 0, "UG"),
            (DISTRICT_ID, "district", "801", "Episodeville", COUNTRY_ID, 1, "UG/801"),
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
                VALUES (:id, 'district_health_office', 'DHO-801', 'Episodeville DHO',
                        'episodeville dho', 0, 'DHO-801', true, now(), now())
                ON CONFLICT DO NOTHING
                """
            ),
            {"id": ORG_UNIT_ID},
        )
        for facility_id, code, name in (
            (FACILITY_A, "HF-801A", "Alpha HC"),
            (FACILITY_B, "HF-801B", "Beta HC"),
        ):
            connection.execute(
                text(
                    """
                    INSERT INTO mars_core.facility
                        (id, organisation_unit_id, district_geography_unit_id, code,
                         raw_name, normalised_name, facility_level, ownership,
                         coordinate_validated, is_active, is_synthetic,
                         created_at, updated_at)
                    VALUES (:id, :org, :geo, :code, :name, :normalised, 'hc_iii',
                            'government', false, true, true, now(), now())
                    ON CONFLICT DO NOTHING
                    """
                ),
                {
                    "id": facility_id,
                    "org": ORG_UNIT_ID,
                    "geo": DISTRICT_ID,
                    "code": code,
                    "name": name,
                    "normalised": name.lower(),
                },
            )


@pytest.fixture
def session(episode_engine_db: Engine) -> Iterator[Session]:
    factory = sessionmaker(bind=episode_engine_db, expire_on_commit=False, future=True)
    with factory() as db:
        yield db
        db.rollback()


@pytest.fixture(autouse=True)
def clean(episode_engine_db: Engine) -> Iterator[None]:
    yield
    with episode_engine_db.begin() as connection:
        for table in (
            "mars_analytics.episode_member",
            "mars_analytics.episode_candidate",
            "mars_analytics.episode_build",
            "mars_core.opd_encounter_prescription",
            "mars_core.opd_encounter_test",
            "mars_core.opd_encounter",
            "mars_core.patient_reference",
            "mars_governance.method_version",
            "mars_governance.method_definition",
        ):
            connection.execute(text(f"DELETE FROM {table}"))


# ---------------------------------------------------------------------------
# Fixtures that build evidence
# ---------------------------------------------------------------------------
@pytest.fixture
def patients(session: Session) -> None:
    for reference_id in (PATIENT_ONE, PATIENT_TWO):
        session.add(PatientReference(id=reference_id, linkage_token_id=None))
    session.commit()


def approve_test_rule(session: Session, window_days: int = TEST_WINDOW_DAYS) -> MethodVersion:
    """Register and activate a **test-only** episode rule.

    Named as such in the database too, so a value that escaped into a real
    deployment would be visibly a test artefact rather than looking like
    programme guidance.
    """
    definition = MethodDefinition(
        code=EPISODE_RULE_CODE,
        label="Malaria episode grouping rule (TEST ONLY)",
        kind=MethodKind.EPISODE_RULE,
        purpose="Test fixture. Not programme-approved guidance.",
    )
    session.add(definition)
    session.flush()

    version = MethodVersion(
        method_definition_id=definition.id,
        semantic_version="0.0.1-test",
        status=LifecycleStatus.ACTIVE,
        summary="Test-only episode window. Never a production default.",
        parameters={REQUIRED_PARAMETER: window_days},
        approved_by="test:fixture",
        approved_at=datetime.now(UTC),
    )
    session.add(version)
    session.commit()
    return version


def add_encounter(
    session: Session,
    *,
    patient: uuid.UUID | None,
    day: date,
    result: MalariaTestResult = MalariaTestResult.POSITIVE,
    method: MalariaTestMethod = MalariaTestMethod.RDT,
    facility: uuid.UUID = FACILITY_A,
    treated: bool = True,
    reference: str | None = None,
) -> OpdEncounter:
    encounter = OpdEncounter(
        facility_id=facility,
        patient_reference_id=patient,
        encounter_date=day,
        date_assignment_method=DateAssignmentMethod.SOURCE_SUPPLIED,
        sex=Sex.FEMALE,
        patient_category=PatientCategory.NATIONAL,
        attendance_type=AttendanceType.NEW_ATTENDANCE,
        fever_present=FeverStatus.YES,
        residence_district_id=DISTRICT_ID,
        source_system="test",
        source_row_reference=reference or f"epi-{uuid.uuid4().hex[:12]}",
    )
    encounter.tests = [OpdEncounterTest(sequence=1, method=method, result=result)]
    if treated:
        encounter.prescriptions = [
            OpdEncounterPrescription(
                sequence=1,
                prescription_raw="Artemether/Lumefantrine 1x2x3",
                drug_name_raw="Artemether/Lumefantrine",
                drug_name_normalised="artemether/lumefantrine",
            )
        ]
    session.add(encounter)
    return encounter


def build(session: Session) -> object:
    return EpisodeEngine(session).build(PERIOD_START, PERIOD_END)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------
class TestTheEngineRefusesToInventAWindow:
    def test_with_no_approved_rule_it_builds_nothing(
        self, session: Session, patients: None
    ) -> None:
        """The most important behaviour in the module.

        Whether two positives forty days apart are one illness or two depends
        on the drug, the setting and the programme's guidance. MARS has no
        defensible universal answer, so it produces none.
        """
        add_encounter(session, patient=PATIENT_ONE, day=date(2026, 2, 1))
        add_encounter(session, patient=PATIENT_ONE, day=date(2026, 3, 1))
        session.commit()

        report = build(session)
        session.commit()

        assert report.status is EpisodeBuildStatus.NOT_CONFIGURED
        assert report.episodes_created == 0
        assert session.execute(select(func.count()).select_from(EpisodeCandidate)).scalar_one() == 0

    def test_the_refusal_is_recorded_with_what_is_missing(
        self, session: Session, patients: None
    ) -> None:
        """An operator has to be able to see that the run happened and why it
        produced nothing."""
        add_encounter(session, patient=PATIENT_ONE, day=date(2026, 2, 1))
        session.commit()
        build(session)
        session.commit()

        stored = session.execute(select(EpisodeBuild)).scalars().one()
        assert stored.build_status is EpisodeBuildStatus.NOT_CONFIGURED
        assert stored.rule_version_id is None
        assert stored.notes is not None
        assert REQUIRED_PARAMETER in stored.notes
        assert "no defensible universal answer" in stored.notes
        # The evidence it would have used is still recorded.
        assert stored.encounters_considered == 1

    def test_an_active_rule_with_no_window_is_treated_as_absent(
        self, session: Session, patients: None
    ) -> None:
        """Repairing it would mean choosing the window."""
        definition = MethodDefinition(
            code=EPISODE_RULE_CODE,
            label="Broken rule",
            kind=MethodKind.EPISODE_RULE,
            purpose="test",
        )
        session.add(definition)
        session.flush()
        session.add(
            MethodVersion(
                method_definition_id=definition.id,
                semantic_version="0.0.1-test",
                status=LifecycleStatus.ACTIVE,
                summary="no parameters",
                parameters={},
                approved_by="test:fixture",
                approved_at=datetime.now(UTC),
            )
        )
        session.commit()

        assert EpisodeEngine(session).active_rule() is None

    def test_a_draft_rule_does_not_count(self, session: Session) -> None:
        definition = MethodDefinition(
            code=EPISODE_RULE_CODE,
            label="Draft rule",
            kind=MethodKind.EPISODE_RULE,
            purpose="test",
        )
        session.add(definition)
        session.flush()
        session.add(
            MethodVersion(
                method_definition_id=definition.id,
                semantic_version="0.0.1-test",
                status=LifecycleStatus.DRAFT,
                summary="not approved",
                parameters={REQUIRED_PARAMETER: 28},
            )
        )
        session.commit()

        assert EpisodeEngine(session).active_rule() is None


class TestGroupingUnderAnApprovedRule:
    def test_visits_inside_the_window_form_one_episode(
        self, session: Session, patients: None
    ) -> None:
        approve_test_rule(session)
        add_encounter(session, patient=PATIENT_ONE, day=date(2026, 2, 1))
        add_encounter(session, patient=PATIENT_ONE, day=date(2026, 2, 15))
        session.commit()

        report = build(session)
        session.commit()

        assert report.status is EpisodeBuildStatus.COMPLETED
        assert report.episodes_created == 1
        episode = session.execute(select(EpisodeCandidate)).scalars().one()
        assert episode.encounter_count == 2
        assert episode.span_days == 14

    def test_a_gap_beyond_the_window_starts_a_new_episode(
        self, session: Session, patients: None
    ) -> None:
        approve_test_rule(session, window_days=28)
        add_encounter(session, patient=PATIENT_ONE, day=date(2026, 2, 1))
        add_encounter(session, patient=PATIENT_ONE, day=date(2026, 4, 1))
        session.commit()

        report = build(session)
        session.commit()

        assert report.episodes_created == 2
        numbers = sorted(
            e.episode_number for e in session.execute(select(EpisodeCandidate)).scalars().all()
        )
        assert numbers == [1, 2]

    def test_the_gap_is_measured_from_the_previous_visit_not_the_first(
        self, session: Session, patients: None
    ) -> None:
        """An illness with weekly follow-ups is one episode. Measuring from the
        episode's start would split it arbitrarily at the window boundary."""
        approve_test_rule(session, window_days=14)
        for day in (date(2026, 2, 1), date(2026, 2, 10), date(2026, 2, 20), date(2026, 3, 1)):
            add_encounter(session, patient=PATIENT_ONE, day=day)
        session.commit()

        report = build(session)
        session.commit()

        assert report.episodes_created == 1, "weekly follow-ups were split"
        episode = session.execute(select(EpisodeCandidate)).scalars().one()
        assert episode.encounter_count == 4

    def test_two_patients_never_share_an_episode(self, session: Session, patients: None) -> None:
        approve_test_rule(session)
        add_encounter(session, patient=PATIENT_ONE, day=date(2026, 2, 1))
        add_encounter(session, patient=PATIENT_TWO, day=date(2026, 2, 2))
        session.commit()

        report = build(session)
        session.commit()

        assert report.episodes_created == 2
        assert report.patients_considered == 2


class TestUnlinkedEncountersAreCountedNeverInvented:
    def test_an_unlinked_encounter_joins_no_episode(self, session: Session, patients: None) -> None:
        """Guessing that two similar encounters are one person is exactly the
        false merge that attaches one patient's history to another."""
        approve_test_rule(session)
        add_encounter(session, patient=None, day=date(2026, 2, 1))
        add_encounter(session, patient=None, day=date(2026, 2, 5))
        session.commit()

        report = build(session)
        session.commit()

        assert report.episodes_created == 0
        assert report.encounters_unlinked == 2

    def test_the_unlinked_count_is_recorded_on_the_build(
        self, session: Session, patients: None
    ) -> None:
        """It is the size of what MARS cannot see. A recurrence rate computed
        without it would be quietly overstated."""
        approve_test_rule(session)
        add_encounter(session, patient=PATIENT_ONE, day=date(2026, 2, 1))
        add_encounter(session, patient=None, day=date(2026, 2, 2))
        session.commit()

        build(session)
        session.commit()

        stored = session.execute(select(EpisodeBuild)).scalars().one()
        assert stored.encounters_considered == 2
        assert stored.encounters_unlinked == 1


class TestTheTimelineIsRecordedNotRecomputed:
    def test_members_carry_their_order_and_role(self, session: Session, patients: None) -> None:
        approve_test_rule(session)
        add_encounter(session, patient=PATIENT_ONE, day=date(2026, 2, 1))
        add_encounter(session, patient=PATIENT_ONE, day=date(2026, 2, 15))
        session.commit()
        build(session)
        session.commit()

        members = (
            session.execute(select(EpisodeMember).order_by(EpisodeMember.sequence)).scalars().all()
        )
        assert [m.sequence for m in members] == [1, 2]
        assert members[0].member_role is EpisodeEncounterRole.INDEX
        assert members[1].member_role is EpisodeEncounterRole.REPEAT_POSITIVE

    def test_intervals_are_stored_in_actual_days_never_banded(
        self, session: Session, patients: None
    ) -> None:
        """Bands are governed configuration. An interval stored as a band cannot
        be re-banded when the programme changes them."""
        approve_test_rule(session)
        add_encounter(session, patient=PATIENT_ONE, day=date(2026, 2, 1))
        add_encounter(session, patient=PATIENT_ONE, day=date(2026, 2, 18))
        session.commit()
        build(session)
        session.commit()

        members = (
            session.execute(select(EpisodeMember).order_by(EpisodeMember.sequence)).scalars().all()
        )
        assert members[0].days_since_previous is None
        assert members[1].days_since_previous == 17

    def test_the_index_visit_has_no_previous_interval(
        self, session: Session, patients: None
    ) -> None:
        approve_test_rule(session)
        add_encounter(session, patient=PATIENT_ONE, day=date(2026, 2, 1))
        session.commit()
        build(session)
        session.commit()

        member = session.execute(select(EpisodeMember)).scalars().one()
        assert member.days_since_previous is None
        assert member.member_role is EpisodeEncounterRole.INDEX


class TestUncertaintyTravelsWithTheEpisode:
    def test_a_repeat_positive_says_what_it_cannot_establish(
        self, session: Session, patients: None
    ) -> None:
        """The whole scientific boundary of the product, carried on the row."""
        approve_test_rule(session)
        add_encounter(session, patient=PATIENT_ONE, day=date(2026, 2, 1))
        add_encounter(session, patient=PATIENT_ONE, day=date(2026, 2, 15))
        session.commit()
        build(session)
        session.commit()

        episode = session.execute(select(EpisodeCandidate)).scalars().one()
        assert episode.has_repeat_positive
        assert episode.uncertainty is not None
        limit = episode.uncertainty["interpretation_limit"]
        assert "recrudescence from reinfection" in limit
        assert "not evidence of treatment failure or resistance" in limit

    def test_a_repeat_positive_without_treatment_records_the_ordinary_explanation(
        self, session: Session, patients: None
    ) -> None:
        """A repeat positive with no recorded treatment has a mundane cause,
        and the episode must not let a reader forget it."""
        approve_test_rule(session)
        add_encounter(session, patient=PATIENT_ONE, day=date(2026, 2, 1), treated=False)
        add_encounter(session, patient=PATIENT_ONE, day=date(2026, 2, 15), treated=False)
        session.commit()
        build(session)
        session.commit()

        episode = session.execute(select(EpisodeCandidate)).scalars().one()
        assert episode.treated_encounter_count == 0
        assert "treatment_not_recorded_for_every_positive" in (episode.uncertainty or {})

    def test_encounters_at_two_facilities_are_flagged(
        self, session: Session, patients: None
    ) -> None:
        """The interval is real; attributing the episode to one facility is not."""
        approve_test_rule(session)
        add_encounter(session, patient=PATIENT_ONE, day=date(2026, 2, 1), facility=FACILITY_A)
        add_encounter(session, patient=PATIENT_ONE, day=date(2026, 2, 15), facility=FACILITY_B)
        session.commit()
        build(session)
        session.commit()

        episode = session.execute(select(EpisodeCandidate)).scalars().one()
        assert "multiple_facilities" in (episode.uncertainty or {})

    def test_an_episode_still_open_at_the_period_end_says_so(
        self, session: Session, patients: None
    ) -> None:
        """Presenting it as finished would be an assertion the data does not
        support."""
        approve_test_rule(session, window_days=28)
        add_encounter(session, patient=PATIENT_ONE, day=PERIOD_END)
        session.commit()
        build(session)
        session.commit()

        episode = session.execute(select(EpisodeCandidate)).scalars().one()
        assert episode.episode_status is EpisodeStatus.OPEN_AT_PERIOD_END


class TestBuildsAreIdempotentAndImmutable:
    def test_rebuilding_the_same_evidence_produces_no_second_build(
        self, session: Session, patients: None
    ) -> None:
        approve_test_rule(session)
        add_encounter(session, patient=PATIENT_ONE, day=date(2026, 2, 1))
        session.commit()

        first = build(session)
        session.commit()
        second = build(session)
        session.commit()

        assert second.build_id == first.build_id
        assert session.execute(select(func.count()).select_from(EpisodeBuild)).scalar_one() == 1

    def test_a_corrected_encounter_produces_a_new_build(
        self, session: Session, patients: None
    ) -> None:
        """Episodes a clinician has already read must not silently change."""
        approve_test_rule(session)
        encounter = add_encounter(session, patient=PATIENT_ONE, day=date(2026, 2, 1))
        session.commit()
        build(session)
        session.commit()

        encounter.fever_present = FeverStatus.NO
        session.commit()
        build(session)
        session.commit()

        assert session.execute(select(func.count()).select_from(EpisodeBuild)).scalar_one() == 2

    def test_every_build_records_its_rule_and_engine_version(
        self, session: Session, patients: None
    ) -> None:
        """Reading an episode without knowing which window was in force would
        be reading a number with no units."""
        version = approve_test_rule(session)
        add_encounter(session, patient=PATIENT_ONE, day=date(2026, 2, 1))
        session.commit()
        build(session)
        session.commit()

        stored = session.execute(select(EpisodeBuild)).scalars().one()
        assert stored.rule_version_id == version.id
        assert stored.engine_version
        assert len(stored.input_fingerprint) == 64
        assert stored.source_cutoff is not None


class TestTheDatabaseRefusesContradictions:
    def test_a_span_that_disagrees_with_its_dates_is_refused(
        self, session: Session, patients: None
    ) -> None:
        stored = _bare_build(session)
        session.add(
            EpisodeCandidate(
                episode_build_id=stored.id,
                patient_reference_id=PATIENT_ONE,
                episode_number=1,
                first_encounter_date=date(2026, 2, 1),
                last_encounter_date=date(2026, 2, 15),
                span_days=99,
                encounter_count=2,
            )
        )
        with pytest.raises(IntegrityError, match="span_matches_dates"):
            session.commit()

    def test_more_positives_than_encounters_is_refused(
        self, session: Session, patients: None
    ) -> None:
        stored = _bare_build(session)
        session.add(
            EpisodeCandidate(
                episode_build_id=stored.id,
                patient_reference_id=PATIENT_ONE,
                episode_number=1,
                first_encounter_date=date(2026, 2, 1),
                last_encounter_date=date(2026, 2, 1),
                span_days=0,
                encounter_count=1,
                positive_encounter_count=3,
            )
        )
        with pytest.raises(IntegrityError, match="positives_within_encounters"):
            session.commit()


def _bare_build(session: Session) -> EpisodeBuild:
    stored = EpisodeBuild(
        rule_version_id=None,
        build_status=EpisodeBuildStatus.COMPLETED,
        period_start=PERIOD_START,
        period_end=PERIOD_END,
        input_fingerprint="e" * 64,
        source_cutoff=datetime.now(UTC),
        engine_version="test",
        started_at=datetime.now(UTC),
    )
    session.add(stored)
    session.flush()
    return stored


class TestNoIdentityIsReachable:
    def test_the_engine_module_does_not_import_the_identity_package(self) -> None:
        """Grouping is by pseudonymous reference. The vault is never queried."""
        import ast

        import mars.analytics.episodes as module

        source = Path(module.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
            elif isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)

        assert not any(name.startswith("mars.identity") for name in imported)

    def test_no_episode_column_can_hold_a_name(self, episode_engine_db: Engine) -> None:
        """Scanned against the catalogue rather than the columns we remember."""
        with episode_engine_db.connect() as connection:
            columns = (
                connection.execute(
                    text(
                        "SELECT table_name || '.' || column_name "
                        "FROM information_schema.columns "
                        "WHERE table_schema = 'mars_analytics' "
                        "AND table_name LIKE 'episode%'"
                    )
                )
                .scalars()
                .all()
            )
        assert columns
        forbidden = {"nin", "passport", "surname", "phone", "patient_name", "given_name"}
        offenders = [
            column
            for column in columns
            if forbidden & set(column.lower().replace(".", "_").split("_"))
        ]
        assert not offenders, offenders
