# SPDX-License-Identifier: LGPL-2.1-or-later
# Copyright (C) 2026 HsingYun
"""One SQLite database per named finger, retaining complete raw USB frames."""

from datetime import datetime, timezone
import sqlite3
import time
from uuid import uuid4

from frame import RAW_FORMAT, RAW_FRAME_SIZE


def saved_fingers(directory):
    fingers = []
    if directory.exists():
        for path in sorted(directory.glob("finger-*.sqlite3")):
            try:
                with sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True) as db:
                    row = db.execute("SELECT value FROM metadata WHERE key='finger_name'").fetchone()
                    if row:
                        fingers.append((row[0], path))
            except sqlite3.DatabaseError:
                continue
    return fingers


class Recording:
    def __init__(self, directory, name=None, path=None):
        directory.mkdir(parents=True, mode=0o700, exist_ok=True)
        self.path = path or directory / f"finger-{uuid4().hex}.sqlite3"
        self.db = sqlite3.connect(self.path)
        self.path.chmod(0o600)
        try:
            if path is None:
                self.db.executescript("""
                    CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                    CREATE TABLE sessions (id INTEGER PRIMARY KEY, started_utc TEXT NOT NULL,
                                           ended_utc TEXT);
                    CREATE TABLE frames (
                        id INTEGER PRIMARY KEY, session_id INTEGER NOT NULL REFERENCES sessions(id),
                        received_unix_ns INTEGER NOT NULL, received_monotonic_ns INTEGER NOT NULL,
                        raw BLOB NOT NULL CHECK(length(raw) = 12166));
                """)
                self.db.executemany("INSERT INTO metadata VALUES (?, ?)", [
                    ("schema_version", "1"), ("finger_name", name),
                    ("raw_format", RAW_FORMAT), ("frame_size", str(RAW_FRAME_SIZE)),
                    ("timestamp_meaning", "host receipt of complete libfprint frame"),
                ])
            metadata = dict(self.db.execute("SELECT key, value FROM metadata"))
            if metadata.get("schema_version") != "1" or metadata.get("raw_format") != RAW_FORMAT:
                raise ValueError("数据库格式不兼容")
            self.name = metadata["finger_name"]
            self.db.execute("PRAGMA foreign_keys=ON")
            self.session_id = self.db.execute("INSERT INTO sessions(started_utc) VALUES (?)",
                                             (self.utc_now(),)).lastrowid
            self.db.commit()
        except Exception:
            self.db.close()
            raise
        self.count = 0
        self.last_commit = time.monotonic()

    @staticmethod
    def utc_now():
        return datetime.now(timezone.utc).isoformat()

    def append(self, frame, unix_ns, monotonic_ns):
        if len(frame) != RAW_FRAME_SIZE:
            raise ValueError("原始帧长度错误")
        self.db.execute("INSERT INTO frames(session_id,received_unix_ns,received_monotonic_ns,raw) "
                        "VALUES (?, ?, ?, ?)", (self.session_id, unix_ns, monotonic_ns, frame))
        self.count += 1
        if time.monotonic() - self.last_commit >= 1:
            self.db.commit()
            self.last_commit = time.monotonic()

    def close(self):
        try:
            self.db.execute("UPDATE sessions SET ended_utc=? WHERE id=?",
                            (self.utc_now(), self.session_id))
            self.db.commit()
        finally:
            self.db.close()
