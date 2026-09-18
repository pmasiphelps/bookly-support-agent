"""
Which data backend app/tools.py actually talks to.

Defaults to the in-memory mock (app/mock_snowflake.py), so the app runs
immediately with no Snowflake account or setup. Set
BOOKLY_DATA_BACKEND=live (plus the Snowflake variables in .env.example) to
run against a real account instead -- app/tools.py doesn't change either
way, since both backends expose the same run_query/run_write/
search_policies contract.
"""

from __future__ import annotations

import os

if os.environ.get("BOOKLY_DATA_BACKEND", "mock").strip().lower() == "live":
    from . import snowflake_client as _backend
else:
    from . import mock_snowflake as _backend

run_query = _backend.run_query
run_write = _backend.run_write
search_policies = _backend.search_policies
