"""Live-session wiring for the metadata-only discovery engine."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from mars.core.settings import Settings
from mars.integrations.dhis2.discovery.client import DiscoveryClient
from mars.integrations.dhis2.discovery.config import DiscoveryConfig
from mars.integrations.dhis2.discovery.render import write_reports
from mars.integrations.dhis2.discovery.service import run_discovery


def build_live_discovery_runner(
    settings: Settings, *, output_dir: Path
) -> Callable[[str, str], dict[str, Any]]:
    """Return the source-specific callback injected at application wiring."""

    def run(username: str, password: str) -> dict[str, Any]:
        config = DiscoveryConfig.from_url(
            settings.dhis2_login_base_url,
            username=username,
            password=password,
            timeout_seconds=settings.dhis2_discovery_timeout_seconds,
            max_retries=settings.dhis2_discovery_max_retries,
            retry_backoff_seconds=settings.dhis2_discovery_retry_backoff_seconds,
            page_size=settings.dhis2_discovery_page_size,
            max_pages=settings.dhis2_discovery_max_pages,
            max_response_bytes=settings.dhis2_discovery_max_response_bytes,
            verify_tls=settings.dhis2_login_verify_tls,
            output_dir=output_dir,
        )
        with DiscoveryClient(config) as client:
            report = run_discovery(client, origin_host=config.origin_host)
        json_path, markdown_path = write_reports(report, output_dir)
        result = report.sanitized_dict()
        result["report_files"] = {
            "json": json_path.name,
            "markdown": markdown_path.name,
        }
        return result

    return run


__all__ = ["build_live_discovery_runner"]
