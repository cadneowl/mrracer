"""SQLite persistence — a thin, hand-written repository layer (no ORM).

Two kinds of state live here:

* ``events`` — the append-only source of truth. Writes are idempotent via a
  UNIQUE ``dedup_key`` and INSERT OR IGNORE.
* ``mr_snapshots`` / ``poll_state`` — current GitLab metadata and the
  updated_after high-water mark, used by the poller. These are caches, not
  truth; they can be rebuilt by re-polling.
* ``obligations`` — a derived snapshot rebuilt from events by ``recompute``.
  The dashboard derives live from events, so this table is for inspection and
  the statistics phases.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Iterator
from datetime import UTC, datetime
from pathlib import Path

from .events import Event

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    dedup_key   TEXT NOT NULL UNIQUE,
    project_id  INTEGER NOT NULL,
    mr_iid      INTEGER NOT NULL,
    event_type  TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    actor       TEXT,
    reviewer    TEXT,
    payload     TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_events_mr ON events (project_id, mr_iid);
CREATE INDEX IF NOT EXISTS idx_events_time ON events (occurred_at, id);

CREATE TABLE IF NOT EXISTS mr_snapshots (
    project_id     INTEGER NOT NULL,
    mr_iid         INTEGER NOT NULL,
    title          TEXT NOT NULL,
    author         TEXT,
    web_url        TEXT,
    source_branch  TEXT,
    target_branch  TEXT,
    description    TEXT NOT NULL DEFAULT '',
    labels         TEXT NOT NULL DEFAULT '[]',
    draft          INTEGER NOT NULL DEFAULT 0,
    state          TEXT NOT NULL DEFAULT 'opened',
    reviewers      TEXT NOT NULL DEFAULT '[]',
    created_at     TEXT,
    updated_at     TEXT,
    last_polled_at TEXT,
    PRIMARY KEY (project_id, mr_iid)
);

-- Stored skill results (e.g. QA test plans). Keyed by skill `kind` too, so
-- distinct storing skills keep separate results for the same MR.
CREATE TABLE IF NOT EXISTS test_plans (
    project_id   INTEGER NOT NULL,
    mr_iid       INTEGER NOT NULL,
    kind         TEXT NOT NULL DEFAULT 'qa',
    jira_keys    TEXT NOT NULL DEFAULT '',
    content      TEXT NOT NULL,
    generated_at TEXT NOT NULL,
    PRIMARY KEY (project_id, mr_iid, kind)
);

CREATE TABLE IF NOT EXISTS poll_state (
    project_key        TEXT PRIMARY KEY,
    project_id         INTEGER,
    last_updated_after TEXT
);

CREATE TABLE IF NOT EXISTS obligations (
    project_id             INTEGER NOT NULL,
    mr_iid                 INTEGER NOT NULL,
    reviewer               TEXT NOT NULL,
    round                  INTEGER NOT NULL,
    requested_at           TEXT NOT NULL,
    state                  TEXT NOT NULL,
    phase                  TEXT,
    first_response_at      TEXT,
    resolved_at            TEXT,
    resolution_type        TEXT,
    within_sla             INTEGER,
    elapsed_business_hours REAL,
    thread_count           INTEGER NOT NULL DEFAULT 0,
    computed_at            TEXT,
    PRIMARY KEY (project_id, mr_iid, reviewer, round)
);
"""


def _now_utc_iso() -> str:
    return datetime.now(UTC).isoformat()


