from __future__ import annotations

import csv
import io
import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional


class HistoryStore:
    """Small, serialized SQLite writer for pressure samples and audit events."""

    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(path, check_same_thread=False, timeout=5)
        self._db.row_factory = sqlite3.Row
        with self._lock:
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA busy_timeout=5000")
            self._db.executescript(
                """
                CREATE TABLE IF NOT EXISTS samples (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    channel INTEGER NOT NULL,
                    pressure_pa REAL,
                    raw_value REAL,
                    raw_unit TEXT NOT NULL,
                    status_code INTEGER,
                    status TEXT NOT NULL,
                    valid INTEGER NOT NULL,
                    mode TEXT NOT NULL,
                    config_version INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS samples_time_channel
                    ON samples(timestamp, channel);
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    type TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    message TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    details_json TEXT NOT NULL DEFAULT '{}'
                );
                CREATE INDEX IF NOT EXISTS events_time ON events(timestamp);
                """
            )
            self._db.commit()

    def write_sample(self, timestamp: str, channels: Iterable[dict], mode: str, version: int) -> None:
        values = [
            (
                timestamp,
                channel["channel"],
                channel.get("pressure_pa"),
                channel.get("raw_value"),
                channel.get("raw_unit", "Pa"),
                channel.get("status_code"),
                channel.get("status", "未知"),
                1 if channel.get("valid") else 0,
                mode,
                version,
            )
            for channel in channels
        ]
        if not values:
            return
        with self._lock:
            self._db.executemany(
                """INSERT INTO samples(timestamp,channel,pressure_pa,raw_value,raw_unit,
                   status_code,status,valid,mode,config_version) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                values,
            )
            self._db.commit()

    def write_event(
        self, kind: str, severity: str, message: str, actor: str = "system", details: Optional[dict] = None
    ) -> dict:
        timestamp = datetime.now(timezone.utc).isoformat()
        encoded = json.dumps(details or {}, ensure_ascii=False, allow_nan=False)
        with self._lock:
            cur = self._db.execute(
                "INSERT INTO events(timestamp,type,severity,message,actor,details_json) VALUES(?,?,?,?,?,?)",
                (timestamp, kind, severity, message, actor, encoded),
            )
            self._db.commit()
            return {
                "id": cur.lastrowid,
                "timestamp": timestamp,
                "type": kind,
                "severity": severity,
                "message": message,
                "actor": actor,
                "details": details or {},
            }

    def list_events(self, start: Optional[str], end: Optional[str], limit: int = 200) -> list[dict]:
        query = "SELECT * FROM events WHERE 1=1"
        params: list = []
        if start:
            query += " AND timestamp >= ?"
            params.append(start)
        if end:
            query += " AND timestamp <= ?"
            params.append(end)
        query += " ORDER BY timestamp DESC, id DESC LIMIT ?"
        params.append(max(1, min(limit, 1000)))
        with self._lock:
            rows = self._db.execute(query, params).fetchall()
        return [self._event_dict(row) for row in rows]

    @staticmethod
    def _event_dict(row: sqlite3.Row) -> dict:
        return {
            "id": row["id"],
            "timestamp": row["timestamp"],
            "type": row["type"],
            "severity": row["severity"],
            "message": row["message"],
            "actor": row["actor"],
            "details": json.loads(row["details_json"]),
        }

    def history(
        self, start: str, end: str, channels: list[int], limit: int = 100_000
    ) -> list[dict]:
        query = "SELECT * FROM samples WHERE timestamp >= ? AND timestamp <= ?"
        params: list = [start, end]
        if channels:
            query += " AND channel IN (" + ",".join("?" for _ in channels) + ")"
            params.extend(channels)
        query += " ORDER BY timestamp ASC, channel ASC LIMIT ?"
        params.append(max(1, min(limit, 250_000)))
        with self._lock:
            rows = self._db.execute(query, params).fetchall()
        return [dict(row) for row in rows]

    def iter_history(
        self, start: str, end: str, channels: list[int], batch_size: int = 2000
    ):
        """Yield a full export from an independent read connection in bounded batches."""
        query = "SELECT * FROM samples WHERE timestamp >= ? AND timestamp <= ?"
        params: list = [start, end]
        if channels:
            query += " AND channel IN (" + ",".join("?" for _ in channels) + ")"
            params.extend(channels)
        query += " ORDER BY timestamp ASC, channel ASC"
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        try:
            cursor = connection.execute(query, params)
            while True:
                rows = cursor.fetchmany(max(100, min(batch_size, 10_000)))
                if not rows:
                    break
                for row in rows:
                    yield dict(row)
        finally:
            connection.close()

    def csv_text(self, rows: list[dict], display_unit: str = "mbar") -> str:
        factor = {"Pa": 1.0, "mbar": 100.0, "Torr": 133.32236842105263}.get(display_unit, 100.0)
        output = io.StringIO(newline="")
        writer = csv.writer(output)
        writer.writerow(
            ["timestamp_utc", "channel", f"pressure_{display_unit}", "pressure_Pa",
             "raw_value", "raw_unit", "status_code", "status", "valid", "mode", "config_version"]
        )
        for row in rows:
            pressure = row["pressure_pa"]
            writer.writerow(
                [row["timestamp"], row["channel"], None if pressure is None else pressure / factor,
                 pressure, row["raw_value"], row["raw_unit"], row["status_code"], row["status"],
                 row["valid"], row["mode"], row["config_version"]]
            )
        return output.getvalue()

    def csv_chunks(self, rows: Iterable[dict], display_unit: str = "mbar", chunk_size: int = 2000):
        factor = {"Pa": 1.0, "mbar": 100.0, "Torr": 133.32236842105263}.get(display_unit, 100.0)
        output = io.StringIO(newline="")
        writer = csv.writer(output)
        writer.writerow(
            ["timestamp_utc", "channel", f"pressure_{display_unit}", "pressure_Pa",
             "raw_value", "raw_unit", "status_code", "status", "valid", "mode", "config_version"]
        )
        yield "\ufeff" + output.getvalue()
        output.seek(0)
        output.truncate(0)
        for index, row in enumerate(rows, start=1):
            pressure = row["pressure_pa"]
            writer.writerow(
                [row["timestamp"], row["channel"], None if pressure is None else pressure / factor,
                 pressure, row["raw_value"], row["raw_unit"], row["status_code"], row["status"],
                 row["valid"], row["mode"], row["config_version"]]
            )
            if index % max(100, chunk_size) == 0:
                yield output.getvalue()
                output.seek(0)
                output.truncate(0)
        if output.tell():
            yield output.getvalue()

    def purge_before(self, iso_timestamp: str) -> int:
        with self._lock:
            cur = self._db.execute("DELETE FROM samples WHERE timestamp < ?", (iso_timestamp,))
            deleted = cur.rowcount
            self._db.execute("DELETE FROM events WHERE timestamp < ?", (iso_timestamp,))
            self._db.commit()
            return deleted

    def close(self) -> None:
        with self._lock:
            self._db.close()
