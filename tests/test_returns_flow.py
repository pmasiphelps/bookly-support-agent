"""Tests for the deterministic returns/refunds workflow (app/returns_flow.py).

These go through app/agent.py's run_turn with classify_intent stubbed to
"returns_refunds" -- the real entry point production traffic uses. Almost
every step is a regex extraction, a fixed question, or a real call into
app/tools.py against the mock Snowflake backend. The one exception is
_summarize_reason (Step 4), which calls a model; _stub_reason_summarization
below stubs it to a fixed string except for the handful of tests further
down that exercise it directly.
"""

import pytest

from app import agent, mock_snowflake, returns_flow
from app.returns_flow import ReturnFlow, looks_like_a_continuation
from tests.fake_anthropic import FakeResponse, ScriptedClient, ToolUseBlock

# Captured before any fixture below can monkeypatch it, so the tests that
# want the real implementation (rather than the fixed-string stub) can
# restore it for just that one test.
_real_summarize_reason = returns_flow._summarize_reason


def setup_function():
    mock_snowflake.RETURNS.clear()
    agent._sessions.clear()


@pytest.fixture(autouse=True)
def _assume_returns_refunds(monkeypatch):
    monkeypatch.setattr(agent, "classify_intent", lambda *a, **kw: "returns_refunds")


@pytest.fixture(autouse=True)
def _stub_reason_summarization(monkeypatch):
    """Stubs the one model call in this module so every other test can
    reach a filed return without scripting a fake response for a step
    it isn't testing. See test_summarize_reason_* below for the real thing.
    """
    monkeypatch.setattr(returns_flow, "_summarize_reason", lambda session: "Customer-requested return (via chat).")


def test_asks_for_order_id_and_email_when_both_are_missing():
    session = agent.Session(session_id="s1")

    result = agent.run_turn(session, "I want to return something")

    assert "order ID" in result.reply
    assert "email" in result.reply
    assert session.return_flow is not None
    assert session.return_flow.order_id is None
    assert session.return_flow.email is None


def test_one_message_with_order_id_email_and_item_files_the_return_in_one_turn():
    session = agent.Session(session_id="s1")

    result = agent.run_turn(
        session, "I'd like to return The Hobbit from order BK-1001, my email is alice@bookly-demo.com"
    )

    assert "The Hobbit" in result.reply
    assert "shipping label" in result.reply
    assert len(mock_snowflake.RETURNS) == 1
    assert mock_snowflake.RETURNS[0]["order_id"] == "BK-1001"
    assert mock_snowflake.RETURNS[0]["order_item_id"] == "OI-10011"
    # Not reset yet -- the label question is still open.
    assert session.return_flow is not None
    assert session.return_flow.filed_return_id is not None


def test_multi_item_order_gets_asked_which_book_then_resolves_by_ordinal():
    session = agent.Session(session_id="s1")

    first = agent.run_turn(session, "I want to return something from order BK-1002, my email is bob@bookly-demo.com")
    assert "Project Hail Mary" in first.reply and "Dune" in first.reply

    second = agent.run_turn(session, "the second one")

    # BK-1002 is still in transit (not delivered), so the item resolves
    # correctly (Dune) but eligibility fails -- this is checking that the
    # ordinal match worked, via the guardrail message it leads to, not that
    # the return succeeded.
    assert "hasn't been delivered yet" in second.reply
    assert mock_snowflake.RETURNS == []
    assert session.return_flow is None  # guardrail resets the flow


def test_single_item_order_never_asks_which_book():
    session = agent.Session(session_id="s1")

    result = agent.run_turn(session, "I'd like to return something from order BK-1004, my email is dave@bookly-demo.com")

    # BK-1004 has exactly one item -- should go straight to the eligibility
    # check, not a clarifying question, and fail on the 30-day window.
    assert "Which one" not in result.reply
    assert "30-day" in result.reply
    assert mock_snowflake.RETURNS == []
    assert session.return_flow is None


def test_return_window_expired_gives_the_guardrail_not_a_retry():
    session = agent.Session(session_id="s1")

    result = agent.run_turn(session, "I'd like to return something from order BK-1004, my email is dave@bookly-demo.com")

    assert "human agent" in result.reply
    assert session.return_flow is None
    assert mock_snowflake.RETURNS == []


def test_identity_mismatch_is_vague_and_resets_both_fields():
    session = agent.Session(session_id="s1")

    result = agent.run_turn(session, "order BK-1001, notalice@example.com")

    assert "alice" not in result.reply.lower()
    assert session.return_flow is not None
    assert session.return_flow.order_id is None
    assert session.return_flow.email is None


