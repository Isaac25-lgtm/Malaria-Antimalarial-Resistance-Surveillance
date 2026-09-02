"""Direct patient identity: the vault, and the linkage that reaches it.

This package is the only part of MARS that handles a patient's name, national
identifier or phone number. Everything else works with a pseudonymous
``patient_reference_id``.

The separation is enforced below the application as well as within it: the
database role the API and analytics run as has no privileges on
``mars_identity`` at all, so a query naming a vault table fails at parse time
rather than at a permission check. See ``docs/security/identity-vault.md``.
"""

from __future__ import annotations
