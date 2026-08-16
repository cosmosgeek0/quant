from __future__ import annotations

"""Bounded SQLite history for the Network dashboard."""

import json
import os
import re
import sqlite3
import time
from typing import Optional


RANGES = {
    "1h": (3_600_000, 60_000),
    "6h": (21_600_000, 300_000),
    "24h": (86_400_000, 900_000),
}
STATUS_RANK = {
    "Outage": 6, "Stale": 5, "Degraded": 4,
    "Unknown": 3, "Off": 2, "Operational": 1,
}

_STATUS_FOR_RANK = {rank: status for status, rank in STATUS_RANK.items()}
_MAX_EVENTS = 200
_MAX_BUCKETS = 720
_HOUR_MS = 3_600_000
_SAMPLE_RETENTION_MS = 48 * _HOUR_MS
_CAP_RETENTION_MS = 24 * _HOUR_MS
_EVENT_RETENTION_MS = 30 * 24 * _HOUR_MS
_CAP_DELETE_BATCH = 1_000
_MAX_CAP_PRUNE_ROUNDS = 200
_METRIC_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

_STATUS_RANK_SQL = (
    "CASE WHEN json_valid(metrics) = 0 THEN 3 "
    "WHEN json_type(metrics) <> 'object' THEN 3 ELSE "
    "CASE status WHEN 'Outage' THEN 6 WHEN 'Stale' THEN 5 "
    "WHEN 'Degraded' THEN 4 WHEN 'Unknown' THEN 3 WHEN 'Off' THEN 2 "
    "WHEN 'Operational' THEN 1 ELSE 3 END END"
)
_SCHEMA = """
CREATE TABLE IF NOT EXISTS samples(
 ts_ms INTEGER NOT NULL, edge_id TEXT NOT NULL, status TEXT NOT NULL,
 metrics TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS samples_edge_ts ON samples(edge_id, ts_ms);
CREATE TABLE IF NOT EXISTS events(
 event_id INTEGER PRIMARY KEY AUTOINCREMENT, ts_ms INTEGER NOT NULL,
 edge_id TEXT, transition TEXT NOT NULL, severity TEXT NOT NULL,
 reason TEXT NOT NULL, evidence TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS events_ts ON events(ts_ms);
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
"""


