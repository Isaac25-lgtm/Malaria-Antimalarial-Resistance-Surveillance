"""Command-line entry point for metadata-only DHIS2 discovery.

    python -m mars.integrations.dhis2.discovery
    mars-dhis2-discover

This command never retrieves patient collections. It writes sanitized JSON and
Markdown reports, then stops.

Exit codes:

    0  discovery completed and reports were written
    2  usage or configuration error
    3  the remote call failed
    4  discovery is not configured
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from mars.core.logging import configure_logging, get_logger
from mars.core.settings import get_settings
from mars.integrations.dhis2.discovery.client import DiscoveryClient, DiscoveryError
from mars.integrations.dhis2.discovery.config import DiscoveryConfig, DiscoveryConfigError
from mars.integrations.dhis2.discovery.render import write_reports
from mars.integrations.dhis2.discovery.service import run_discovery

logger = get_logger(__name__)

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_FAILED = 3
EXIT_NOT_CONFIGURED = 4


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mars-dhis2-discover",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="Directory for sanitized reports. Defaults to MARS_DHIS2_DISCOVERY_OUTPUT_DIR.",
    )
    parser.add_argument(
        "--dry-run-config",
        action="store_true",
        help="Print whether discovery is configured, never contacting DHIS2.",
    )
    return parser


def run(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    settings = get_settings()
    configure_logging(settings)

    try:
        config = DiscoveryConfig.from_settings(settings)
    except DiscoveryConfigError as error:
        print(str(error), file=sys.stderr)
        return EXIT_USAGE

    if config is None:
        print(
            "DHIS2 discovery is not configured. Set MARS_DHIS2_DISCOVERY_BASE_URL "
            "and a GET-restricted token through the environment. See "
            "docs/runbooks/dhis2-discovery.md.",
            file=sys.stderr,
        )
        return EXIT_NOT_CONFIGURED

    if args.dry_run_config:
        print(f"origin            {config.origin_host}")
        print(f"credentials       {'present' if config.has_credentials else 'absent'}")
        print(f"tls verification  {config.verify_tls}")
        print(f"output directory  {config.output_dir}")
        print("patient retrieval  disabled")
        return EXIT_OK if config.has_credentials else EXIT_NOT_CONFIGURED

    if not config.has_credentials:
        print(
            "DHIS2 discovery has a base URL but no credentials. Set "
            "MARS_DHIS2_DISCOVERY_TOKEN or a username and password in the environment.",
            file=sys.stderr,
        )
        return EXIT_NOT_CONFIGURED

    output_dir = args.output_dir or config.output_dir
    try:
        with DiscoveryClient(config) as client:
            report = run_discovery(client, origin_host=config.origin_host)
    except DiscoveryError as error:
        logger.warning("dhis2_discovery_failed", category=error.category.value)
        print(str(error), file=sys.stderr)
        return EXIT_FAILED

    json_path, markdown_path = write_reports(report, output_dir)
    print(f"wrote {json_path}")
    print(f"wrote {markdown_path}")
    print("Discovery stopped before patient retrieval.")
    return EXIT_OK


def main() -> None:
    sys.exit(run())


if __name__ == "__main__":
    main()
