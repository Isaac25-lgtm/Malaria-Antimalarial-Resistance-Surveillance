#!/usr/bin/env python3
"""Export the OpenAPI document that defines the frontend contract.

The TypeScript client is generated from this file, and CI regenerates it and
fails on any difference. That is what stops a backend field rename from silently
breaking the interface at run time instead of at build time.

The document is exported from a **local, development-authentication** build, so
it includes the development sign-in routes. That is deliberate: the frontend
needs their types to render the development login screen, and those routes are
simply absent from a staging or production deployment.

    python scripts/export_openapi.py --output contracts/openapi.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = REPO_ROOT / "contracts" / "openapi.json"

sys.path.insert(0, str(REPO_ROOT / "backend" / "src"))


def build_document() -> dict[str, object]:
    from mars.core.settings import Environment, Settings
    from mars.main import create_app

    settings = Settings(
        environment=Environment.LOCAL,
        database_url="postgresql+psycopg://mars:contract@localhost:5432/mars",
        auth_mode="demo",
        dev_auth_enabled=True,
        demo_mode_enabled=True,
        dev_auth_secret="contract-export-only",
        log_format="console",
        # Pinned so the exported document does not change with the build.
        release_version="0.1.0",
        git_sha="contract-export",
    )
    app = create_app(settings)
    return app.openapi()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Compare against the tracked document and fail on any difference",
    )
    args = parser.parse_args(argv)

    document = build_document()
    rendered = json.dumps(document, indent=2, sort_keys=True) + "\n"

    if args.check:
        if not args.output.exists():
            print(f"ERROR: {args.output} does not exist. Run without --check.", file=sys.stderr)
            return 1
        current = args.output.read_text(encoding="utf-8")
        if current != rendered:
            print(
                "ERROR: the OpenAPI contract is out of date.\n"
                "  The API has changed but contracts/openapi.json was not regenerated.\n"
                "  Run: python scripts/export_openapi.py && npm --prefix frontend run generate:api",
                file=sys.stderr,
            )
            return 1
        print(f"{args.output} is up to date")
        return 0

    args.output.parent.mkdir(parents=True, exist_ok=True)
    # Keep the tracked contract byte-stable on Windows and Linux.  Without an
    # explicit newline policy TextIO translates every LF to CRLF on Windows,
    # which makes every regenerated line look like trailing whitespace to Git.
    args.output.write_text(rendered, encoding="utf-8", newline="\n")
    paths = len(document.get("paths", {}))  # type: ignore[union-attr]
    schemas = len(document.get("components", {}).get("schemas", {}))  # type: ignore[union-attr]
    print(f"wrote {args.output} ({paths} paths, {schemas} schemas)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