class NetworkStore:
    def __init__(self, path: str,
                 size_cap_bytes: int = 100 * 1024 * 1024) -> None:
        self.path = os.fspath(path)
        self.size_cap_bytes = int(size_cap_bytes)
        self.reset_note: Optional[str] = None
        self._connection: Optional[sqlite3.Connection] = None

    def open(self) -> None:
        if self._connection is not None:
            return
        self.reset_note = None
        try:
            self._connection = self._open_database()
        except sqlite3.DatabaseError:
            if self.path == ":memory:" or not os.path.isfile(self.path):
                raise
            reset_ms = int(time.time() * 1000)
            corrupt_path = "%s.corrupt-%d" % (self.path, reset_ms)
            while os.path.exists(corrupt_path):
                reset_ms += 1
                corrupt_path = "%s.corrupt-%d" % (self.path, reset_ms)
            os.replace(self.path, corrupt_path)
            self._connection = self._open_database()
            self.reset_note = (
                "storage reset after the history database could not be read"
            )

    def _open_database(self) -> sqlite3.Connection:
        if self.path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(self.path)),
                        exist_ok=True)
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA auto_vacuum=INCREMENTAL")
            if connection.execute("PRAGMA auto_vacuum").fetchone()[0] != 2:
                connection.execute("VACUUM")
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=NORMAL")
            connection.executescript(_SCHEMA)
            connection.executemany(
                "INSERT OR IGNORE INTO meta(key, value) VALUES(?, ?)",
                (("schema_version", "1"), ("last_prune_ms", "0")),
            )
            connection.commit()
            return connection
        except Exception:
            connection.close()
            raise

    def close(self) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None

    def _db(self) -> sqlite3.Connection:
        if self._connection is None:
            raise RuntimeError("NetworkStore is not open")
        return self._connection

    @staticmethod
    def _json_object(value: dict) -> str:
        return json.dumps(value, allow_nan=False, separators=(",", ":"),
                          sort_keys=True)

    def insert_sample(self, ts_ms: int, edge_id: str, status: str,
                      metrics: dict, commit: bool = True) -> None:
        self._db().execute(
            "INSERT INTO samples(ts_ms, edge_id, status, metrics) "
            "VALUES(?, ?, ?, ?)",
            (ts_ms, edge_id, status, self._json_object(metrics)),
        )
        if commit:
            self.commit()

    def commit(self) -> None:
        self._db().commit()

    def insert_event(self, ts_ms: int, edge_id: Optional[str],
                     transition: str, severity: str, reason: str,
                     evidence: dict) -> int:
        cursor = self._db().execute(
            "INSERT INTO events(ts_ms, edge_id, transition, severity, reason, "
            "evidence) VALUES(?, ?, ?, ?, ?, ?)",
            (ts_ms, edge_id, transition, severity, reason,
             self._json_object(evidence)),
        )
        self._db().commit()
        return int(cursor.lastrowid)

    def history(self, edge_id: str, metric: str, range_key: str,
                now_ms: int) -> dict:
        if range_key not in RANGES:
            raise ValueError("unknown history range")
        if metric != "status" and not _METRIC_NAME.fullmatch(metric):
            raise ValueError("unknown metric")

        range_ms, bucket_ms = RANGES[range_key]
        bucket_count = range_ms // bucket_ms
        last_bucket_start = (now_ms // bucket_ms) * bucket_ms
        from_ms = last_bucket_start - (bucket_count - 1) * bucket_ms
        metric_columns = ", NULL AS metric_avg, NULL AS metric_max"
        metric_parameters = ()
        valid_metrics_filter = ""
        if metric != "status":
            path = "$.%s" % metric
            numeric = (
                "CASE WHEN json_type(metrics, ?) IN ('integer', 'real') "
                "THEN json_extract(metrics, ?) END"
            )
            metric_columns = ", AVG(%s) metric_avg, MAX(%s) metric_max" % (
                numeric, numeric)
            metric_parameters = (path, path, path, path)
            valid_metrics_filter = " AND json_valid(metrics) = 1"

        query = """
	SELECT CAST((ts_ms - ?) / ? AS INTEGER) bucket_index,
	       COUNT(*) sample_count, MAX(%s) worst_rank %s
	FROM samples
	WHERE edge_id = ? AND ts_ms >= ? AND ts_ms <= ?%s
	GROUP BY bucket_index ORDER BY bucket_index DESC LIMIT ?
""" % (_STATUS_RANK_SQL, metric_columns, valid_metrics_filter)
        parameters = (from_ms, bucket_ms) + metric_parameters + (
            edge_id, from_ms, now_ms, _MAX_BUCKETS)
        rows = reversed(self._db().execute(query, parameters).fetchall())

        buckets = []
        for row in rows:
            bucket = {
                "t": from_ms + int(row["bucket_index"]) * bucket_ms,
                "n": int(row["sample_count"]),
                "status": _STATUS_FOR_RANK.get(int(row["worst_rank"]),
                                                "Unknown"),
            }
            if metric != "status":
                bucket.update(avg=row["metric_avg"], max=row["metric_max"])
            buckets.append(bucket)
        return {
            "edge_id": edge_id, "metric": metric, "range": range_key,
            "bucket_s": bucket_ms // 1000, "from_ms": from_ms,
            "to_ms": now_ms, "buckets": buckets,
        }

    @staticmethod
    def _event_from_row(row: sqlite3.Row) -> dict:
        try:
            evidence = json.loads(row["evidence"])
        except (TypeError, ValueError):
            evidence = {}
        if not isinstance(evidence, dict):
            evidence = {}
        return {
            "event_id": int(row["event_id"]), "ts": int(row["ts_ms"]),
            "edge_id": row["edge_id"], "transition": row["transition"],
            "severity": row["severity"], "reason": row["reason"],
            "evidence": evidence,
        }

    @staticmethod
    def _event_limit(limit: int) -> int:
        return max(0, min(int(limit), _MAX_EVENTS))

    def events_after(self, after_id: int, limit: int) -> dict:
        rows = self._db().execute(
            "SELECT event_id, ts_ms, edge_id, transition, severity, reason, "
            "evidence FROM events WHERE event_id > ? ORDER BY event_id ASC "
            "LIMIT ?",
            (after_id, self._event_limit(limit)),
        ).fetchall()
        events = [self._event_from_row(row) for row in rows]
        next_after = events[-1]["event_id"] if events else after_id
        return {"events": events, "next_after": next_after}

    def latest_events(self, limit: int = 20) -> list:
        rows = self._db().execute(
            "SELECT event_id, ts_ms, edge_id, transition, severity, reason, "
            "evidence FROM events ORDER BY ts_ms DESC, event_id DESC LIMIT ?",
            (self._event_limit(limit),),
        ).fetchall()
        return [self._event_from_row(row) for row in rows]

    def prune(self, now_ms: int) -> dict:
        database = self._db()
        row = database.execute(
            "SELECT value FROM meta WHERE key = 'last_prune_ms'"
        ).fetchone()
        last_prune_ms = int(row["value"]) if row else 0
        elapsed_ms = now_ms - last_prune_ms
        if last_prune_ms > 0 and elapsed_ms < _HOUR_MS:
            current_bytes = self.size_bytes()
            return {"samples_deleted": 0, "events_deleted": 0,
                    "bytes": current_bytes,
                    "cap_met": current_bytes <= self.size_cap_bytes}

        sample_cursor = database.execute(
            "DELETE FROM samples WHERE ts_ms < ?",
            (now_ms - _SAMPLE_RETENTION_MS,),
        )
        event_cursor = database.execute(
            "DELETE FROM events WHERE ts_ms < ?",
            (now_ms - _EVENT_RETENTION_MS,),
        )
        samples_deleted = sample_cursor.rowcount
        events_deleted = event_cursor.rowcount
        database.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES(?, ?)",
            ("last_prune_ms", str(now_ms)),
        )
        database.commit()

        current_bytes = self.size_bytes()
        if current_bytes > self.size_cap_bytes:
            cursor = database.execute(
                "DELETE FROM samples WHERE ts_ms < ?",
                (now_ms - _CAP_RETENTION_MS,),
            )
            samples_deleted += cursor.rowcount
            database.commit()
            database.execute("PRAGMA incremental_vacuum").fetchall()
            database.commit()

        for _round in range(_MAX_CAP_PRUNE_ROUNDS):
            current_bytes = self.size_bytes()
            if current_bytes <= self.size_cap_bytes:
                break
            cursor = database.execute(
                "DELETE FROM samples WHERE rowid IN ("
                "SELECT rowid FROM samples "
                "ORDER BY ts_ms ASC, rowid ASC LIMIT ?)",
                (_CAP_DELETE_BATCH,),
            )
            deleted = cursor.rowcount
            database.commit()
            if deleted <= 0:
                break
            samples_deleted += deleted
            database.execute("PRAGMA incremental_vacuum").fetchall()
            database.commit()

        current_bytes = self.size_bytes()
        return {"samples_deleted": samples_deleted,
                "events_deleted": events_deleted,
                "bytes": current_bytes,
                "cap_met": current_bytes <= self.size_cap_bytes}

    def size_bytes(self) -> int:
        database = self._db()
        pages = int(database.execute("PRAGMA page_count").fetchone()[0])
        page_size = int(database.execute("PRAGMA page_size").fetchone()[0])
        return pages * page_size
