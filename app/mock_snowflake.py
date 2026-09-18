"""
In-memory mock of Snowflake -- the app's default data backend.

This is what app/tools.py talks to out of the box, via
app/data_backend.py, so the app runs with no Snowflake account or setup --
and what the test suite runs against, so there's one mock, not two copies
of one. The schema and seed data mirror snowflake/01_schema.sql,
snowflake/02_seed_data.sql, and snowflake/03_cortex_search.sql exactly.
Set BOOKLY_DATA_BACKEND=live to run against a real Snowflake account
instead -- see app/snowflake_client.py.

search_policies below is a plain keyword-overlap scorer (whole words,
simple prefix stemming), not a real semantic-search model -- good enough
to demo the tool's contract, not a stand-in for what Cortex Search does.
"""

from __future__ import annotations

import datetime as dt
import re
import threading
from typing import Any

TODAY = dt.date.today()

# Seed data -- identical in shape to snowflake/02_seed_data.sql and
# snowflake/03_cortex_search.sql, with dates computed relative to today.

CUSTOMERS = [
    {"CUSTOMER_ID": "CUST-001", "EMAIL": "alice@bookly-demo.com", "FULL_NAME": "Alice Nguyen"},
    {"CUSTOMER_ID": "CUST-002", "EMAIL": "bob@bookly-demo.com", "FULL_NAME": "Bob Martinez"},
    {"CUSTOMER_ID": "CUST-003", "EMAIL": "carol@bookly-demo.com", "FULL_NAME": "Carol Osei"},
    {"CUSTOMER_ID": "CUST-004", "EMAIL": "dave@bookly-demo.com", "FULL_NAME": "Dave Chen"},
]

ORDERS = [
    {
        "ORDER_ID": "BK-1001", "CUSTOMER_ID": "CUST-001",
        "ORDER_DATE": TODAY - dt.timedelta(days=9), "STATUS": "delivered", "TOTAL_AMOUNT": 24.98,
        "CARRIER": "UPS", "TRACKING_NUMBER": "1Z999AA10123456784",
        "ESTIMATED_DELIVERY": TODAY - dt.timedelta(days=5), "DELIVERED_AT": TODAY - dt.timedelta(days=5),
    },
    {
        "ORDER_ID": "BK-1002", "CUSTOMER_ID": "CUST-002",
        "ORDER_DATE": TODAY - dt.timedelta(days=3), "STATUS": "shipped", "TOTAL_AMOUNT": 42.97,
        "CARRIER": "USPS", "TRACKING_NUMBER": "9400111899223197428941",
        "ESTIMATED_DELIVERY": TODAY + dt.timedelta(days=2), "DELIVERED_AT": None,
    },
    {
        "ORDER_ID": "BK-1003", "CUSTOMER_ID": "CUST-003",
        "ORDER_DATE": TODAY, "STATUS": "processing", "TOTAL_AMOUNT": 15.99,
        "CARRIER": None, "TRACKING_NUMBER": None,
        "ESTIMATED_DELIVERY": TODAY + dt.timedelta(days=6), "DELIVERED_AT": None,
    },
    {
        "ORDER_ID": "BK-1004", "CUSTOMER_ID": "CUST-004",
        "ORDER_DATE": TODAY - dt.timedelta(days=50), "STATUS": "delivered", "TOTAL_AMOUNT": 18.50,
        "CARRIER": "FedEx", "TRACKING_NUMBER": "789123456123",
        "ESTIMATED_DELIVERY": TODAY - dt.timedelta(days=45), "DELIVERED_AT": TODAY - dt.timedelta(days=45),
    },
    {
        "ORDER_ID": "BK-1005", "CUSTOMER_ID": "CUST-001",
        "ORDER_DATE": TODAY - dt.timedelta(days=2), "STATUS": "out_for_delivery", "TOTAL_AMOUNT": 31.98,
        "CARRIER": "UPS", "TRACKING_NUMBER": "1Z999AA1019999991",
        "ESTIMATED_DELIVERY": TODAY, "DELIVERED_AT": None,
    },
]

