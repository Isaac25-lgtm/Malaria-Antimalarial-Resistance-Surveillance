"""Name normalisation for geography and organisation units.

The supplied boundary files contain name defects that would break naive joins:
double and triple spaces (``LUBYA  TOWN COUNCIL``, ``NTWETWE  TOWN  COUNCIL``),
parenthetical aliases (``ANAKA (PAYIRA)``) and mixed unit-type suffixes.

Normalisation exists so lookups work. It never replaces the raw name: both are
stored, and the raw name is what a user sees.
"""

from __future__ import annotations

import re
import unicodedata

from mars.domain.enums import GeographyUnitKind

_WHITESPACE = re.compile(r"\s+")
_PARENTHETICAL = re.compile(r"\s*\(([^)]*)\)")
_PUNCTUATION = re.compile(r"[^A-Z0-9 '\-]")

#: Suffixes that describe the kind of local-government unit rather than its
#: name. Order matters: the longest phrase must be tested first.
_KIND_SUFFIXES: tuple[tuple[str, GeographyUnitKind], ...] = (
    ("TOWN COUNCIL", GeographyUnitKind.TOWN_COUNCIL),
    ("MUNICIPAL COUNCIL", GeographyUnitKind.MUNICIPALITY),
    ("MUNICIPALITY", GeographyUnitKind.MUNICIPALITY),
    ("CITY COUNCIL", GeographyUnitKind.CITY),
    ("DIVISION", GeographyUnitKind.URBAN_DIVISION),
    ("CITY", GeographyUnitKind.CITY),
)


def normalise_name(raw: str) -> str:
    """Return a lookup-safe form of ``raw``.

    Uppercases, strips accents, collapses internal whitespace, removes
    parenthetical aliases and drops punctuation other than apostrophes and
    hyphens, which occur inside genuine Ugandan place names.
    """
    if raw is None:
        raise ValueError("name is required")
    text = unicodedata.normalize("NFKD", raw)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.upper().strip()
    text = _PARENTHETICAL.sub("", text)
    text = _PUNCTUATION.sub(" ", text)
    text = _WHITESPACE.sub(" ", text).strip()
    return text


def extract_alias_names(raw: str) -> list[str]:
    """Return any parenthetical alternative names found in ``raw``.

    ``ANAKA (PAYIRA)`` yields ``["PAYIRA"]``. These are recorded as additional
    alias rows so that a residence field naming the alternative still resolves.
    """
    return [normalise_name(match) for match in _PARENTHETICAL.findall(raw) if match.strip()]


def infer_unit_kind(raw: str) -> GeographyUnitKind:
    """Infer the local-government form from a unit's name suffix.

    A conservative reading of the name only. Returns ``RURAL_SUBCOUNTY`` when no
    recognised suffix is present, which is the correct default at the subcounty
    level in the supplied data but is overridden by the source whenever the
    source states the kind explicitly.
    """
    normalised = normalise_name(raw)
    for suffix, kind in _KIND_SUFFIXES:
        if normalised.endswith(suffix) or f" {suffix} " in f" {normalised} ":
            return kind
    return GeographyUnitKind.RURAL_SUBCOUNTY


def name_defects(raw: str) -> list[str]:
    """Report defects observed in a supplied name.

    Recorded during import so that data-quality reporting can show what the
    source contained, rather than quietly hiding it behind the normalised form.
    """
    defects: list[str] = []
    if raw != raw.strip():
        defects.append("leading_or_trailing_whitespace")
    if "  " in raw:
        defects.append("repeated_whitespace")
    if raw != raw.upper():
        defects.append("mixed_case")
    if _PARENTHETICAL.search(raw):
        defects.append("parenthetical_alias")
    return defects
