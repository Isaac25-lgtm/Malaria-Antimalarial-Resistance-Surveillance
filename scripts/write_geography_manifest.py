#!/usr/bin/env python3
"""Write the tracked checksum manifest for the supplied boundary files.

The four boundary files total 226 MB and are excluded from Git. The manifest is
tracked in their place, so provenance survives a clone even though the payload
does not: any later run of ``geography_audit.py`` can prove the files on disk
are byte-for-byte the ones the project was built against.

Run this only when the source files legitimately change - which, for supplied
reference data, should be never without a corresponding boundary_version record.

    python scripts/write_geography_manifest.py
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = REPO_ROOT / "data" / "manifests" / "geography.sha256.json"

FILES: dict[str, str] = {
    "COUNTRY_BOUNDARY.json": "National outline and import control layer",
    "UGANDA_DISTRICT.json": "Primary district geometry (standard GeoJSON)",
    "UGANDA_DISTRICTS.json": "Esri JSON provenance and equivalence witness",
    "UGANDA_SUBCOUNTIES.json": "Hierarchy spine and subcounty geometry",
}


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=REPO_ROOT)
    parser.add_argument("--output", type=Path, default=MANIFEST_PATH)
    args = parser.parse_args(argv)

    entries = []
    for filename, role in FILES.items():
        path = args.data_dir / filename
        if not path.exists():
            print(f"ERROR: {path} not found", file=sys.stderr)
            return 1
        stat = path.stat()
        entries.append(
            {
                "filename": filename,
                "role": role,
                "sha256": sha256_of(path),
                "size_bytes": stat.st_size,
                "modified_utc": datetime.fromtimestamp(stat.st_mtime, tz=UTC)
                .isoformat()
                .replace("+00:00", "Z"),
            }
        )

    manifest = {
        "schema": "mars.geography.manifest/1",
        "generated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "note": (
            "Supplied reference data, excluded from Git by size. These checksums "
            "are the tracked record of exactly which bytes MARS was built "
            "against. scripts/geography_audit.py --verify-only checks them."
        ),
        "files": entries,
    }

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {args.output} ({len(entries)} files)")
    for entry in entries:
        print(f"  {entry['sha256']}  {entry['filename']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
