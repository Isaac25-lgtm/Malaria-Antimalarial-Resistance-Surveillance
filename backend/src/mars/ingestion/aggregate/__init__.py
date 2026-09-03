"""HMIS 033b and 105 aggregate ingestion — Prompt 11.

A MARS-owned versioned inbound contract for weekly and monthly submissions,
turned into the aggregate model in ``mars.domain.aggregate`` and reconciled
against the encounters MARS already holds.

The forms are transcribed in ``mars.domain.hmis_elements``; the contract is
documented in ``docs/data-dictionary/hmis-aggregate.md``.
"""

from __future__ import annotations
