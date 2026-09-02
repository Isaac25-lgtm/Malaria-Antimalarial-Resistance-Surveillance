"""Interpretation of the six-digit ``FScode`` found in the supplied subcounty file.

The code is hierarchical. Reading it left to right:

    3      region          matches the RCode attribute on every supplied row
    314    district        one code per district-level unit
    3141   county          one code per county within a district
    314101 subcounty       unique per subcounty, town council or division

**Status: source alias, not a primary key.** ``FScode`` is a national-statistics
/ shapefile code. It has not been confirmed as the Ministry of Health or UBOS
organisation-unit code, so MARS records it as an alias against a UUID primary
key. If an authoritative code arrives, ``preferred_code`` changes and the
internal identity does not.

This module contains only structural interpretation. It performs no import; the
Prompt 5 importer uses these helpers to derive the four hierarchy levels and to
write the corresponding alias rows.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from mars.domain.enums import GeographyLevel

#: Source system name used on alias rows derived from this code.
SOURCE_SYSTEM = "ubos_fscode"

#: Preferred code assigned to the single country-level unit. Not derived from
#: FScode, which has no country segment.
COUNTRY_CODE = "UG"

_FSCODE_PATTERN = re.compile(r"^\d{6}$")

#: Character length of the prefix that identifies each level.
LEVEL_PREFIX_LENGTH: dict[GeographyLevel, int] = {
    GeographyLevel.REGION: 1,
    GeographyLevel.DISTRICT: 3,
    GeographyLevel.COUNTY: 4,
    GeographyLevel.SUBCOUNTY: 6,
}

#: Levels derivable from an FScode, ordered from broadest to narrowest.
DERIVABLE_LEVELS: tuple[GeographyLevel, ...] = (
    GeographyLevel.REGION,
    GeographyLevel.DISTRICT,
    GeographyLevel.COUNTY,
    GeographyLevel.SUBCOUNTY,
)


class InvalidFsCodeError(ValueError):
    """Raised when a value does not have the expected six-digit shape."""


@dataclass(frozen=True, slots=True)
class FsCodeParts:
    """The four hierarchy prefixes contained in one FScode."""

    region: str
    district: str
    county: str
    subcounty: str

    def code_for(self, level: GeographyLevel) -> str:
        match level:
            case GeographyLevel.REGION:
                return self.region
            case GeographyLevel.DISTRICT:
                return self.district
            case GeographyLevel.COUNTY:
                return self.county
            case GeographyLevel.SUBCOUNTY:
                return self.subcounty
            case _:
                raise InvalidFsCodeError(f"{level.value} is not derivable from an FScode")

    def parent_code_for(self, level: GeographyLevel) -> str:
        """Return the code of the parent unit for ``level``.

        The region's parent is the country, which has no FScode segment.
        """
        match level:
            case GeographyLevel.REGION:
                return COUNTRY_CODE
            case GeographyLevel.DISTRICT:
                return self.region
            case GeographyLevel.COUNTY:
                return self.district
            case GeographyLevel.SUBCOUNTY:
                return self.county
            case _:
                raise InvalidFsCodeError(f"{level.value} is not derivable from an FScode")


def normalise_fscode(value: str | int) -> str:
    """Coerce a raw FScode to its canonical six-character string form.

    Integers and short strings are zero-padded, because a spreadsheet round trip
    strips a leading zero and that must not silently become a different region.
    """
    text = str(value).strip()
    if not text:
        raise InvalidFsCodeError("FScode is empty")
    if not text.isdigit():
        raise InvalidFsCodeError(f"FScode must be numeric, got {text!r}")
    text = text.zfill(6)
    if not _FSCODE_PATTERN.match(text):
        raise InvalidFsCodeError(f"FScode must be six digits, got {text!r}")
    return text


def parse_fscode(value: str | int) -> FsCodeParts:
    """Split an FScode into its region, district, county and subcounty prefixes."""
    code = normalise_fscode(value)
    return FsCodeParts(
        region=code[:1],
        district=code[:3],
        county=code[:4],
        subcounty=code,
    )


def level_of(value: str | int) -> GeographyLevel:
    """Return the level a code prefix identifies, by its length."""
    text = str(value).strip()
    for level, length in LEVEL_PREFIX_LENGTH.items():
        if len(text) == length:
            return level
    raise InvalidFsCodeError(f"no geography level corresponds to a code of length {len(text)}")


def is_consistent_with_region(value: str | int, region_code: str | int) -> bool:
    """Check that an FScode's leading digit agrees with a separately supplied region.

    In the supplied subcounty layer this held for every row. The importer
    re-checks it per feature rather than relying on that observation.
    """
    return parse_fscode(value).region == str(region_code).strip()
