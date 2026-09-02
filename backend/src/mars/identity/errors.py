"""The single public failure of an identity lookup.

Re-identification can fail four ways: the caller lacks the permission, the
caller's sensitivity ceiling is too low, no reason was stated, or the reference
resolves to nobody. Internally these are four different facts and each is
audited separately.

**Externally, three of them are one error.** If a refused caller could tell
"you may not" from "there is nobody", the service would answer the question it
exists to protect: whether a given pseudonymous reference belongs to a real
person. A caller could then walk a list of references and learn which are real
without ever being granted a single disclosure.

The missing-reason case is deliberately *not* folded in. It describes the
caller's own request - they omitted a field they control - and says nothing
about whether any patient exists, so returning a validation error there leaks
nothing and tells the caller how to fix their call.
"""

from __future__ import annotations

from mars.core.errors import MarsError


class IdentityUnavailableError(MarsError):
    """The identity is not available to this caller.

    Returned identically for a missing permission, an insufficient sensitivity
    ceiling and an unknown reference. The status is 404 rather than 403 because
    403 would itself be an answer: it would confirm the reference is real and
    that only authorisation stands in the way.

    The cost of this choice is real and worth stating: a caller who genuinely
    should have been granted access gets no hint that a permission is what they
    are missing. That is why the audit trail keeps the outcomes distinct - the
    person reviewing access can see exactly why each attempt failed, even though
    the caller cannot.
    """

    status_code = 404
    code = "identity_unavailable"
    title = "Identity unavailable"

    def __init__(self) -> None:
        # No detail parameter. A caller-supplied or context-derived message
        # would eventually differ between the branches and reintroduce the
        # distinction this class exists to remove.
        super().__init__("No identity is available for that reference.")


__all__ = ["IdentityUnavailableError"]
