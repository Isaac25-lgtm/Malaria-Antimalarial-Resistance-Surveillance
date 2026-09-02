"""E-register encounter ingestion.

A versioned, MARS-owned inbound contract turned into canonical outpatient
encounters, with direct identity consumed inside the identity boundary and never
reaching ``mars_core``.

The contract is documented in ``docs/data-dictionary/ereg-inbound-contract.md``.
"""

from __future__ import annotations