ORDER_ITEMS = [
    {"ORDER_ITEM_ID": "OI-10011", "ORDER_ID": "BK-1001", "BOOK_TITLE": "The Hobbit", "ISBN": "9780547928227", "QUANTITY": 1, "UNIT_PRICE": 14.99},
    {"ORDER_ITEM_ID": "OI-10012", "ORDER_ID": "BK-1001", "BOOK_TITLE": "A Short History of Nearly Everything", "ISBN": "9780767908184", "QUANTITY": 1, "UNIT_PRICE": 9.99},
    {"ORDER_ITEM_ID": "OI-10021", "ORDER_ID": "BK-1002", "BOOK_TITLE": "Project Hail Mary", "ISBN": "9780593135204", "QUANTITY": 1, "UNIT_PRICE": 19.99},
    {"ORDER_ITEM_ID": "OI-10022", "ORDER_ID": "BK-1002", "BOOK_TITLE": "Dune", "ISBN": "9780441013593", "QUANTITY": 1, "UNIT_PRICE": 22.98},
    {"ORDER_ITEM_ID": "OI-10031", "ORDER_ID": "BK-1003", "BOOK_TITLE": "Educated: A Memoir", "ISBN": "9780399590504", "QUANTITY": 1, "UNIT_PRICE": 15.99},
    {"ORDER_ITEM_ID": "OI-10041", "ORDER_ID": "BK-1004", "BOOK_TITLE": "Sapiens: A Brief History of Humankind", "ISBN": "9780062316097", "QUANTITY": 1, "UNIT_PRICE": 18.50},
    {"ORDER_ITEM_ID": "OI-10051", "ORDER_ID": "BK-1005", "BOOK_TITLE": "The Name of the Wind", "ISBN": "9780756404079", "QUANTITY": 1, "UNIT_PRICE": 16.99},
    {"ORDER_ITEM_ID": "OI-10052", "ORDER_ID": "BK-1005", "BOOK_TITLE": "Circe", "ISBN": "9780316556347", "QUANTITY": 1, "UNIT_PRICE": 14.99},
]

# Mirrors snowflake/03_cortex_search.sql's policy_docs table exactly.
POLICY_DOCS = [
    {"topic": "shipping", "title": "Standard & expedited shipping",
     "content": "Standard shipping takes 4-6 business days within the continental US and is free on orders over $35. Expedited shipping (2-3 business days) is available at checkout for an extra $6.99."},
    {"topic": "shipping", "title": "Late or missing packages",
     "content": "If tracking shows delivered but the customer says the package never arrived, or tracking hasn't updated in 5+ business days, treat it as lost in transit: offer a free reshipment or a full refund, the customer's choice. Do not make them wait out the carrier's own claims process first."},
    {"topic": "returns", "title": "30-day return window",
     "content": "Books can be returned within 30 days of delivery for a full refund, as long as they are in resellable condition. After 30 days we cannot offer a refund, but store credit may be available case-by-case -- escalate that to a human agent."},
    {"topic": "returns", "title": "Damaged, defective, or wrong item",
     "content": "If a book arrives damaged, defective, or isn't what the customer ordered, waive the resellable-condition requirement and process the return or replacement immediately. Don't require the customer to ship the damaged item back before a refund or replacement goes out."},
    {"topic": "returns", "title": "Returning a gift",
     "content": "Items marked as a gift at checkout can be returned for Bookly store credit within 60 days of delivery, even without the original purchaser's account or order number."},
    {"topic": "password_reset", "title": "Resetting your password",
     "content": "Go to bookly.com/login, click \"Forgot password\", and enter the email on file. A reset link is emailed immediately and expires after 60 minutes."},
    {"topic": "password_reset", "title": "Reset email never arrives",
     "content": "If the customer says the reset email never arrived, have them check spam/promotions first and confirm the email matches their account exactly (case and typos included). If it still hasn't arrived after 10 minutes, send another one -- links can expire silently without bouncing."},
    {"topic": "payment", "title": "Accepted payment methods",
     "content": "We accept Visa, Mastercard, American Express, Discover, PayPal, and Bookly gift cards. We do not currently support Buy Now Pay Later options."},
    {"topic": "payment", "title": "When your card is charged",
     "content": "Cards are authorized at checkout but only actually charged once the order ships, not when it's placed. A pending authorization that never settles will drop off the customer's statement within 5-7 business days."},
    {"topic": "international", "title": "International shipping & customs",
     "content": "Bookly ships to over 40 countries; delivery takes 10-20 business days. International orders may be subject to customs fees charged by the destination country, which are the customer's responsibility and are not included in the order total."},
]