def test_unknown_order_id_only_resets_the_order_id_not_the_email():
    session = agent.Session(session_id="s1")

    result = agent.run_turn(session, "order BK-9999, alice@bookly-demo.com")

    assert "No order found" in result.reply
    assert session.return_flow is not None
    assert session.return_flow.order_id is None
    assert session.return_flow.email == "alice@bookly-demo.com"


def test_accepting_the_label_offer_generates_one_and_resets_the_flow():
    session = agent.Session(session_id="s1")
    agent.run_turn(session, "I'd like to return The Hobbit from order BK-1001, my email is alice@bookly-demo.com")

    result = agent.run_turn(session, "How do I send it back?")

    assert "tracking" in result.reply.lower()
    assert "issued once the item arrives" in result.reply
    assert session.return_flow is None


def test_declining_the_label_offer_ends_the_flow_without_generating_one():
    session = agent.Session(session_id="s1")
    agent.run_turn(session, "I'd like to return The Hobbit from order BK-1001, my email is alice@bookly-demo.com")

    result = agent.run_turn(session, "no thanks")

    assert result.reply == "No problem -- anything else I can help with?"
    assert session.return_flow is None


def test_tool_calls_are_logged_for_the_dev_debug_view():
    session = agent.Session(session_id="s1")

    result = agent.run_turn(
        session, "I'd like to return The Hobbit from order BK-1001, my email is alice@bookly-demo.com"
    )

    names = [call["name"] for call in result.tool_calls]
    assert names == ["get_order_status", "initiate_return"]
    assert result.tool_calls[0]["input"] == {"order_id": "BK-1001", "email": "alice@bookly-demo.com"}
    assert result.tool_calls[1]["result"]["success"] is True


def test_messages_are_recorded_as_a_normal_user_then_assistant_turn():
    session = agent.Session(session_id="s1")

    agent.run_turn(session, "I want to return something")

    assert session.messages[0] == {"role": "user", "content": "I want to return something"}
    assert session.messages[1]["role"] == "assistant"
    assert "order ID" in session.messages[1]["content"]


def test_a_second_return_in_the_same_session_starts_a_clean_flow():
    session = agent.Session(session_id="s1")
    agent.run_turn(session, "I'd like to return The Hobbit from order BK-1001, my email is alice@bookly-demo.com")
    agent.run_turn(session, "no thanks")  # finishes and resets the first return

    # A second, unrelated return in the same session shouldn't carry over
    # the first one's order_id/email/item.
    result = agent.run_turn(session, "I want to return something")

    assert "order ID" in result.reply
    assert session.return_flow.order_id is None


# --- looks_like_a_continuation -----------------------------------------
#
# A reply that plainly supplies a still-missing order ID or email should
# reach advance_return_flow even if classify_intent would call it
# something else. These test the helper directly, then the bypass in run_turn.


def test_looks_like_a_continuation_true_when_the_message_supplies_the_missing_order_id():
    session = agent.Session(session_id="s1")
    session.return_flow = ReturnFlow(email="alice@bookly-demo.com")  # only order_id missing

    assert looks_like_a_continuation(session, "it's order BK-1001") is True


def test_looks_like_a_continuation_true_when_the_message_supplies_the_missing_email():
    session = agent.Session(session_id="s1")
    session.return_flow = ReturnFlow(order_id="BK-1001")  # only email missing

    assert looks_like_a_continuation(session, "alice@bookly-demo.com") is True


def test_looks_like_a_continuation_false_when_nothing_is_actually_missing():
    session = agent.Session(session_id="s1")
    session.return_flow = ReturnFlow(order_id="BK-1001", email="alice@bookly-demo.com")

    # Both already known -- a coincidental order-id-shaped string here
    # isn't "supplying a missing piece," so this shouldn't fire.
    assert looks_like_a_continuation(session, "BK-9999") is False


def test_looks_like_a_continuation_false_when_no_return_is_in_progress():
    session = agent.Session(session_id="s1")  # return_flow is None

    assert looks_like_a_continuation(session, "BK-1001, alice@bookly-demo.com") is False


def test_looks_like_a_continuation_false_when_the_message_has_neither():
    session = agent.Session(session_id="s1")
    session.return_flow = ReturnFlow()

    assert looks_like_a_continuation(session, "hello there") is False


def test_run_turn_bypasses_classification_when_a_message_supplies_a_missing_order_id_and_email(monkeypatch):
    session = agent.Session(session_id="s1")
    session.return_flow = ReturnFlow()  # a return's already in progress, nothing given yet

    def _must_not_be_called(*args, **kwargs):
        raise AssertionError("classify_intent should have been bypassed, not called")

    monkeypatch.setattr(agent, "classify_intent", _must_not_be_called)

    result = agent.run_turn(
        session, "I'd like to return The Hobbit from order BK-1001, my email is alice@bookly-demo.com"
    )

    assert "shipping label" in result.reply
    assert len(mock_snowflake.RETURNS) == 1


