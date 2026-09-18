"""Tests for the orchestration loop (app/agent.py), using a scripted fake
Anthropic client (tests/fake_anthropic.py) and the app's own mock
Snowflake backend. These pin down the loop mechanics -- tool_use blocks
get dispatched, results get fed back, the loop stops on a non-tool_use
response -- not model quality (see tests/test_intent_router.py for the
scope gate in front of all this).
"""

import pytest

from app import agent, mock_snowflake
from tests.fake_anthropic import FakeResponse, ScriptedClient, TextBlock, ToolUseBlock


def setup_function():
    mock_snowflake.RETURNS.clear()
    agent._sessions.clear()


@pytest.fixture(autouse=True)
def _assume_in_scope(monkeypatch):
    """Stub classify_intent so every turn here reaches the loop directly,
    without a scripted classification response in front of every script."""
    monkeypatch.setattr(agent, "classify_intent", lambda *a, **kw: "order_status")


def _use_scripted_client(monkeypatch, script):
    client = ScriptedClient(script)
    monkeypatch.setattr(agent, "_get_client", lambda: client)
    return client


def test_agent_asks_clarifying_question_when_info_missing(monkeypatch):
    script = [
        FakeResponse(
            content=[TextBlock("Sure -- could you share your order ID and the email on the account?")],
            stop_reason="end_turn",
        )
    ]
    client = _use_scripted_client(monkeypatch, script)

    session = agent.Session(session_id="test-session")
    result = agent.run_turn(session, "Where's my order?")

    assert "order ID" in result.reply
    assert result.suggestions is None
    assert result.tool_calls == []
    assert len(client.messages.calls) == 1  # no tool round-trip needed to ask a question


def test_agent_calls_tool_and_incorporates_result(monkeypatch):
    script = [
        FakeResponse(
            content=[
                ToolUseBlock(
                    id="t1",
                    name="get_order_status",
                    input={"order_id": "BK-1001", "email": "alice@bookly-demo.com"},
                )
            ],
            stop_reason="tool_use",
        ),
        FakeResponse(
            content=[
                TextBlock(
                    "Your order BK-1001 (The Hobbit) was delivered 5 days ago via UPS -- "
                    "happy to help with anything else."
                )
            ],
            stop_reason="end_turn",
        ),
    ]
    client = _use_scripted_client(monkeypatch, script)

    session = agent.Session(session_id="test-session")
    result = agent.run_turn(session, "My order is BK-1001, email alice@bookly-demo.com")

    assert "BK-1001" in result.reply
    assert len(client.messages.calls) == 2  # exactly one tool round-trip

    # The tool_result actually sent back to the model should reflect the
    # real (fake) Snowflake data -- not something the test scripted.
    tool_result_message = session.messages[2]
    assert tool_result_message["role"] == "user"
    tool_result_content = tool_result_message["content"][0]["content"]
    assert "The Hobbit" in tool_result_content
    assert '"return_eligible": true' in tool_result_content

    # Same call, logged for the frontend's "Dev: show tool calls" view.
    assert len(result.tool_calls) == 1
    assert result.tool_calls[0]["name"] == "get_order_status"
    assert result.tool_calls[0]["input"] == {"order_id": "BK-1001", "email": "alice@bookly-demo.com"}
    assert result.tool_calls[0]["result"]["status"] == "delivered"


def test_agent_loop_gives_up_after_max_iterations(monkeypatch):
    # A model stuck calling tools forever shouldn't hang the request -- the
    # loop should bail out with a fallback message instead of looping forever.
    loop_forever = FakeResponse(
        content=[ToolUseBlock(id="t", name="search_policies", input={"query": "shipping times"})],
        stop_reason="tool_use",
    )
    script = [loop_forever] * agent.MAX_TOOL_ITERATIONS
    client = _use_scripted_client(monkeypatch, script)

    session = agent.Session(session_id="test-session")
    result = agent.run_turn(session, "What's your shipping policy?")

    assert "human agent" in result.reply
    assert len(client.messages.calls) == agent.MAX_TOOL_ITERATIONS


def test_verification_is_captured_from_a_successful_identity_tool_call():
    session = agent.Session(session_id="s1")
    result = agent._execute_tool(
        "get_order_status", {"order_id": "BK-1001", "email": "alice@bookly-demo.com"}, session
    )
    assert "error" not in result
    assert session.verified_email == "alice@bookly-demo.com"


def test_verification_is_not_captured_on_identity_mismatch():
    session = agent.Session(session_id="s2")
    result = agent._execute_tool(
        "get_order_status", {"order_id": "BK-1001", "email": "not-alice@example.com"}, session
    )
    assert result["error"] == "identity_mismatch"
    assert session.verified_email is None
