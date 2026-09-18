"""Langfuse tracing and cost tracking, through LiteLLM's callback.

LiteLLM already knows every provider's token counts and per-model
prices, so cost tracking is a callback registration, not a pricing
table this project would have to keep current (decision 8,
docs/plans/phase-3.md). Off unless both ``LANGFUSE_PUBLIC_KEY`` and
``LANGFUSE_SECRET_KEY`` are set - when they are not, ``configure``
does nothing and ``span`` yields a no-op object, so tracing is absent,
not merely disabled.
"""

import os
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, Protocol

import litellm

from ragbridge.config import Settings


class Span(Protocol):
    def update(self, *, output: Any = None) -> object:
        """Attach the operation's result to this span, if it is a real one.

        Returns ``object``, not ``None``: the real Langfuse span's
        ``update`` returns itself for chaining, which callers here
        always ignore - declaring ``object`` accepts that return value
        (and any other) without claiming callers get nothing back.
        """
        ...


class _NullSpan:
    """A real implementation of Span that does nothing - the same idea as
    NoOpReranker (Phase 2): the "off" behaviour is a normal implementation
    of the interface, not an `if` guarding every call site.
    """

    def update(self, *, output: Any = None) -> None:
        pass


def configure(settings: Settings) -> None:
    """Register LiteLLM's Langfuse callback, if both keys are configured.

    Bridges the settings into ``os.environ`` first: LiteLLM's Langfuse
    integrations read their credentials from there directly, not from
    this project's ``Settings`` object, which matters because
    ``pydantic-settings`` reads a ``.env`` file into its own merged
    configuration without exporting those values back into
    ``os.environ`` - this project's normal configuration path would
    otherwise be invisible to them.

    Registers ``"langfuse_otel"``, not the more commonly documented
    ``"langfuse"``: verified against the installed ``langfuse`` 4.15.4
    that ``"langfuse"`` raises ``AttributeError`` on the first real LLM
    call (it reads ``langfuse.version.__version__``, removed when
    ``langfuse`` v3 rewrote itself around OpenTelemetry), while
    ``"langfuse_otel"`` - LiteLLM's OpenTelemetry-native integration -
    works end to end. See docs/adr/0005-....
    """
    if not settings.langfuse_public_key or not settings.langfuse_secret_key:
        return

    os.environ.setdefault("LANGFUSE_PUBLIC_KEY", settings.langfuse_public_key)
    os.environ.setdefault("LANGFUSE_SECRET_KEY", settings.langfuse_secret_key)
    os.environ.setdefault("LANGFUSE_HOST", settings.langfuse_host)
    litellm.success_callback = ["langfuse_otel"]
    litellm.failure_callback = ["langfuse_otel"]


def flush(settings: Settings) -> None:
    """Flush any buffered spans before the process exits.

    A no-op when Langfuse isn't configured - it never imports the
    client at all in that case, keeping tracing genuinely absent, not
    merely idle.
    """
    if not settings.langfuse_public_key or not settings.langfuse_secret_key:
        return

    from langfuse import get_client

    get_client().flush()


@contextmanager
def span(settings: Settings, name: str, **input_fields: Any) -> Iterator[Span]:
    """Open one span around a whole operation, for LiteLLM's own per-call
    spans (an embedding call, a chat completion) to nest inside.

    Retrieval is not an LLM call and would otherwise never appear in a
    trace at all - this is what gives it a place to show up. Yields a
    ``_NullSpan`` when Langfuse isn't configured.
    """
    if not settings.langfuse_public_key or not settings.langfuse_secret_key:
        yield _NullSpan()
        return

    from langfuse import get_client

    client = get_client()
    with client.start_as_current_observation(
        name=name, as_type="span", input=input_fields
    ) as real_span:
        yield real_span
