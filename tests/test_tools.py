"""Unit tests for the tool implementations, against the app's own mock
Snowflake backend -- no live warehouse, no credentials. Pin down the
rules that matter most: identity has to match before anything is
disclosed, and the 30-day return window is enforced in code.
"""

from app import mock_snowflake, tools


def setup_function():
    mock_snowflake.RETURNS.clear()


def test_flexible_loop_tools_excludes_the_returns_only_tools():
    # app/agent.py's flexible loop (order_status/general_policy/small_talk)
    # should never be able to reach for initiate_return or
    # generate_return_label -- those are app/returns_flow.py's alone now.
    # A regression here would silently hand the model back tools it no
    # longer has any prompt guidance for.
    flexible_names = {t["name"] for t in tools.FLEXIBLE_LOOP_TOOLS}
    assert "initiate_return" not in flexible_names
    assert "generate_return_label" not in flexible_names
    # ...but TOOLS itself still lists everything -- it's the canonical set,
    # just not all of it offered to every caller.
    all_names = {t["name"] for t in tools.TOOLS}
    assert {"initiate_return", "generate_return_label"} <= all_names


def test_get_order_status_happy_path():
    result = tools.get_order_status("BK-1001", "alice@bookly-demo.com")
    assert result["status"] == "delivered"
    assert result["return_eligible"] is True
    assert len(result["items"]) == 2
    assert result["items"][0]["book_title"] == "The Hobbit"


def test_get_order_status_wrong_email_is_rejected():
    result = tools.get_order_status("BK-1001", "someone-else@example.com")
    assert result["error"] == "identity_mismatch"
    # Must not leak the real account email in the error.
    assert "alice" not in result["message"]


def test_get_order_status_unknown_order():
    result = tools.get_order_status("BK-9999", "alice@bookly-demo.com")
    assert result["error"] == "not_found"


def test_return_window_expired_is_rejected():
    # BK-1004 (Dave's order) was delivered 45 days ago in the seed data;
    # policy window is 30 days.
    result = tools.initiate_return(
        "BK-1004", "OI-10041", "dave@bookly-demo.com", reason="changed my mind"
    )
    assert result["error"] == "not_eligible"
    assert "30-day" in result["message"]
    assert mock_snowflake.RETURNS == []  # nothing written


def test_return_within_window_succeeds_and_writes_to_snowflake():
    result = tools.initiate_return(
        "BK-1001", "OI-10011", "alice@bookly-demo.com", reason="arrived damaged"
    )
    assert result["success"] is True
    assert result["book_title"] == "The Hobbit"
    assert result["estimated_refund"] == 14.99
    assert len(mock_snowflake.RETURNS) == 1
    assert mock_snowflake.RETURNS[0]["order_id"] == "BK-1001"


def test_return_not_yet_delivered_is_rejected():
    result = tools.initiate_return(
        "BK-1002", "OI-10021", "bob@bookly-demo.com", reason="don't want it anymore"
    )
    assert result["error"] == "not_eligible"
    assert "hasn't been delivered" in result["message"]


def test_search_policies_matches_by_meaning_not_exact_keyword():
    # The query never says "return" or "30 days" -- a real Cortex Search
    # service would still surface the returns doc by meaning; the mock
    # scorer here just checks the tool's contract (a results list back).
    result = tools.search_policies("can I send this book back for a refund?")
    assert "results" in result
    assert any("30 days" in r["content"] for r in result["results"])


def test_search_policies_no_match_is_structured_not_fabricated():
    result = tools.search_policies("do you sell dinosaur fossils")
    assert result["error"] == "not_found"


def test_generate_return_label_requires_a_pending_return():
    result = tools.generate_return_label("BK-1001", "OI-10011", "alice@bookly-demo.com")
    assert result["error"] == "no_pending_return"


def test_generate_return_label_succeeds_after_initiate_return():
    tools.initiate_return("BK-1001", "OI-10011", "alice@bookly-demo.com", reason="arrived damaged")

    result = tools.generate_return_label("BK-1001", "OI-10011", "alice@bookly-demo.com")

    assert result["success"] is True
    assert result["carrier"] == "UPS"
    assert result["tracking_number"].startswith("1Z")
    assert "labels.bookly-demo.com" in result["label_url"]
    # Policy: refund happens on receipt, not at label creation -- the note
    # has to say so, not just confirm the label.
    assert "14.99" in result["refund_note"]
    assert "arrives back at our warehouse" in result["refund_note"]
    assert "not at label creation" in result["refund_note"]


def test_generate_return_label_still_enforces_identity():
    tools.initiate_return("BK-1001", "OI-10011", "alice@bookly-demo.com", reason="arrived damaged")

    result = tools.generate_return_label("BK-1001", "OI-10011", "someone-else@example.com")

    assert result["error"] == "identity_mismatch"
