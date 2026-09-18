"""
Tool definitions + implementations for the Bookly support agent.

Every tool acts on the schema in snowflake/01_schema.sql, through
app/data_backend.py -- an in-memory mock by default (app/mock_snowflake.py),
or a real Snowflake account if BOOKLY_DATA_BACKEND=live is set
(app/snowflake_client.py). This file doesn't know which backend it's
talking to, and there's no framework doing tool dispatch for it either --
app/agent.py drives the loop directly.

Tools never raise on expected failure paths (not found, identity
mismatch, not eligible) -- they return a structured {"error": ...} payload
and let the model phrase that to the customer, so "never fabricate
account data" stays enforceable in one place: the model only ever states
facts that came back from a tool result.
"""

from __future__ import annotations

import datetime as dt
import random
import string
from typing import Any

from . import data_backend as sf

RETURN_WINDOW_DAYS = 30

# Tools app/returns_flow.py's state machine calls directly, bypassing the
# model once a message is classified returns_refunds. Excluded from
# FLEXIBLE_LOOP_TOOLS below so the flexible loop's model never has the
# option to file a return or generate a label on its own.
_RETURNS_FLOW_ONLY_TOOLS = {"initiate_return", "generate_return_label"}


# ---------------------------------------------------------------------------
# Tool schemas (Anthropic "tools" format)
# ---------------------------------------------------------------------------

TOOLS: list[dict[str, Any]] = [
    {
        "name": "get_order_status",
        "description": (
            "Look up one order's status, carrier tracking, and line items. "
            "Requires the order ID AND the email on the account; the email must "
            "match the order's customer before any details are returned. This is "
            "the identity check for order-specific questions -- call it before "
            "telling a customer anything about a specific order, and never state "
            "order details you haven't just received from this tool."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "order_id": {"type": "string", "description": "e.g. BK-1001"},
                "email": {"type": "string", "description": "Email on the Bookly account"},
            },
            "required": ["order_id", "email"],
        },
    },
    {
        "name": "list_orders_for_customer",
        "description": (
            "List a customer's recent orders (order ID, date, status only) by "
            "email, for when they don't have an order ID on hand. Use this to "
            "help them find the right order, then call get_order_status for detail."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"email": {"type": "string"}},
            "required": ["email"],
        },
    },
    {
        "name": "initiate_return",
        "description": (
            "File a return for one specific line item on one specific order. "
            "Only call this once the customer has confirmed exactly which order "
            "AND which item (if the order has more than one), and only after "
            "get_order_status has verified the email against that order in this "
            "conversation. Server-side enforces the 30-day return window -- if "
            "the item isn't eligible you'll get back a structured reason, not an error."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "order_id": {"type": "string"},
                "order_item_id": {"type": "string", "description": "From get_order_status's items list"},
                "email": {"type": "string"},
                "reason": {"type": "string", "description": "Customer's stated reason for the return"},
            },
            "required": ["order_id", "order_item_id", "email", "reason"],
        },
    },
    {
        "name": "generate_return_label",
        "description": (
            "Generate a prepaid return shipping label for an item that "
            "already has a return on file (call initiate_return first -- "
            "this errors if there's no pending return for the item). Hits "
            "a mocked shipping-carrier system (a real build would call "
            "something like EasyPost/Shippo/UPS), separate from Snowflake, "
            "to produce a tracking number and label. Bookly's policy is to "
            "refund after the item is received back at the warehouse, not "
            "at label creation -- always relay the refund_note in the "
            "result so the customer knows when to expect their money, and "
            "don't imply the refund happens immediately."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "order_id": {"type": "string"},
                "order_item_id": {"type": "string", "description": "From get_order_status's items list"},
                "email": {"type": "string"},
            },
            "required": ["order_id", "order_item_id", "email"],
        },
    },
    {
        "name": "search_policies",
        "description": (
            "Semantic search over Bookly's policy knowledge base (shipping, returns, "
            "payment methods, international shipping) via a Snowflake Cortex Search "
            "service. Pass the customer's own question or phrasing as the query -- "
            "it doesn't need to match any fixed keyword, the service retrieves the "
            "most relevant excerpts by meaning. Always use this for general/policy "
            "questions instead of answering from memory, and only state what the "
            "returned excerpts actually say."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "The customer's question, in their own words (e.g. 'is there a fee to ship to Canada?')",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max excerpts to retrieve (default 3, max 5)",
                },
            },
            "required": ["query"],
        },
    },
]

