from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path


class ConfigStore:
    def __init__(self, path: str = "data/config.db"):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        self._init()

    def _init(self):
        with self.conn:
            self.conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS groups_config (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL UNIQUE,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    current_channel_id INTEGER,
                    created_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS group_channels (
                    group_id INTEGER NOT NULL REFERENCES groups_config(id) ON DELETE CASCADE,
                    channel_id INTEGER NOT NULL,
                    priority INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY(group_id, channel_id)
                );
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at INTEGER NOT NULL,
                    payload TEXT NOT NULL
                );
                """
            )

    def groups(self):
        with self.lock:
            return [dict(r) for r in self.conn.execute("SELECT * FROM groups_config ORDER BY id").fetchall()]

    def get_group(self, group_id: int):
        with self.lock:
            row = self.conn.execute("SELECT * FROM groups_config WHERE id=?", (group_id,)).fetchone()
            return dict(row) if row else None

    def create_group(self, name: str):
        with self.lock, self.conn:
            cur = self.conn.execute("INSERT INTO groups_config(name,created_at) VALUES(?,?)", (name.strip(), int(time.time())))
            return self.get_group(cur.lastrowid)

    def update_group(self, group_id: int, name: str | None = None, enabled: bool | None = None):
        fields, values = [], []
        if name is not None:
            fields.append("name=?"); values.append(name.strip())
        if enabled is not None:
            fields.append("enabled=?"); values.append(1 if enabled else 0)
        if fields:
            values.append(group_id)
            with self.lock, self.conn:
                self.conn.execute(f"UPDATE groups_config SET {','.join(fields)} WHERE id=?", values)
        return self.get_group(group_id)

    def delete_group(self, group_id: int):
        with self.lock, self.conn:
            self.conn.execute("DELETE FROM group_channels WHERE group_id=?", (group_id,))
            self.conn.execute("DELETE FROM groups_config WHERE id=?", (group_id,))

    def candidates(self, group_id: int):
        with self.lock:
            return [dict(r) for r in self.conn.execute("SELECT * FROM group_channels WHERE group_id=? ORDER BY priority DESC, channel_id", (group_id,)).fetchall()]

    def replace_candidates(self, group_id: int, rows: list[dict]):
        with self.lock, self.conn:
            self.conn.execute("DELETE FROM group_channels WHERE group_id=?", (group_id,))
            self.conn.executemany("INSERT INTO group_channels(group_id,channel_id,priority) VALUES(?,?,?)", [(group_id, int(r["channel_id"]), int(r.get("priority", 0))) for r in rows])

    def groups_for_channel(self, channel_id: int):
        with self.lock:
            return [dict(r) for r in self.conn.execute("SELECT g.* FROM groups_config g JOIN group_channels c ON c.group_id=g.id WHERE c.channel_id=? AND g.enabled=1", (channel_id,)).fetchall()]

    def set_current(self, group_id: int, channel_id: int):
        with self.lock, self.conn:
            self.conn.execute("UPDATE groups_config SET current_channel_id=? WHERE id=?", (channel_id, group_id))

    def add_event(self, payload: str):
        with self.lock, self.conn:
            self.conn.execute("INSERT INTO events(created_at,payload) VALUES(?,?)", (int(time.time()), payload))

    def recent_events(self, limit=100):
        with self.lock:
            return [dict(r) for r in self.conn.execute("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()]
