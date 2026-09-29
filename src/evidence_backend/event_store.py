"""事件溯源存储层。

所有状态变更都以事件形式追加到 ``event_log``，业务表只保存聚合快照。
SQLite 触发器在数据库层面禁止对事件日志执行 UPDATE / DELETE，
保证撤销、重复提交、部分替代、申诉复核等历史只能追加、不可篡改。
"""
from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS event_log (
    seq          INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id     TEXT NOT NULL UNIQUE,
    aggregate    TEXT NOT NULL,
    aggregate_id TEXT NOT NULL,
    event_type   TEXT NOT NULL,
    event_data   TEXT NOT NULL,
    actor_id     TEXT NOT NULL,
    actor_role   TEXT NOT NULL,
    org_id       TEXT NOT NULL DEFAULT '',
    occurred_at  TEXT NOT NULL,
    version      INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_event_agg ON event_log(aggregate, aggregate_id, seq);
CREATE INDEX IF NOT EXISTS idx_event_time ON event_log(occurred_at);

CREATE TABLE IF NOT EXISTS snapshot (
    aggregate    TEXT NOT NULL,
    aggregate_id TEXT NOT NULL,
    version      INTEGER NOT NULL,
    state        TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    PRIMARY KEY (aggregate, aggregate_id)
);

CREATE TABLE IF NOT EXISTS read_model (
    name  TEXT NOT NULL,
    key   TEXT NOT NULL,
    value TEXT NOT NULL,
    PRIMARY KEY (name, key)
);

-- 只追加（append-only）物理保护：任何修改或删除历史事件的尝试都被拒绝。
CREATE TRIGGER IF NOT EXISTS trg_event_no_update
BEFORE UPDATE ON event_log
BEGIN
    SELECT RAISE(ABORT, 'event_log 为只追加日志，禁止 UPDATE');
END;
CREATE TRIGGER IF NOT EXISTS trg_event_no_delete
BEFORE DELETE ON event_log
BEGIN
    SELECT RAISE(ABORT, 'event_log 为只追加日志，禁止 DELETE');
END;
"""


def utcnow() -> datetime:
    """统一的 UTC 时间源，测试中可替换。"""
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="microseconds")


def parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value)


class EventStore:
    """封装 SQLite 的事件存储与快照管理。"""

    def __init__(self, dsn: str = ":memory:") -> None:
        self._conn = sqlite3.connect(dsn, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._conn.executescript(SCHEMA)

    @classmethod
    def open_file(cls, path: str | Path) -> "EventStore":
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        return cls(str(path))

    @property
    def connection(self) -> sqlite3.Connection:
        return self._conn

    def close(self) -> None:
        self._conn.close()

    # ---- 事件读写 -------------------------------------------------------

    def append(
        self,
        *,
        event_id: str,
        aggregate: str,
        aggregate_id: str,
        event_type: str,
        data: dict[str, Any],
        version: int,
        actor_id: str,
        actor_role: str,
        org_id: str = "",
        occurred_at: datetime | None = None,
    ) -> int:
        """追加一个事件；``version`` 为该事件写入后聚合的版本号。"""
        ts = iso(occurred_at or utcnow())
        cur = self._conn.execute(
            """
            INSERT INTO event_log
                (event_id, aggregate, aggregate_id, event_type, event_data,
                 actor_id, actor_role, org_id, occurred_at, version)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_id,
                aggregate,
                aggregate_id,
                event_type,
                json.dumps(data, ensure_ascii=False, sort_keys=True),
                actor_id,
                actor_role,
                org_id,
                ts,
                version,
            ),
        )
        return int(cur.lastrowid)

    def events_for(self, aggregate: str, aggregate_id: str) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            """
            SELECT * FROM event_log
            WHERE aggregate = ? AND aggregate_id = ?
            ORDER BY seq ASC
            """,
            (aggregate, aggregate_id),
        ).fetchall()
        return [self._decode(r) for r in rows]

    def all_events(self, aggregate: str | None = None) -> list[dict[str, Any]]:
        if aggregate is None:
            rows = self._conn.execute("SELECT * FROM event_log ORDER BY seq ASC").fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM event_log WHERE aggregate = ? ORDER BY seq ASC", (aggregate,)
            ).fetchall()
        return [self._decode(r) for r in rows]

    def append_many(self, events: Iterable[dict[str, Any]]) -> list[int]:
        ids: list[int] = []
        with self._conn:
            for event in events:
                ids.append(self.append(**event))
        return ids

    # ---- 快照 -----------------------------------------------------------

    def save_snapshot(
        self, aggregate: str, aggregate_id: str, version: int, state: dict[str, Any]
    ) -> None:
        self._conn.execute(
            """
            INSERT INTO snapshot (aggregate, aggregate_id, version, state, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(aggregate, aggregate_id) DO UPDATE SET
                version = excluded.version,
                state = excluded.state,
                updated_at = excluded.updated_at
            """,
            (aggregate, aggregate_id, version, json.dumps(state, ensure_ascii=False), iso(utcnow())),
        )

    def load_snapshot(self, aggregate: str, aggregate_id: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT state FROM snapshot WHERE aggregate = ? AND aggregate_id = ?",
            (aggregate, aggregate_id),
        ).fetchone()
        return json.loads(row["state"]) if row else None

    def snapshot_ids(self, aggregate: str) -> list[str]:
        rows = self._conn.execute(
            "SELECT aggregate_id FROM snapshot WHERE aggregate = ? ORDER BY aggregate_id",
            (aggregate,),
        ).fetchall()
        return [r["aggregate_id"] for r in rows]

    # ---- 读模型 ---------------------------------------------------------

    def rm_put(self, name: str, key: str, value: dict[str, Any]) -> None:
        self._conn.execute(
            """
            INSERT INTO read_model(name, key, value) VALUES (?, ?, ?)
            ON CONFLICT(name, key) DO UPDATE SET value = excluded.value
            """,
            (name, key, json.dumps(value, ensure_ascii=False, sort_keys=True)),
        )

    def rm_delete(self, name: str, key: str) -> None:
        self._conn.execute("DELETE FROM read_model WHERE name = ? AND key = ?", (name, key))

    def rm_get(self, name: str, key: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT value FROM read_model WHERE name = ? AND key = ?", (name, key)
        ).fetchone()
        return json.loads(row["value"]) if row else None

    def rm_list(self, name: str) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT value FROM read_model WHERE name = ? ORDER BY key", (name,)
        ).fetchall()
        return [json.loads(r["value"]) for r in rows]

    def commit(self) -> None:
        self._conn.commit()

    @staticmethod
    def _decode(row: sqlite3.Row) -> dict[str, Any]:
        event = {
            "seq": row["seq"],
            "event_id": row["event_id"],
            "aggregate": row["aggregate"],
            "aggregate_id": row["aggregate_id"],
            "event_type": row["event_type"],
            "data": json.loads(row["event_data"]),
            "actor_id": row["actor_id"],
            "actor_role": row["actor_role"],
            "org_id": row["org_id"],
            "occurred_at": row["occurred_at"],
            "version": row["version"],
        }
        return event