# What app/agent.py's flexible loop actually offers the model. TOOLS
# itself stays the full, canonical list -- it's what returns_flow.py's
# TOOL_IMPLEMENTATIONS lookup and this file's own tests key off of.
FLEXIBLE_LOOP_TOOLS = [t for t in TOOLS if t["name"] not in _RETURNS_FLOW_ONLY_TOOLS]


# ---------------------------------------------------------------------------
# Implementations
# ---------------------------------------------------------------------------


def _find_customer_by_email(email: str) -> dict[str, Any] | None:
    rows = sf.run_query(
        "SELECT customer_id, email, full_name FROM customers WHERE LOWER(email) = LOWER(%(email)s)",
        {"email": email},
    )
    return rows[0] if rows else None


def get_order_status(order_id: str, email: str) -> dict[str, Any]:
    rows = sf.run_query(
        """
        SELECT o.order_id, o.customer_id, o.order_date, o.status, o.total_amount,
               o.carrier, o.tracking_number, o.estimated_delivery, o.delivered_at,
               c.email AS account_email
        FROM orders o
        JOIN customers c ON c.customer_id = o.customer_id
        WHERE o.order_id = %(order_id)s
        """,
        {"order_id": order_id},
    )
    if not rows:
        return {"error": "not_found", "message": f"No order found with ID {order_id}."}

    order = rows[0]
    if order["ACCOUNT_EMAIL"].lower() != email.strip().lower():
        # Deliberately vague: don't confirm/deny whether the order ID
        # itself is valid to an unverified caller.
        return {
            "error": "identity_mismatch",
            "message": "That email doesn't match our records for this order.",
        }

    items = sf.run_query(
        "SELECT order_item_id, book_title, isbn, quantity, unit_price FROM order_items WHERE order_id = %(order_id)s",
        {"order_id": order_id},
    )

    return_eligible, return_eligible_until = _return_eligibility(order["STATUS"], order["DELIVERED_AT"])

    return {
        "order_id": order["ORDER_ID"],
        "status": order["STATUS"],
        "order_date": str(order["ORDER_DATE"]),
        "total_amount": float(order["TOTAL_AMOUNT"]),
        "carrier": order["CARRIER"],
        "tracking_number": order["TRACKING_NUMBER"],
        "estimated_delivery": str(order["ESTIMATED_DELIVERY"]) if order["ESTIMATED_DELIVERY"] else None,
        "delivered_at": str(order["DELIVERED_AT"]) if order["DELIVERED_AT"] else None,
        "return_eligible": return_eligible,
        "return_eligible_until": str(return_eligible_until) if return_eligible_until else None,
        "items": [
            {
                "order_item_id": it["ORDER_ITEM_ID"],
                "book_title": it["BOOK_TITLE"],
                "isbn": it["ISBN"],
                "quantity": it["QUANTITY"],
                "unit_price": float(it["UNIT_PRICE"]),
            }
            for it in items
        ],
    }


def list_orders_for_customer(email: str) -> dict[str, Any]:
    customer = _find_customer_by_email(email)
    if not customer:
        return {"error": "not_found", "message": "No Bookly account found with that email."}

    orders = sf.run_query(
        """
        SELECT order_id, order_date, status
        FROM orders
        WHERE customer_id = %(customer_id)s
        ORDER BY order_date DESC
        LIMIT 5
        """,
        {"customer_id": customer["CUSTOMER_ID"]},
    )
    return {
        "orders": [
            {"order_id": o["ORDER_ID"], "order_date": str(o["ORDER_DATE"]), "status": o["STATUS"]}
            for o in orders
        ]
    }


def _return_eligibility(status: str, delivered_at) -> tuple[bool, dt.date | None]:
    if status != "delivered" or delivered_at is None:
        return False, None
    if isinstance(delivered_at, str):
        delivered_at = dt.date.fromisoformat(delivered_at)
    deadline = delivered_at + dt.timedelta(days=RETURN_WINDOW_DAYS)
    return dt.date.today() <= deadline, deadline


