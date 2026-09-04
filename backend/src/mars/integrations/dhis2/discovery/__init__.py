"""Metadata-only DHIS2 discovery.

A GET-only, HTTPS-only, allowlisted client that infers what a Ministry DHIS2
instance can offer without retrieving patient collections. Candidate mappings
in its reports are proposals, not accepted crosswalks.

Run:

    python -m mars.integrations.dhis2.discovery
    mars-dhis2-discover
"""

from __future__ import annotations

from mars.integrations.dhis2.discovery.cli import main, run

__all__ = ["main", "run"]
