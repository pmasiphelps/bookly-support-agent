"""
SQLite persistence for the Agent Studio: config draft + immutable
versions, deployment history, test cases/runs, and the conversation log
the Monitor tab reads.

One file (data/studio.db by default, BOOKLY_STUDIO_DB to override), stdlib
sqlite3, one lock. Plenty for a single-process demo; a real deployment
would put the log tables in a warehouse (Snowflake, in this repo's case).
"""

from __future__ import annotations

import datetime as dt
import json
import os
import sqlite3
import threading
from pathlib import Path
from typing import Any

from ..agent_config import AgentConfig

DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "studio.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS draft (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    config TEXT NOT NULL,
    base_version_id INTEGER,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    config TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS deployments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    production_version_id INTEGER NOT NULL,
    canary_version_id INTEGER,
    canary_percent INTEGER NOT NULL DEFAULT 0,
    action TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    gate_override_reason TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS test_cases (
    id TEXT PRIMARY KEY,
    data TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS test_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    target TEXT NOT NULL,
    version_id INTEGER,
    fingerprint TEXT NOT NULL,
    status TEXT NOT NULL,
    total INTEGER NOT NULL DEFAULT 0,
    passed INTEGER NOT NULL DEFAULT 0,
    failed INTEGER NOT NULL DEFAULT 0,
    full_suite INTEGER NOT NULL DEFAULT 1,
    started_at TEXT NOT NULL,
    finished_at TEXT
);
CREATE TABLE IF NOT EXISTS test_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL,
    case_id TEXT NOT NULL,
    case_name TEXT NOT NULL,
    kind TEXT NOT NULL,
    passed INTEGER NOT NULL,
    checks TEXT NOT NULL,
    transcript TEXT NOT NULL,
    error TEXT,
    duration_ms INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS conversations (
    id TEXT PRIMARY KEY,
    env TEXT NOT NULL,
    version_id INTEGER,
    started_at TEXT NOT NULL,
    last_at TEXT NOT NULL,
    turn_count INTEGER NOT NULL DEFAULT 0,
    first_message TEXT,
    escalated INTEGER NOT NULL DEFAULT 0,
    csat INTEGER,
    qa TEXT
);
CREATE INDEX IF NOT EXISTS conversations_started ON conversations(started_at);
CREATE TABLE IF NOT EXISTS turns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT NOT NULL,
    idx INTEGER NOT NULL,
    at TEXT NOT NULL,
    user_text TEXT NOT NULL,
    reply TEXT NOT NULL,
    category TEXT,
    route TEXT,
    escalated INTEGER NOT NULL DEFAULT 0,
    tool_calls TEXT NOT NULL,
    llm_calls TEXT NOT NULL,
    latency_ms INTEGER NOT NULL,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    error TEXT
);
CREATE INDEX IF NOT EXISTS turns_conversation ON turns(conversation_id);
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS watchtowers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    criteria TEXT NOT NULL,
    labels TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS watchtower_results (
    watchtower_id INTEGER NOT NULL,
    conversation_id TEXT NOT NULL,
    flagged INTEGER NOT NULL,
    label TEXT,
    reason TEXT NOT NULL DEFAULT '',
    analyzed_at TEXT NOT NULL,
    analyzed_turns INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (watchtower_id, conversation_id)
);
"""

# Columns added after the first release of the schema: (table, column, DDL).
# Applied on startup so an existing data/studio.db keeps working.
_MIGRATIONS = [
    ("conversations", "channel", "TEXT NOT NULL DEFAULT 'chat'"),
    ("conversations", "review", "TEXT"),
    ("conversations", "qa_turns", "INTEGER NOT NULL DEFAULT 0"),
    ("turns", "aops", "TEXT NOT NULL DEFAULT '[]'"),
]


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self, path: str | Path):
        self.path = str(path)
        self._lock = threading.RLock()
        # Called after the store had to recreate a deleted database file,
        # so the app can re-run its first-boot setup (app/main.py sets it).
        self.on_recreate = None
        self._open()

    def _open(self) -> None:
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._raw = sqlite3.connect(self.path, check_same_thread=False)
        self._raw.row_factory = sqlite3.Row
        with self._lock:
            self._raw.executescript(_SCHEMA)
            for table, column, ddl in _MIGRATIONS:
                cols = {r[1] for r in self._raw.execute(f"PRAGMA table_info({table})")}
                if column not in cols:
                    self._raw.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
            self._raw.commit()

    @property
    def _conn(self) -> sqlite3.Connection:
        """The connection -- reopened if the database file was deleted while
        the server was running. SQLite otherwise keeps serving reads from the
        unlinked file but refuses every write ("attempt to write a readonly
        database"), which surfaces as a 500 on the first save."""
        if self.path != ":memory:" and not os.path.exists(self.path):
            with self._lock:
                recreated = not os.path.exists(self.path)
                if recreated:
                    self._raw.close()
                    self._open()
            if recreated and self.on_recreate:
                self.on_recreate()
        return self._raw

    # -- low level ----------------------------------------------------------

    def _exec(self, sql: str, params: tuple | dict = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self._conn.execute(sql, params)
            self._conn.commit()
            return cur

    def _all(self, sql: str, params: tuple | dict = ()) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql, params).fetchall()]

    def _one(self, sql: str, params: tuple | dict = ()) -> dict[str, Any] | None:
        rows = self._all(sql, params)
        return rows[0] if rows else None

    # -- draft + versions ---------------------------------------------------

    def get_draft(self) -> dict[str, Any] | None:
        row = self._one("SELECT * FROM draft WHERE id = 1")
        if row:
            row["config"] = AgentConfig.from_dict(json.loads(row["config"]), check_refs=False)
        return row

    def save_draft(self, config: AgentConfig, base_version_id: int | None) -> None:
        config.validate(check_refs=False)
        self._exec(
            "INSERT INTO draft (id, config, base_version_id, updated_at) VALUES (1, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET config = excluded.config, "
            "base_version_id = excluded.base_version_id, updated_at = excluded.updated_at",
            (json.dumps(config.to_dict()), base_version_id, now()),
        )

    def create_version(self, config: AgentConfig, note: str) -> int:
        config.validate()
        cur = self._exec(
            "INSERT INTO versions (config, fingerprint, note, created_at) VALUES (?, ?, ?, ?)",
            (json.dumps(config.to_dict()), config.fingerprint(), note, now()),
        )
        return int(cur.lastrowid)

    def get_version(self, version_id: int) -> dict[str, Any] | None:
        row = self._one("SELECT * FROM versions WHERE id = ?", (version_id,))
        if row:
            row["config"] = AgentConfig.from_dict(json.loads(row["config"]), check_refs=False)
        return row

    def delete_version(self, version_id: int) -> None:
        self._exec("DELETE FROM versions WHERE id = ?", (version_id,))

    def list_versions(self) -> list[dict[str, Any]]:
        return self._all("SELECT id, fingerprint, note, created_at FROM versions ORDER BY id DESC")

    # -- deployments --------------------------------------------------------

    def record_deployment(
        self,
        production_version_id: int,
        action: str,
        note: str = "",
        canary_version_id: int | None = None,
        canary_percent: int = 0,
        gate_override_reason: str | None = None,
    ) -> int:
        cur = self._exec(
            "INSERT INTO deployments (production_version_id, canary_version_id, canary_percent, "
            "action, note, gate_override_reason, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (production_version_id, canary_version_id, canary_percent, action, note, gate_override_reason, now()),
        )
        return int(cur.lastrowid)

    def current_deployment(self) -> dict[str, Any] | None:
        return self._one("SELECT * FROM deployments ORDER BY id DESC LIMIT 1")

    def deployment_history(self, limit: int = 50) -> list[dict[str, Any]]:
        return self._all("SELECT * FROM deployments ORDER BY id DESC LIMIT ?", (limit,))

    # -- test cases ---------------------------------------------------------

    def upsert_test_case(self, case: dict[str, Any]) -> None:
        self._exec(
            "INSERT INTO test_cases (id, data, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET data = excluded.data, updated_at = excluded.updated_at",
            (case["id"], json.dumps(case), now()),
        )

    def delete_test_case(self, case_id: str) -> None:
        self._exec("DELETE FROM test_cases WHERE id = ?", (case_id,))

    def list_test_cases(self) -> list[dict[str, Any]]:
        rows = self._all("SELECT data FROM test_cases ORDER BY rowid")
        return [json.loads(r["data"]) for r in rows]

    def get_test_case(self, case_id: str) -> dict[str, Any] | None:
        row = self._one("SELECT data FROM test_cases WHERE id = ?", (case_id,))
        return json.loads(row["data"]) if row else None

    # -- test runs ----------------------------------------------------------

    def create_test_run(
        self, target: str, version_id: int | None, fingerprint: str, total: int, full_suite: bool
    ) -> int:
        cur = self._exec(
            "INSERT INTO test_runs (target, version_id, fingerprint, status, total, full_suite, started_at) "
            "VALUES (?, ?, ?, 'running', ?, ?, ?)",
            (target, version_id, fingerprint, total, int(full_suite), now()),
        )
        return int(cur.lastrowid)

    def add_test_result(self, run_id: int, result: dict[str, Any]) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO test_results (run_id, case_id, case_name, kind, passed, checks, transcript, "
                "error, duration_ms) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    run_id, result["case_id"], result["case_name"], result["kind"], int(result["passed"]),
                    json.dumps(result["checks"]), json.dumps(result["transcript"]), result.get("error"),
                    result.get("duration_ms", 0),
                ),
            )
            col = "passed" if result["passed"] else "failed"
            self._conn.execute(f"UPDATE test_runs SET {col} = {col} + 1 WHERE id = ?", (run_id,))
            self._conn.commit()

    def finish_test_run(self, run_id: int, status: str = "completed") -> None:
        self._exec("UPDATE test_runs SET status = ?, finished_at = ? WHERE id = ?", (status, now(), run_id))

    def list_test_runs(self, limit: int = 30) -> list[dict[str, Any]]:
        return self._all("SELECT * FROM test_runs ORDER BY id DESC LIMIT ?", (limit,))

    def get_test_run(self, run_id: int) -> dict[str, Any] | None:
        run = self._one("SELECT * FROM test_runs WHERE id = ?", (run_id,))
        if run:
            results = self._all("SELECT * FROM test_results WHERE run_id = ? ORDER BY id", (run_id,))
            for r in results:
                r["checks"] = json.loads(r["checks"])
                r["transcript"] = json.loads(r["transcript"])
                r["passed"] = bool(r["passed"])
            run["results"] = results
        return run

    def latest_full_run_for_fingerprint(self, fingerprint: str) -> dict[str, Any] | None:
        return self._one(
            "SELECT * FROM test_runs WHERE fingerprint = ? AND full_suite = 1 AND status = 'completed' "
            "ORDER BY id DESC LIMIT 1",
            (fingerprint,),
        )

    def mark_interrupted_runs(self) -> None:
        """A run's worker threads die with the process; don't leave it
        'running' forever after a restart."""
        self._exec("UPDATE test_runs SET status = 'interrupted', finished_at = ? WHERE status = 'running'", (now(),))

    # -- conversation log ---------------------------------------------------

    def log_turn(
        self,
        conversation_id: str,
        env: str,
        version_id: int | None,
        turn: dict[str, Any],
    ) -> None:
        at = now()
        with self._lock:
            existing = self._conn.execute(
                "SELECT turn_count FROM conversations WHERE id = ?", (conversation_id,)
            ).fetchone()
            if existing is None:
                self._conn.execute(
                    "INSERT INTO conversations (id, env, version_id, started_at, last_at, turn_count, "
                    "first_message, escalated, channel) VALUES (?, ?, ?, ?, ?, 0, ?, 0, ?)",
                    (conversation_id, env, version_id, at, at, turn["user_text"][:200], turn.get("channel", "chat")),
                )
                idx = 0
            else:
                idx = existing["turn_count"]
            self._conn.execute(
                "INSERT INTO turns (conversation_id, idx, at, user_text, reply, category, route, escalated, "
                "tool_calls, llm_calls, latency_ms, input_tokens, output_tokens, error, aops) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    conversation_id, idx, at, turn["user_text"], turn["reply"], turn.get("category"),
                    turn.get("route"), int(turn.get("escalated", False)), json.dumps(turn.get("tool_calls", []),
                    default=str), json.dumps(turn.get("llm_calls", [])), turn["latency_ms"],
                    turn.get("input_tokens", 0), turn.get("output_tokens", 0), turn.get("error"),
                    json.dumps(turn.get("aops", [])),
                ),
            )
            self._conn.execute(
                "UPDATE conversations SET last_at = ?, turn_count = turn_count + 1, "
                "escalated = MAX(escalated, ?) WHERE id = ?",
                (at, int(turn.get("escalated", False)), conversation_id),
            )
            self._conn.commit()

    def set_csat(self, conversation_id: str, rating: int) -> bool:
        cur = self._exec("UPDATE conversations SET csat = ? WHERE id = ?", (rating, conversation_id))
        return cur.rowcount > 0

    def set_qa(self, conversation_id: str, qa: dict[str, Any], analyzed_turns: int = 0) -> None:
        self._exec("UPDATE conversations SET qa = ?, qa_turns = ? WHERE id = ?",
                   (json.dumps(qa), analyzed_turns, conversation_id))

    def set_review(self, conversation_id: str, review: dict[str, Any]) -> bool:
        cur = self._exec("UPDATE conversations SET review = ? WHERE id = ?", (json.dumps(review), conversation_id))
        return cur.rowcount > 0

    # -- meta (one-off bookkeeping) -----------------------------------------------

    def get_meta(self, key: str, default: Any = None) -> Any:
        row = self._one("SELECT value FROM meta WHERE key = ?", (key,))
        return json.loads(row["value"]) if row else default

    def set_meta(self, key: str, value: Any) -> None:
        self._exec("INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                   (key, json.dumps(value)))

    # -- watchtower ---------------------------------------------------------

    def save_watchtower(self, wt: dict[str, Any]) -> int:
        labels = json.dumps(wt.get("labels") or [])
        if wt.get("id"):
            self._exec("UPDATE watchtowers SET name = ?, criteria = ?, labels = ?, updated_at = ? WHERE id = ?",
                       (wt["name"], wt["criteria"], labels, now(), wt["id"]))
            # Changed criteria make old verdicts meaningless.
            self._exec("DELETE FROM watchtower_results WHERE watchtower_id = ?", (wt["id"],))
            return int(wt["id"])
        cur = self._exec("INSERT INTO watchtowers (name, criteria, labels, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                         (wt["name"], wt["criteria"], labels, now(), now()))
        return int(cur.lastrowid)

    def delete_watchtower(self, wt_id: int) -> None:
        self._exec("DELETE FROM watchtower_results WHERE watchtower_id = ?", (wt_id,))
        self._exec("DELETE FROM watchtowers WHERE id = ?", (wt_id,))

    def list_watchtowers(self) -> list[dict[str, Any]]:
        rows = self._all("SELECT * FROM watchtowers ORDER BY id")
        for r in rows:
            r["labels"] = json.loads(r["labels"])
        return rows

    def save_watchtower_result(self, wt_id: int, conversation_id: str, verdict: dict[str, Any], turns: int) -> None:
        self._exec(
            "INSERT INTO watchtower_results (watchtower_id, conversation_id, flagged, label, reason, analyzed_at, "
            "analyzed_turns) VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(watchtower_id, conversation_id) DO UPDATE SET "
            "flagged = excluded.flagged, label = excluded.label, reason = excluded.reason, "
            "analyzed_at = excluded.analyzed_at, analyzed_turns = excluded.analyzed_turns",
            (wt_id, conversation_id, int(verdict["flagged"]), verdict.get("label"), verdict.get("reason", ""),
             now(), turns),
        )

    def watchtower_results(self, conversation_ids: list[str] | None = None) -> list[dict[str, Any]]:
        if conversation_ids is None:
            return self._all("SELECT * FROM watchtower_results")
        out: list[dict[str, Any]] = []
        for i in range(0, len(conversation_ids), 500):
            chunk = conversation_ids[i:i + 500]
            out += self._all("SELECT * FROM watchtower_results WHERE conversation_id IN (%s)"
                             % ",".join("?" * len(chunk)), tuple(chunk))
        return out

    def get_conversation(self, conversation_id: str) -> dict[str, Any] | None:
        conv = self._one("SELECT * FROM conversations WHERE id = ?", (conversation_id,))
        if conv is None:
            return None
        conv["qa"] = json.loads(conv["qa"]) if conv["qa"] else None
        conv["review"] = json.loads(conv["review"]) if conv["review"] else None
        turns = self._all("SELECT * FROM turns WHERE conversation_id = ? ORDER BY idx", (conversation_id,))
        for t in turns:
            t["tool_calls"] = json.loads(t["tool_calls"])
            t["llm_calls"] = json.loads(t["llm_calls"])
            t["aops"] = json.loads(t["aops"])
            t["escalated"] = bool(t["escalated"])
        conv["turns"] = turns
        return conv

    def query_conversations(
        self, envs: list[str], since: str | None, version_id: int | None = None
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM conversations WHERE env IN (%s)" % ",".join("?" * len(envs))
        params: list[Any] = list(envs)
        if since:
            sql += " AND started_at >= ?"
            params.append(since)
        if version_id is not None:
            sql += " AND version_id = ?"
            params.append(version_id)
        rows = self._all(sql + " ORDER BY last_at DESC", tuple(params))
        for r in rows:
            r["qa"] = json.loads(r["qa"]) if r["qa"] else None
            r["review"] = json.loads(r["review"]) if r["review"] else None
        return rows

    def query_turns(self, conversation_ids: list[str]) -> list[dict[str, Any]]:
        if not conversation_ids:
            return []
        out: list[dict[str, Any]] = []
        # Chunk to stay under SQLite's bound-parameter limit.
        for i in range(0, len(conversation_ids), 500):
            chunk = conversation_ids[i:i + 500]
            out += self._all(
                "SELECT conversation_id, at, category, route, escalated, tool_calls, latency_ms, "
                "input_tokens, output_tokens, error, aops FROM turns WHERE conversation_id IN (%s)"
                % ",".join("?" * len(chunk)),
                tuple(chunk),
            )
        for t in out:
            t["tool_calls"] = json.loads(t["tool_calls"])
            t["aops"] = json.loads(t["aops"])
        return out


_store: Store | None = None
_store_lock = threading.Lock()


def get_store() -> Store:
    global _store
    with _store_lock:
        if _store is None:
            _store = Store(os.environ.get("BOOKLY_STUDIO_DB", DEFAULT_DB_PATH))
        return _store


def set_store(store: Store | None) -> None:
    """Tests swap in a throwaway store."""
    global _store
    with _store_lock:
        _store = store