# A plain shared list, on purpose: every chat session talks to the same
# in-process store, the same way every session talks to the same
# Snowflake table in the real backend.
RETURNS: list[dict[str, Any]] = []
_lock = threading.Lock()

# Common connector words excluded from scoring so they don't create noise
# matches (e.g. "you" inside "your", "with" matching nearly everything).
_STOPWORDS = {
    "the", "and", "for", "are", "you", "your", "can", "with", "this", "that",
    "back", "from", "into", "than", "then", "have", "has", "had", "not",
    "but", "all", "any", "out", "our", "own", "off", "over", "only", "its",
    "isn", "don", "doesn", "won", "will", "just", "also", "been", "were",
    "was", "did", "does", "about", "there", "their", "them", "they", "what",
    "when", "where", "which", "who", "how", "why", "some", "more", "most",
    "much", "many", "get", "got", "one", "two",
}

# A small expansion table so a few colloquial phrasings ("it showed up
# with a torn cover") land on the right doc despite sharing no words with
# it -- real semantic search wouldn't need this.
_SYNONYMS = {
    "torn": "damaged", "ripped": "damaged", "cracked": "damaged",
    "broken": "damaged", "scratched": "damaged", "dented": "damaged",
}


def _tokenize(text: str) -> set[str]:
    words = re.findall(r"[a-z0-9]+", text.lower())
    return {w for w in words if len(w) > 3 and w not in _STOPWORDS}


def _word_matches(q_word: str, doc_word: str) -> bool:
    """Whole-word match with simple prefix stemming, so 'ship' catches
    'shipping'/'shipped' but 'sell' doesn't catch 'resellable'."""
    return q_word == doc_word or q_word.startswith(doc_word) or doc_word.startswith(q_word)


def search_policies(query: str, limit: int = 3) -> list[dict[str, Any]]:
    """Keyword-overlap scorer standing in for Cortex Search -- see the
    module docstring."""
    q_words = _tokenize(query)
    q_words |= {_SYNONYMS[w] for w in q_words if w in _SYNONYMS}

    scored = []
    for doc in POLICY_DOCS:
        haystack_words = _tokenize(f"{doc['title']} {doc['content']}")
        score = sum(1 for qw in q_words if any(_word_matches(qw, dw) for dw in haystack_words))
        if score:
            scored.append((score, doc))
    scored.sort(key=lambda pair: -pair[0])
    return [dict(doc) for _, doc in scored[:limit]]


def run_query(sql: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    params = params or {}
    sql_norm = " ".join(sql.split())

    if "FROM customers WHERE LOWER(email)" in sql_norm:
        email = params["email"].lower()
        return [c for c in CUSTOMERS if c["EMAIL"].lower() == email]

    if "FROM orders o JOIN customers c" in sql_norm:
        order = next((o for o in ORDERS if o["ORDER_ID"] == params["order_id"]), None)
        if not order:
            return []
        customer = next(c for c in CUSTOMERS if c["CUSTOMER_ID"] == order["CUSTOMER_ID"])
        return [{**order, "ACCOUNT_EMAIL": customer["EMAIL"]}]

    if "FROM order_items WHERE order_id" in sql_norm:
        return [i for i in ORDER_ITEMS if i["ORDER_ID"] == params["order_id"]]

    if "FROM orders" in sql_norm and "WHERE customer_id" in sql_norm:
        rows = [o for o in ORDERS if o["CUSTOMER_ID"] == params["customer_id"]]
        return sorted(rows, key=lambda o: o["ORDER_DATE"], reverse=True)

    if "FROM returns" in sql_norm and "WHERE order_id" in sql_norm:
        with _lock:
            matches = [
                r for r in RETURNS
                if r["order_id"] == params["order_id"] and r["order_item_id"] == params["order_item_id"]
            ]
        if not matches:
            return []
        latest = matches[-1]  # ORDER BY created_at DESC LIMIT 1
        return [{"RETURN_ID": latest["return_id"], "REQUESTED_REFUND": latest["refund"]}]

    raise AssertionError(f"mock_snowflake: unhandled query: {sql_norm}")


def run_write(sql: str, params: dict[str, Any] | None = None) -> int:
    params = params or {}
    if "INSERT INTO returns" in sql:
        with _lock:
            RETURNS.append(dict(params))
        return 1
    raise AssertionError(f"mock_snowflake: unhandled write: {sql}")
