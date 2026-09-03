"""DHIS2 adapter — Prompt 12.

The only package in MARS that knows DHIS2 exists. It implements the ports in
:mod:`mars.integrations.ports`; domain and analytics code depends on those
ports and never imports anything from here (ADR 0003).

Disabled and unconfigured by default. See ``docs/architecture/dhis2.md``.
"""

from __future__ import annotations
