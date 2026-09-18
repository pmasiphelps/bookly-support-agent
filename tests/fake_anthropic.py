"""A scripted stand-in for the Anthropic client, used only by tests.

It doesn't simulate model *reasoning* -- it replays a fixed sequence of
responses you hand it, so tests can assert the agent loop's mechanics
(dispatching tool_use blocks, feeding tool_results back, stopping on a
non-tool_use response) without calling a real model.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class TextBlock:
    text: str
    type: str = "text"


@dataclass
class ToolUseBlock:
    id: str
    name: str
    input: dict[str, Any]
    type: str = "tool_use"


@dataclass
class FakeResponse:
    content: list[Any]
    stop_reason: str


class _FakeMessages:
    def __init__(self, script: list[FakeResponse]):
        self._script = list(script)
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if not self._script:
            raise AssertionError("fake_anthropic: script exhausted -- agent looped more than expected")
        return self._script.pop(0)


class ScriptedClient:
    """Drop-in replacement for anthropic.Anthropic() in tests."""

    def __init__(self, script: list[FakeResponse]):
        self.messages = _FakeMessages(script)