def test_run_turn_still_classifies_normally_once_the_return_has_both_fields(monkeypatch):
    session = agent.Session(session_id="s1")
    session.return_flow = ReturnFlow(order_id="BK-1001", email="alice@bookly-demo.com")

    calls = []

    def _record(client, prior_messages, user_text):
        calls.append(user_text)
        return "returns_refunds"

    monkeypatch.setattr(agent, "classify_intent", _record)

    agent.run_turn(session, "the second one")

    # Nothing left for looks_like_a_continuation to match on (both fields
    # already set), so this later step still goes through classify_intent
    # like any ordinary returns_refunds turn.
    assert calls == ["the second one"]


# --- _summarize_reason ---------------------------------------------------
#
# advance_return_flow's Step 4 is the one place this module calls a model.
# These test it directly against a ScriptedClient, plus one integration
# test proving a real summary lands on the filed RETURNS row.


def test_summarize_reason_returns_the_forced_tool_calls_argument(monkeypatch):
    # The autouse fixture above stubs _summarize_reason itself -- undo that
    # for this one test, which is testing the real implementation.
    monkeypatch.setattr(returns_flow, "_summarize_reason", _real_summarize_reason)
    client = ScriptedClient(
        [
            FakeResponse(
                content=[
                    ToolUseBlock(
                        id="r1",
                        name="summarize_return_reason",
                        input={"reason": "The book arrived with a torn cover."},
                    )
                ],
                stop_reason="tool_use",
            )
        ]
    )
    monkeypatch.setattr(returns_flow, "_get_client", lambda: client)
    session = agent.Session(session_id="s1")
    session.messages = [
        {"role": "user", "content": "It arrived with a torn cover, I'd like to return it"}
    ]

    reason = returns_flow._summarize_reason(session)

    assert reason == "The book arrived with a torn cover."


def test_summarize_reason_falls_back_when_the_response_has_no_matching_tool_use(monkeypatch):
    # Same as above: use the real implementation, not the autouse stub.
    monkeypatch.setattr(returns_flow, "_summarize_reason", _real_summarize_reason)
    # Forced tool_choice means this shouldn't normally happen -- same
    # "malformed response" situation app/intent_router.py's classify_intent
    # guards against -- but a non-critical field falls back plainly instead
    # of raising.
    client = ScriptedClient([FakeResponse(content=[], stop_reason="end_turn")])
    monkeypatch.setattr(returns_flow, "_get_client", lambda: client)
    session = agent.Session(session_id="s1")

    reason = returns_flow._summarize_reason(session)

    assert reason == "No reason given."


def test_advance_return_flow_falls_back_when_summarize_reason_raises(monkeypatch):
    # A network error or similar shouldn't block a real, eligible return --
    # see advance_return_flow's Step 4 try/except.
    def _boom(session):
        raise RuntimeError("network blip")

    monkeypatch.setattr(returns_flow, "_summarize_reason", _boom)

    session = agent.Session(session_id="s1")
    result = agent.run_turn(
        session, "I'd like to return The Hobbit from order BK-1001, my email is alice@bookly-demo.com"
    )

    assert "shipping label" in result.reply
    assert mock_snowflake.RETURNS[0]["reason"] == "Customer-requested return (via chat) -- reason unavailable."


def test_a_filed_return_stores_the_model_generated_reason(monkeypatch):
    # Restore the real _summarize_reason for this one test -- every other
    # test in this file uses the fixed-string autouse stub instead.
    monkeypatch.setattr(returns_flow, "_summarize_reason", _real_summarize_reason)
    client = ScriptedClient(
        [
            FakeResponse(
                content=[
                    ToolUseBlock(id="r1", name="summarize_return_reason", input={"reason": "Ordered by mistake."})
                ],
                stop_reason="tool_use",
            )
        ]
    )
    monkeypatch.setattr(returns_flow, "_get_client", lambda: client)

    session = agent.Session(session_id="s1")
    result = agent.run_turn(
        session,
        "I ordered the wrong book by mistake -- please return The Hobbit from order BK-1001, "
        "my email is alice@bookly-demo.com",
    )

    assert "shipping label" in result.reply
    assert mock_snowflake.RETURNS[0]["reason"] == "Ordered by mistake."
    assert len(client.messages.calls) == 1  # exactly one model call for the whole turn
