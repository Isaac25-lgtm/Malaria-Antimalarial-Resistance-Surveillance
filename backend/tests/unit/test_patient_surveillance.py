from __future__ import annotations

import uuid

from mars.services.patient_surveillance import patient_alias


def test_patient_alias_is_stable_hmac_and_not_the_reference() -> None:
    patient = uuid.UUID("6f8721e1-f171-443c-8050-dd90d623c66b")
    first = patient_alias(patient, key=b"test-key-one", key_version="v3")
    second = patient_alias(patient, key=b"test-key-one", key_version="v3")
    assert first == second
    assert first.startswith("MARS-PT-V3-")
    assert str(patient) not in first
    assert len(first.rsplit("-", 1)[-1]) >= 20


def test_patient_alias_changes_when_display_key_rotates() -> None:
    patient = uuid.UUID("6f8721e1-f171-443c-8050-dd90d623c66b")
    before = patient_alias(patient, key=b"test-key-one", key_version="v1")
    after = patient_alias(patient, key=b"test-key-two", key_version="v2")
    assert before != after
