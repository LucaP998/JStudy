"""Database helpers shared by the web server and the worker."""
import json
import os
import sqlite3
from pathlib import Path

import levels

HERE = Path(__file__).resolve().parent


def connect(path):
    conn = sqlite3.connect(path, timeout=15)
    conn.row_factory = sqlite3.Row
    return conn


def env_level():
    lv = os.environ.get("JSTUDY_LEVEL", levels.DEFAULT_LEVEL).strip().upper()
    return lv if lv in levels.LEVELS else levels.DEFAULT_LEVEL


def init(path):
    """Create the tables, and upgrade databases made by older versions."""
    with connect(path) as conn:
        conn.executescript((HERE / "schema.sql").read_text())
        added = {"sets": "level TEXT", "jobs": "level TEXT", "mocks": "level TEXT", "answers": "mock_id INTEGER"}
        for table, coldef in added.items():
            cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
            if coldef.split()[0] not in cols:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {coldef}")
        # Rows from before levels existed belong to the configured level.
        lv = current_level(conn)
        for table in ("sets", "jobs", "mocks"):
            conn.execute(f"UPDATE {table} SET level = ? WHERE level IS NULL", (lv,))
        # Mock plans from before levels used {key, label}; rename to {subtype}.
        for m in conn.execute("SELECT id, plan FROM mocks").fetchall():
            plan = json.loads(m["plan"])
            if plan and "subtype" not in plan[0]:
                for slot in plan:
                    slot["subtype"] = slot.pop("label", slot.get("key"))
                    slot.pop("key", None)
                conn.execute("UPDATE mocks SET plan = ? WHERE id = ?", (json.dumps(plan), m["id"]))


def current_level(conn):
    r = conn.execute("SELECT value FROM meta WHERE key = 'level'").fetchone()
    if r and r[0] in levels.LEVELS:
        return r[0]
    lv = env_level()
    conn.execute("INSERT OR REPLACE INTO meta VALUES ('level', ?)", (lv,))
    return lv