class Database:
    """Owns a SQLite connection and exposes repository methods."""

    def __init__(self, path: str | Path):
        self.path = str(path)
        self.conn = sqlite3.connect(self.path)
        try:
            self.conn.row_factory = sqlite3.Row
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("PRAGMA foreign_keys=ON")
            self.init_schema()
        except Exception:
            self.conn.close()  # don't leak the handle if setup/migration fails
            raise

    def init_schema(self) -> None:
        self.conn.executescript(_SCHEMA)
        self._migrate()
        self.conn.commit()

    def _migrate(self) -> None:
        """Add columns introduced after a DB was first created."""
        cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(mr_snapshots)")}
        if "description" not in cols:
            self.conn.execute(
                "ALTER TABLE mr_snapshots ADD COLUMN description TEXT NOT NULL DEFAULT ''"
            )

        # test_plans gained `kind` in the primary key so multiple storing skills
        # don't share one row. SQLite can't ALTER a PK, so rebuild the table;
        # existing rows are all QA plans (the only skill that stored before).
        tp_cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(test_plans)")}
        if tp_cols and "kind" not in tp_cols:
            self.conn.executescript(
                """
                ALTER TABLE test_plans RENAME TO test_plans_old;
                CREATE TABLE test_plans (
                    project_id   INTEGER NOT NULL,
                    mr_iid       INTEGER NOT NULL,
                    kind         TEXT NOT NULL DEFAULT 'qa',
                    jira_keys    TEXT NOT NULL DEFAULT '',
                    content      TEXT NOT NULL,
                    generated_at TEXT NOT NULL,
                    PRIMARY KEY (project_id, mr_iid, kind)
                );
                INSERT INTO test_plans
                    (project_id, mr_iid, kind, jira_keys, content, generated_at)
                    SELECT project_id, mr_iid, 'qa', jira_keys, content, generated_at
                    FROM test_plans_old;
                DROP TABLE test_plans_old;
                """
            )

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> Database:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # --- events ------------------------------------------------------------

    def insert_events(self, events: Iterable[Event]) -> int:
        """Insert events idempotently. Returns the number newly inserted."""
        rows = [
            (
                e.dedup_key,
                e.project_id,
                e.mr_iid,
                e.event_type,
                e.occurred_at.astimezone(UTC).isoformat(),
                e.actor,
                e.reviewer,
                e.payload_json(),
            )
            for e in events
        ]
        if not rows:
            return 0
        before = self.conn.total_changes
        self.conn.executemany(
            "INSERT OR IGNORE INTO events "
            "(dedup_key, project_id, mr_iid, event_type, occurred_at, actor, reviewer, payload) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
        self.conn.commit()
        return self.conn.total_changes - before

    def iter_events(
        self, project_id: int | None = None, mr_iid: int | None = None
    ) -> Iterator[Event]:
        sql = "SELECT * FROM events"
        clauses, params = [], []
        if project_id is not None:
            clauses.append("project_id = ?")
            params.append(project_id)
        if mr_iid is not None:
            clauses.append("mr_iid = ?")
            params.append(mr_iid)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY occurred_at, id"
        for row in self.conn.execute(sql, params):
            yield _row_to_event(row)

    def all_events(self) -> list[Event]:
        return list(self.iter_events())

    def event_count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]

    # --- MR snapshots ------------------------------------------------------

    def upsert_mr_snapshot(
        self,
        *,
        project_id: int,
        mr_iid: int,
        title: str,
        author: str | None,
        web_url: str | None,
        source_branch: str | None,
        target_branch: str | None,
        description: str,
        labels: list[str],
        draft: bool,
        state: str,
        reviewers: list[str],
        created_at: str | None,
        updated_at: str | None,
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO mr_snapshots
                (project_id, mr_iid, title, author, web_url, source_branch,
                 target_branch, description, labels, draft, state, reviewers,
                 created_at, updated_at, last_polled_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(project_id, mr_iid) DO UPDATE SET
                title=excluded.title, author=excluded.author, web_url=excluded.web_url,
                source_branch=excluded.source_branch, target_branch=excluded.target_branch,
                description=excluded.description, labels=excluded.labels, draft=excluded.draft,
                state=excluded.state, reviewers=excluded.reviewers, created_at=excluded.created_at,
                updated_at=excluded.updated_at, last_polled_at=excluded.last_polled_at
            """,
            (
                project_id,
                mr_iid,
                title,
                author,
                web_url,
                source_branch,
                target_branch,
                description or "",
                json.dumps(labels),
                1 if draft else 0,
                state,
                json.dumps(reviewers),
                created_at,
                updated_at,
                _now_utc_iso(),
            ),
        )
        self.conn.commit()

    # --- stored skill results (QA test plans, etc.) ------------------------

    def save_test_plan(
        self, project_id: int, mr_iid: int, kind: str, jira_keys: str, content: str
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO test_plans (project_id, mr_iid, kind, jira_keys, content, generated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(project_id, mr_iid, kind) DO UPDATE SET
                jira_keys=excluded.jira_keys, content=excluded.content,
                generated_at=excluded.generated_at
            """,
            (project_id, mr_iid, kind, jira_keys, content, _now_utc_iso()),
        )
        self.conn.commit()

    def get_test_plan(self, project_id: int, mr_iid: int, kind: str = "qa") -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM test_plans WHERE project_id=? AND mr_iid=? AND kind=?",
            (project_id, mr_iid, kind),
        ).fetchone()
        return dict(row) if row else None

    def stored_kinds(self, project_id: int, mr_iid: int) -> list[str]:
        """Skill kinds that have a stored result for this MR (for board badges)."""
        rows = self.conn.execute(
            "SELECT kind FROM test_plans WHERE project_id=? AND mr_iid=?",
            (project_id, mr_iid),
        ).fetchall()
        return [r["kind"] for r in rows]

    def get_snapshot(self, project_id: int, mr_iid: int) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM mr_snapshots WHERE project_id=? AND mr_iid=?",
            (project_id, mr_iid),
        ).fetchone()
        return _snapshot_row(row) if row else None

    def open_snapshots(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM mr_snapshots WHERE state='opened' ORDER BY project_id, mr_iid"
        )
        return [_snapshot_row(r) for r in rows]

    def all_snapshots(self) -> list[dict]:
        rows = self.conn.execute("SELECT * FROM mr_snapshots ORDER BY project_id, mr_iid")
        return [_snapshot_row(r) for r in rows]

    # --- poll state --------------------------------------------------------

    def get_poll_state(self, project_key: str) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM poll_state WHERE project_key=?", (project_key,)
        ).fetchone()
        return dict(row) if row else None

    def set_poll_state(
        self, project_key: str, project_id: int | None, last_updated_after: str | None
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO poll_state (project_key, project_id, last_updated_after)
            VALUES (?, ?, ?)
            ON CONFLICT(project_key) DO UPDATE SET
                project_id=excluded.project_id,
                last_updated_after=excluded.last_updated_after
            """,
            (project_key, project_id, last_updated_after),
        )
        self.conn.commit()

    # --- derived obligations (rebuilt by recompute) ------------------------

    def replace_obligations(self, obligations: Iterable[dict]) -> int:
        cur = self.conn.cursor()
        cur.execute("DELETE FROM obligations")
        rows = [
            (
                o["project_id"],
                o["mr_iid"],
                o["reviewer"],
                o["round"],
                o["requested_at"],
                o["state"],
                o.get("phase"),
                o.get("first_response_at"),
                o.get("resolved_at"),
                o.get("resolution_type"),
                None if o.get("within_sla") is None else (1 if o["within_sla"] else 0),
                o.get("elapsed_business_hours"),
                o.get("thread_count", 0),
                _now_utc_iso(),
            )
            for o in obligations
        ]
        cur.executemany(
            """
            INSERT INTO obligations
                (project_id, mr_iid, reviewer, round, requested_at, state, phase,
                 first_response_at, resolved_at, resolution_type, within_sla,
                 elapsed_business_hours, thread_count, computed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
        self.conn.commit()
        return len(rows)


def _row_to_event(row: sqlite3.Row) -> Event:
    return Event(
        project_id=row["project_id"],
        mr_iid=row["mr_iid"],
        event_type=row["event_type"],
        occurred_at=datetime.fromisoformat(row["occurred_at"]),
        dedup_key=row["dedup_key"],
        actor=row["actor"],
        reviewer=row["reviewer"],
        payload=json.loads(row["payload"]),
    )


def _snapshot_row(row: sqlite3.Row) -> dict:
    data = dict(row)
    data["labels"] = json.loads(data.get("labels") or "[]")
    data["reviewers"] = json.loads(data.get("reviewers") or "[]")
    data["draft"] = bool(data.get("draft"))
    return data
