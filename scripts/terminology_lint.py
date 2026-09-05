#!/usr/bin/env python3
"""Guard the scientific boundary in code, copy and documentation.

Blueprint section 008: the wording of the interface is part of the analytical
safety system. Routine e-register and HMIS data can identify patterns requiring
investigation; they cannot confirm antimalarial resistance.

That rule is too important to rest on every future contributor remembering it,
so it is enforced here and in CI.

What is prohibited
------------------
Language asserting confirmed resistance as an output of routine surveillance -
"confirmed resistance", "resistance detected", "resistant strain identified" and
similar.

What is permitted
-----------------
* Bounded surveillance language: "potential treatment-response signal",
  "repeat-positive pattern", "signal requiring investigation".
* Discussion *of* the boundary itself - a sentence saying MARS does **not**
  confirm resistance is exactly what the rule exists to encourage, so a negated
  or explicitly-governed occurrence passes.
* The separately governed confirmed-evidence lane (Lane B), which is fed only by
  therapeutic efficacy studies and molecular results. Files under a directory
  named ``confirmed`` are exempt, and any line may be exempted individually with
  a ``mars-lint: confirmed-evidence-lane`` marker plus a justification.

Usage
-----
    python scripts/terminology_lint.py            # scan the repository
    python scripts/terminology_lint.py --explain  # show the rules
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

#: Directories never scanned.
SKIP_DIRECTORIES = {
    ".git",
    ".venv",
    "venv",
    "node_modules",
    "dist",
    "build",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "htmlcov",
    ".idea",
    ".vscode",
}

#: File suffixes scanned.
SCANNED_SUFFIXES = {
    ".py",
    ".ts",
    ".tsx",
    ".js",
    ".jsx",
    ".json",
    ".md",
    ".html",
    ".css",
    ".yml",
    ".yaml",
    ".toml",
    ".sql",
    ".txt",
}

#: Files exempt because they define or test the rule itself.
EXEMPT_PATHS = {
    "scripts/terminology_lint.py",
    "docs/adr/0005-scientific-terminology-and-evidence-lanes.md",
    "contracts/openapi.json",
}

#: Path fragment marking the separately governed confirmed-evidence lane.
CONFIRMED_LANE_MARKER = "confirmed"

#: Inline escape hatch. Requires a justification after the marker.
LINE_EXEMPTION = re.compile(r"mars-lint:\s*confirmed-evidence-lane\s*[-:]\s*\S+")

#: Words that, near a prohibited phrase, show it is being discussed or denied
#: rather than asserted. "MARS does not confirm resistance" must pass.
NEGATION_CUES = (
    "not ",
    "never ",
    "cannot ",
    "without ",
    "no ",
    "avoid",
    "prohibit",
    "forbid",
    "must not",
    "does not",
    "do not",
    "rather than",
    "instead of",
    "reserve",
    "only lane",
    "separately governed",
    "externally confirmed",
    "prohibited",
    "refuses",
)


@dataclass(frozen=True)
class Rule:
    """A prohibited pattern and why it is prohibited."""

    pattern: re.Pattern[str]
    label: str
    guidance: str


RULES: tuple[Rule, ...] = (
    Rule(
        re.compile(r"\bconfirmed\s+(?:drug\s+|antimalarial\s+)?resistance\b", re.I),
        "confirmed resistance",
        "Routine data cannot confirm resistance. Use 'priority resistance-surveillance "
        "signal', or route the statement through the confirmed-evidence lane.",
    ),
    Rule(
        re.compile(
            r"\bresistance\s+(?:is\s+)?(?:confirmed|detected|proven|established)\b",
            re.I,
        ),
        "resistance detected or confirmed",
        "Say what was observed - a repeat-positive pattern, an unusual recurrence - "
        "not what it proves.",
    ),
    Rule(
        re.compile(
            r"\bresistan(?:t|ce)\s+(?:strain|parasite|case)s?\s+(?:found|identified)\b",
            re.I,
        ),
        "resistant organism identified",
        "Identifying a resistant organism requires molecular or therapeutic efficacy "
        "evidence, which routine data do not contain.",
    ),
    Rule(
        re.compile(
            r"\b(?:diagnos\w+|prove[ns]?|verif\w+)\s+(?:drug\s+)?resistance\b", re.I
        ),
        "resistance diagnosed or proven",
        "MARS produces signals for investigation. Diagnosis and proof are outcomes of "
        "programme investigation, not of the analytics.",
    ),
    Rule(
        re.compile(r"\btreatment\s+failure\s+(?:confirmed|detected|proven)\b", re.I),
        "treatment failure confirmed",
        "A repeat-positive encounter is not confirmed treatment failure: reinfection, "
        "adherence and incomplete records remain alternative explanations.",
    ),
)


@dataclass(frozen=True)
class Finding:
    path: Path
    line_number: int
    line: str
    rule: Rule


def is_scanned(path: Path, root: Path) -> bool:
    """Whether a file is in scope, relative to the root being scanned."""
    if path.suffix.lower() not in SCANNED_SUFFIXES:
        return False
    try:
        relative = path.relative_to(root).as_posix()
    except ValueError:
        return False
    if relative in EXEMPT_PATHS:
        return False
    return all(part not in SKIP_DIRECTORIES for part in path.parts)


def is_confirmed_lane(path: Path) -> bool:
    """Whether a file belongs to the separately governed confirmed-evidence lane."""
    return CONFIRMED_LANE_MARKER in {part.lower() for part in path.parts}


#: Cues that follow a phrase and negate it. Assertions of *absence* - a test
#: checking the phrase never appears, or a rule stating it is not permitted -
#: are exactly what the lint exists to encourage.
TRAILING_NEGATION_CUES = (
    "not in",
    "not present",
    "is prohibited",
    "are prohibited",
    "is forbidden",
    "must not",
    "never appears",
    "not allowed",
    "not permitted",
    "!==",
    "!=",
)


def is_discussing_not_asserting(line: str, match: re.Match[str]) -> bool:
    """Whether a match is denied or discussed rather than asserted.

    Looks both ways. Backwards catches a negation earlier in the sentence
    ("MARS must never state that resistance is confirmed"); forwards catches one
    that follows the phrase, which is how an assertion of absence reads
    ("'confirmed resistance' not in text").
    """
    before = line[max(0, match.start() - 90) : match.start()].lower()
    if any(cue in before for cue in NEGATION_CUES):
        return True
    after = line[match.end() : match.end() + 40].lower()
    return any(cue in after for cue in TRAILING_NEGATION_CUES)


def scan_file(path: Path) -> list[Finding]:
    try:
        text = path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return []

    findings: list[Finding] = []
    for number, line in enumerate(text.splitlines(), start=1):
        if LINE_EXEMPTION.search(line):
            continue
        for rule in RULES:
            for match in rule.pattern.finditer(line):
                if is_discussing_not_asserting(line, match):
                    continue
                findings.append(Finding(path, number, line.strip(), rule))
    return findings


def candidate_files(root: Path) -> list[Path]:
    """Every file the repository actually contains, ignored artefacts excluded.

    ``SKIP_DIRECTORIES`` is a hand-maintained list, and it drifted: seven local
    DHIS2 discovery reports under ``data/discovery/`` failed this gate with 56
    findings, all of them genuine DHIS2 option names for *rifampicin*
    resistance — tuberculosis metadata, in a file ``.gitignore`` excludes
    precisely because it is evidence of a run rather than a repository fact.

    Asking Git which files are repository content keeps the gate bound to what
    a clone would receive, so a new ignored directory cannot fail the build for
    text nobody will ever publish. The walk remains as a fallback for a source
    tree extracted without ``.git``, where scanning everything is the safe
    default.
    """
    try:
        completed = subprocess.run(
            [
                "git",
                "-C",
                str(root),
                "ls-files",
                "-z",
                "--cached",
                "--others",
                "--exclude-standard",
            ],
            capture_output=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return sorted(root.rglob("*"))
    names = completed.stdout.decode("utf-8", errors="surrogateescape").split(chr(0))
    return sorted(root / name for name in names if name)


def scan(root: Path) -> list[Finding]:
    findings: list[Finding] = []
    for path in candidate_files(root):
        if not path.is_file() or not is_scanned(path, root):
            continue
        if is_confirmed_lane(path):
            continue
        findings.extend(scan_file(path))
    return findings


def explain() -> None:
    print(__doc__)
    print("Prohibited patterns:\n")
    for rule in RULES:
        print(f"  {rule.label}")
        print(f"    pattern:  {rule.pattern.pattern}")
        print(f"    guidance: {rule.guidance}\n")
    print("Exempt paths:")
    for path in sorted(EXEMPT_PATHS):
        print(f"  {path}")
    print(
        f"\nExempt directories: any path containing a '{CONFIRMED_LANE_MARKER}' segment"
    )
    print(
        "Inline exemption: add 'mars-lint: confirmed-evidence-lane - <reason>' to the line"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="MARS terminology lint")
    parser.add_argument("--root", type=Path, default=REPO_ROOT)
    parser.add_argument(
        "--explain", action="store_true", help="Describe the rules and exit"
    )
    args = parser.parse_args(argv)

    if args.explain:
        explain()
        return 0

    findings = scan(args.root)

    if not findings:
        print("terminology lint: no prohibited resistance claims found")
        return 0

    print(
        f"terminology lint: {len(findings)} prohibited claim(s) found\n",
        file=sys.stderr,
    )
    for finding in findings:
        relative = finding.path.relative_to(args.root).as_posix()
        print(
            f"{relative}:{finding.line_number}: {finding.rule.label}", file=sys.stderr
        )
        print(f"    {finding.line[:160]}", file=sys.stderr)
        print(f"    -> {finding.rule.guidance}\n", file=sys.stderr)

    print(
        "Routine e-register and HMIS data identify patterns requiring investigation.\n"
        "They do not confirm antimalarial resistance. See "
        "docs/adr/0005-scientific-terminology-and-evidence-lanes.md",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
