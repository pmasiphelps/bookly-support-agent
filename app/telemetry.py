"""
Per-turn tracing of model calls.

Every Claude call in the app goes through create_message(), which times
it and records model + token usage into whatever Trace is active for the
current turn (a contextvar, so concurrent requests never mix). With no
active trace it is a plain pass-through -- tests and scripts that never
start one see no difference.
"""

from __future__ import annotations

import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Iterator


@dataclass
class LLMCall:
    purpose: str  # "router" | "agent" | "return_reason"
    model: str
    latency_ms: int
    input_tokens: int
    output_tokens: int
    stop_reason: str | None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


@dataclass
class Trace:
    llm_calls: list[LLMCall] = field(default_factory=list)

    @property
    def input_tokens(self) -> int:
        return sum(c.input_tokens for c in self.llm_calls)

    @property
    def output_tokens(self) -> int:
        return sum(c.output_tokens for c in self.llm_calls)


_current: ContextVar[Trace | None] = ContextVar("bookly_trace", default=None)


@contextmanager
def trace() -> Iterator[Trace]:
    t = Trace()
    token = _current.set(t)
    try:
        yield t
    finally:
        _current.reset(token)


def create_message(client: Any, purpose: str, **kwargs: Any) -> Any:
    """client.messages.create(**kwargs), recorded on the active trace."""
    active = _current.get()
    start = time.perf_counter()
    try:
        response = client.messages.create(**kwargs)
    except Exception as exc:
        if active is not None:
            active.llm_calls.append(LLMCall(
                purpose=purpose, model=kwargs.get("model", "?"),
                latency_ms=int((time.perf_counter() - start) * 1000),
                input_tokens=0, output_tokens=0, stop_reason=None, error=f"{type(exc).__name__}: {exc}",
            ))
        raise
    if active is not None:
        usage = getattr(response, "usage", None)
        active.llm_calls.append(LLMCall(
            purpose=purpose,
            model=kwargs.get("model", "?"),
            latency_ms=int((time.perf_counter() - start) * 1000),
            input_tokens=int(getattr(usage, "input_tokens", 0) or 0),
            output_tokens=int(getattr(usage, "output_tokens", 0) or 0),
            stop_reason=getattr(response, "stop_reason", None),
        ))
    return response
