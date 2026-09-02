"""The five storylines the synthetic dataset plants.

A presentation must never depend on live sensitive data, and a demo that is
merely *plausible* is worse than no demo: it teaches the audience to trust a
conclusion nobody checked. So each storyline is declared here with what the
generator plants and what MARS should - and should not - make of it.

**These are statements about the fixture, not about malaria.** The generator
plants a pattern; the manifest records that it planted it. Nothing here asserts
an epidemiological threshold, a population denominator, or a real-world rate.
When later work builds detectors, this file is the golden fixture they are
measured against, and `must_not` is the half that matters: a detector that fires
on the control district is worse than one that fires on nothing.

**Nothing here claims resistance.** Repeat positivity in routine data is a
reason to *look*, and MARS may say so. Confirmed antimalarial resistance is a
Lane B finding, established by an external reference laboratory under separate
governance, and no amount of routine data becomes it.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class StorylineKey(str, Enum):
    """Stable keys. A manifest is read by later code, so these are an API."""

    REPEAT_POSITIVE_CLUSTER = "repeat_positive_cluster"
    TESTING_ANOMALY_STOCKOUT = "testing_anomaly_stockout"
    COMPLETENESS_ARTEFACT = "completeness_artefact"
    SPATIAL_CLUSTER = "spatial_cluster"
    SEASONAL_CONTROL = "seasonal_control"


@dataclass(frozen=True, slots=True)
class Storyline:
    """One planted pattern, and what it is for."""

    key: StorylineKey
    title: str

    #: What the generator does to the data. Written so a reader can verify the
    #: claim against the generator rather than taking it on trust.
    planted: str

    #: What MARS should surface. Phrased as a signal, never as a conclusion.
    expected: tuple[str, ...]

    #: What MARS must not conclude. The more important half: a demo that only
    #: records successes cannot catch a detector that fires on everything.
    must_not: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "key": self.key.value,
            "title": self.title,
            "planted": self.planted,
            "expected": list(self.expected),
            "must_not": list(self.must_not),
        }


STORYLINES: tuple[Storyline, ...] = (
    Storyline(
        key=StorylineKey.REPEAT_POSITIVE_CLUSTER,
        title="Repeat-positive cluster at one facility",
        planted=(
            "A group of demo patients each attend two or three times within 42 "
            "days, testing positive on every visit, at one facility in this "
            "district. Each visit records a full antimalarial course, so the "
            "pattern is not explained by an absent treatment record. Every other "
            "facility in the district keeps the baseline re-attendance rate."
        ),
        expected=(
            "the facility is surfaced as a repeat-positive signal candidate",
            "the explanation shows the per-patient visit timeline and the "
            "interval between positive tests",
            "the district total alone does not explain the pattern, so the "
            "signal is attributed to the facility rather than the district",
        ),
        must_not=(
            "claim confirmed antimalarial resistance - repeat positivity in "
            "routine data is a reason to look, never a finding",
            "surface the other facilities in the same district",
            "merge two demo patients who share no identifier",
        ),
    ),
    Storyline(
        key=StorylineKey.TESTING_ANOMALY_STOCKOUT,
        title="Apparent case decline during an RDT stock-out",
        planted=(
            "Partway through the period this district's RDT testing rate falls "
            "sharply for six weeks and then recovers. Confirmed case counts fall "
            "with it. Attendance and fever reporting are unchanged, and the "
            "positivity rate among the tests that were still performed does not "
            "fall."
        ),
        expected=(
            "MARS prioritises a testing and commodity investigation rather than "
            "reporting an improvement",
            "the explanation pairs the case decline with the fall in tests "
            "performed, and shows positivity holding steady",
            "the tested denominator is used, not attendance",
        ),
        must_not=(
            "report the decline as a reduction in malaria burden",
            "raise a positivity signal from the smaller denominator alone",
        ),
    ),
    Storyline(
        key=StorylineKey.COMPLETENESS_ARTEFACT,
        title="Apparent spike explained by improved reporting completeness",
        planted=(
            "Two of this district's facilities report nothing for the first half "
            "of the period and then begin reporting. Per-facility rates are flat "
            "throughout; only the district total rises, and it rises because "
            "more facilities are reporting."
        ),
        expected=(
            "the district rise is attributed to reporting completeness, with the "
            "reporting-facility count shown beside the total",
            "a like-for-like comparison over the continuously reporting facilities shows no rise",
        ),
        must_not=(
            "report the district-level increase as an epidemiological rise",
            "treat the newly reporting facilities' first month as a spike",
        ),
    ),
    Storyline(
        key=StorylineKey.SPATIAL_CLUSTER,
        title="Village-level clustering inside a stable district",
        planted=(
            "Repeat-positive demo patients concentrate in two adjacent "
            "subcounties while the district's own total stays flat. The "
            "residence fields are populated at subcounty level; no household "
            "location exists anywhere in the dataset."
        ),
        expected=(
            "the cluster is surfaced at the subcounty level using administrative aggregation",
            "a minimum-count rule suppresses any area too small to report",
            "the district total is shown as stable, so the cluster is not "
            "mistaken for a district-wide rise",
        ),
        must_not=(
            "plot household points - none exist, and none may be inferred",
            "report an area below the minimum count",
        ),
    ),
    Storyline(
        key=StorylineKey.SEASONAL_CONTROL,
        title="Clean district: a seasonal rise that is not a signal",
        planted=(
            "Case counts rise gradually across the period in the shape of a "
            "transmission season. Testing rate, positivity and reporting "
            "completeness are all stable. No recurrence, no stock-out, no "
            "reporting change."
        ),
        expected=(
            "no signal is raised for this district",
            "the district remains visible and navigable in the demo, so the "
            "journey can be walked through a district where nothing is wrong",
        ),
        must_not=(
            "alert merely because counts are high",
            "raise any signal at all - this district is the control, and a "
            "detector that fires here fires on everything",
        ),
    ),
)

STORYLINES_BY_KEY: dict[StorylineKey, Storyline] = {s.key: s for s in STORYLINES}


__all__ = ["STORYLINES", "STORYLINES_BY_KEY", "Storyline", "StorylineKey"]
