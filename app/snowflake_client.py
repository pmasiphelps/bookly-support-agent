"""
Thin Snowflake access layer -- the "live" data backend.

Not used by default -- app/tools.py talks to app/data_backend.py, which
uses the in-memory mock in app/mock_snowflake.py unless
BOOKLY_DATA_BACKEND=live is set. Set that (plus the Snowflake variables in
.env.example) to point the app at a real account running
snowflake/01_schema.sql through snowflake/04_agent_access.sql.

All agent tools go through `run_query` / `run_write` below rather than
opening their own connections, so there's exactly one place that knows
about Snowflake connection details.
"""

from __future__ import annotations

import os
import threading
from typing import Any

import snowflake.connector
from snowflake.connector import DictCursor
from snowflake.core import Root

_local = threading.local()

# Cortex Search service used by tools.search_policies -- defaults match
# snowflake/03_cortex_search.sql.
_CORTEX_SEARCH_DB = os.environ.get("SNOWFLAKE_DATABASE", "BOOKLY")
_CORTEX_SEARCH_SCHEMA = os.environ.get("SNOWFLAKE_SCHEMA", "SUPPORT")
_CORTEX_SEARCH_SERVICE = os.environ.get("CORTEX_SEARCH_SERVICE", "BOOKLY_POLICY_SEARCH")


def _get_connection():
    """Lazily create one Snowflake connection per thread and reuse it.

    Authenticates as BOOKLY_AGENT_SVC (snowflake/04_agent_access.sql) via
    key-pair auth, not a password -- that user has no password to leak or
    phish in the first place.
    """
    conn = getattr(_local, "conn", None)
    if conn is not None and not conn.is_closed():
        return conn

    conn = snowflake.connector.connect(
        account=os.environ["SNOWFLAKE_ACCOUNT"],
        user=os.environ["SNOWFLAKE_USER"],  # BOOKLY_AGENT_SVC
        authenticator="SNOWFLAKE_JWT",
        private_key_file=os.environ["SNOWFLAKE_PRIVATE_KEY_PATH"],
        private_key_file_pwd=os.environ.get("SNOWFLAKE_PRIVATE_KEY_PASSPHRASE") or None,
        warehouse=os.environ.get("SNOWFLAKE_WAREHOUSE", "BOOKLY_AGENT_WH"),
        database=os.environ.get("SNOWFLAKE_DATABASE", "BOOKLY"),
        schema=os.environ.get("SNOWFLAKE_SCHEMA", "SUPPORT"),
        role=os.environ.get("SNOWFLAKE_ROLE", "BOOKLY_AGENT_ROLE"),
    )
    _local.conn = conn
    return conn


def run_query(sql: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Execute a SELECT and return rows as a list of dicts."""
    conn = _get_connection()
    with conn.cursor(DictCursor) as cur:
        cur.execute(sql, params or {})
        return cur.fetchall()


def run_write(sql: str, params: dict[str, Any] | None = None) -> int:
    """Execute an INSERT/UPDATE and return the affected row count."""
    conn = _get_connection()
    with conn.cursor() as cur:
        cur.execute(sql, params or {})
        return cur.rowcount


def _get_root() -> Root:
    """Lazily build a snowflake.core.Root on the same connection used for
    plain SQL, rather than opening a second one."""
    root = getattr(_local, "root", None)
    if root is None:
        root = Root(_get_connection())
        _local.root = root
    return root


def search_policies(query: str, limit: int = 3) -> list[dict[str, Any]]:
    """Query the Cortex Search service over policy_docs (see
    snowflake/03_cortex_search.sql) and return the top matching excerpts --
    semantic/hybrid search, not a keyword or exact-match lookup."""
    service = (
        _get_root()
        .databases[_CORTEX_SEARCH_DB]
        .schemas[_CORTEX_SEARCH_SCHEMA]
        .cortex_search_services[_CORTEX_SEARCH_SERVICE]
    )
    response = service.search(query=query, columns=["topic", "title", "content"], limit=limit)
    return [dict(r) for r in response.results]
