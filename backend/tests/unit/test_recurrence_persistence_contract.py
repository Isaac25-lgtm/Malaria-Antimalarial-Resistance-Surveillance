from sqlalchemy import CheckConstraint, UniqueConstraint

from mars.domain.investigation import Investigation
from mars.domain.recurrence_analysis import ClinicalEvidenceDataset


def _columns(constraint: UniqueConstraint) -> tuple[str, ...]:
    return tuple(column.name for column in constraint.columns)


def test_retained_dataset_identity_includes_the_authorisation_scope() -> None:
    constraint = next(
        item
        for item in ClinicalEvidenceDataset.__table__.constraints
        if isinstance(item, UniqueConstraint)
        and item.name == "uq_clinical_evidence_dataset_identity"
    )

    assert _columns(constraint) == (
        "source_kind",
        "scope_key",
        "mapping_version",
        "dataset_hash",
        "key_version",
    )


def test_investigation_requires_exactly_one_signal_or_patient_finding() -> None:
    constraint = next(
        item
        for item in Investigation.__table__.constraints
        if isinstance(item, CheckConstraint) and item.name == "ck_investigation_exactly_one_source"
    )

    assert "signal_id IS NULL" in str(constraint.sqltext)
    assert "patient_finding_id IS NULL" in str(constraint.sqltext)
