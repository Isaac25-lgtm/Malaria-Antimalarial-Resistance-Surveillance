"""The provider boundary — Prompt 27.

MARS declares the shape of a language-model provider and ships no
implementation that calls one. That is deliberate: naming a vendor here would
embed a procurement decision nobody has made, and shipping a client that reads
an API key from the environment would make it possible to enable the assistant
by accident.

A deployment supplies a provider by registering one. Until it does,
:func:`resolve_provider` returns ``None`` and every request is answered with an
honest unavailable state.

Tests never call out. :class:`NullProvider` exists so the grounding, redaction,
citation and authorisation paths can all be exercised without a network, and
without anyone being tempted to point a test suite at a real model.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True, slots=True)
class ProviderResponse:
    """What a provider returned, and enough to audit it.

    ``text`` is prose. It is never parsed back into a number, a status or a
    decision: everything MARS treats as fact came from the records that were
    retrieved, not from what the model said about them.
    """

    text: str
    model: str
    provider: str


@runtime_checkable
class AssistantProvider(Protocol):
    """A language-model provider.

    Deliberately narrow. A provider receives an already-assembled, already
    redacted, already scope-filtered prompt and returns text. It is given no
    tools, no database handle and no ability to call back into MARS.
    """

    name: str

    def complete(self, *, system: str, prompt: str) -> ProviderResponse:
        """Return a completion for one grounded prompt."""
        ...


class NullProvider:
    """A provider that answers from the supplied context and nothing else.

    Used in tests and in any deployment that wants the retrieval, grounding and
    citation machinery without a model. It never invents a figure, because it
    has no capacity to: it restates that the context was assembled and leaves
    the interpretation to the reader.
    """

    name = "null"

    def complete(self, *, system: str, prompt: str) -> ProviderResponse:
        del system, prompt
        return ProviderResponse(
            text=(
                "No language model is configured for this deployment. The MARS "
                "records relevant to the question were retrieved and are cited "
                "below; read them directly."
            ),
            model="none",
            provider=self.name,
        )


#: Set by a deployment that has an approved provider. MARS ships nothing here.
_registered: AssistantProvider | None = None


def register_provider(provider: AssistantProvider | None) -> None:
    """Install the provider for this process.

    Called from deployment wiring, never from library code. Passing ``None``
    removes it, which is how a test restores the unconfigured state.
    """
    global _registered
    _registered = provider


def resolve_provider() -> AssistantProvider | None:
    """The registered provider, or ``None``.

    ``None`` is the shipped state and the honest one: no vendor is chosen, no
    key is read from the environment, and nothing is called.
    """
    return _registered


__all__ = [
    "AssistantProvider",
    "NullProvider",
    "ProviderResponse",
    "register_provider",
    "resolve_provider",
]
