"""Tests for ragbridge.tracing.

Covers the wiring, not a real Langfuse project: CI never calls a real
observability backend, the same way it never calls a real LLM provider
(decision 5, docs/plans/phase-1.md, extended here to "or a real tracing
backend"). litellm.success_callback and os.environ are global state, so
every test restores them - otherwise one test could leak configuration
into another.
"""

import os
from collections.abc import Iterator

import litellm
import pytest

from ragbridge import tracing
from ragbridge.config import Settings


@pytest.fixture(autouse=True)
def _restore_litellm_callbacks() -> Iterator[None]:
    """Undo any callback registration a test triggers, and any env vars
    tracing.configure() sets - both are global, process-wide state.
    """
    original_success = list(litellm.success_callback)
    original_failure = list(litellm.failure_callback)
    original_env = {
        key: os.environ.get(key)
        for key in ("LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY", "LANGFUSE_HOST")
    }
    yield
    litellm.success_callback = original_success
    litellm.failure_callback = original_failure
    for key, value in original_env.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


def test_configure_does_nothing_when_keys_are_unset() -> None:
    tracing.configure(Settings(langfuse_public_key="", langfuse_secret_key=""))

    assert litellm.success_callback == []
    assert litellm.failure_callback == []


def test_configure_registers_langfuse_otel_when_both_keys_are_set() -> None:
    os.environ.pop("LANGFUSE_PUBLIC_KEY", None)
    os.environ.pop("LANGFUSE_SECRET_KEY", None)
    os.environ.pop("LANGFUSE_HOST", None)

    tracing.configure(
        Settings(
            langfuse_public_key="pk-test",
            langfuse_secret_key="sk-test",
            langfuse_host="https://example.com",
        )
    )

    assert litellm.success_callback == ["langfuse_otel"]
    assert litellm.failure_callback == ["langfuse_otel"]
    assert os.environ["LANGFUSE_PUBLIC_KEY"] == "pk-test"
    assert os.environ["LANGFUSE_SECRET_KEY"] == "sk-test"
    assert os.environ["LANGFUSE_HOST"] == "https://example.com"


def test_span_yields_a_working_no_op_when_not_configured() -> None:
    settings = Settings(langfuse_public_key="", langfuse_secret_key="")

    with tracing.span(settings, "test-span", question="anything") as span:
        span.update(output={"answer": "anything"})  # must not raise