def initiate_return(order_id: str, order_item_id: str, email: str, reason: str) -> dict[str, Any]:
    order = get_order_status(order_id, email)
    if "error" in order:
        return order

    item = next((it for it in order["items"] if it["order_item_id"] == order_item_id), None)
    if item is None:
        return {"error": "item_not_found", "message": f"{order_item_id} isn't on order {order_id}."}

    if not order["return_eligible"]:
        if order["status"] != "delivered":
            reason_msg = f"Order {order_id} hasn't been delivered yet (current status: {order['status']})."
        else:
            reason_msg = (
                f"Order {order_id} was delivered on {order['delivered_at']}, which is outside "
                f"Bookly's {RETURN_WINDOW_DAYS}-day return window (it ended {order['return_eligible_until']})."
            )
        return {"error": "not_eligible", "message": reason_msg}

    return_id = "RET-" + "".join(random.choices(string.digits, k=6))
    refund = round(item["unit_price"] * item["quantity"], 2)

    sf.run_write(
        """
        INSERT INTO returns (return_id, order_id, order_item_id, reason, status, requested_refund)
        VALUES (%(return_id)s, %(order_id)s, %(order_item_id)s, %(reason)s, 'requested', %(refund)s)
        """,
        {
            "return_id": return_id,
            "order_id": order_id,
            "order_item_id": order_item_id,
            "reason": reason,
            "refund": refund,
        },
    )

    return {
        "success": True,
        "return_id": return_id,
        "status": "requested",
        "book_title": item["book_title"],
        "estimated_refund": refund,
    }


def generate_return_label(order_id: str, order_item_id: str, email: str) -> dict[str, Any]:
    order = get_order_status(order_id, email)
    if "error" in order:
        return order

    item = next((it for it in order["items"] if it["order_item_id"] == order_item_id), None)
    if item is None:
        return {"error": "item_not_found", "message": f"{order_item_id} isn't on order {order_id}."}

    existing = sf.run_query(
        """
        SELECT return_id, requested_refund
        FROM returns
        WHERE order_id = %(order_id)s AND order_item_id = %(order_item_id)s
        ORDER BY created_at DESC
        LIMIT 1
        """,
        {"order_id": order_id, "order_item_id": order_item_id},
    )
    if not existing:
        return {
            "error": "no_pending_return",
            "message": f"There's no return on file for {order_item_id} yet -- file one with initiate_return first.",
        }

    ret = existing[0]

    # Mocked: a production build would call a real carrier/label API
    # (EasyPost, Shippo, UPS/FedEx) instead of fabricating one locally.
    tracking_number = "1Z" + "".join(random.choices(string.ascii_uppercase + string.digits, k=14))
    print(f"[MOCK CARRIER] Generated UPS label {tracking_number} for return {ret['RETURN_ID']}")

    return {
        "success": True,
        "return_id": ret["RETURN_ID"],
        "carrier": "UPS",
        "tracking_number": tracking_number,
        "label_url": f"https://labels.bookly-demo.com/{ret['RETURN_ID']}.pdf",
        "drop_off": "Any UPS Store or authorized UPS drop-off location",
        "refund_note": (
            f"The ${ret['REQUESTED_REFUND']} refund is issued once the item arrives back at our "
            "warehouse and passes inspection -- typically 3-5 business days after drop-off, not "
            "at label creation."
        ),
    }


def search_policies(query: str, limit: int = 3) -> dict[str, Any]:
    limit = max(1, min(limit, 5))
    hits = sf.search_policies(query, limit=limit)
    if not hits:
        return {"error": "not_found", "message": "No policy content matched that question."}
    return {
        "results": [
            {"topic": h.get("topic") or h.get("TOPIC"), "title": h.get("title") or h.get("TITLE"), "content": h.get("content") or h.get("CONTENT")}
            for h in hits
        ]
    }


TOOL_IMPLEMENTATIONS = {
    "get_order_status": get_order_status,
    "list_orders_for_customer": list_orders_for_customer,
    "initiate_return": initiate_return,
    "generate_return_label": generate_return_label,
    "search_policies": search_policies,
}
