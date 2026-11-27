"""Database helpers shared by the web server and the worker."""
import json
import sqlite3
from pathlib import Path

import levels

HERE = Path(__file__).resolve().parent


def connect(path):
    conn = sqlite3.connect(path, timeout=15)
    conn.row_factory = sqlite3.Row
    return conn


def init(path):
    """Create the tables, and upgrade databases made by older versions."""
    with connect(path) as conn:
        conn.executescript((HERE / "schema.sql").read_text())
        added = [("sets", "level TEXT"), ("jobs", "level TEXT"), ("mocks", "level TEXT"), ("answers", "mock_id INTEGER"),
                 ("sets", "no_review INTEGER NOT NULL DEFAULT 0")]
        for table, coldef in added:
            cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
            if coldef.split()[0] not in cols:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {coldef}")
        # Rows from before levels existed (only N3 was supported then) are N3.
        legacy = any(conn.execute(f"SELECT 1 FROM {t} WHERE level IS NULL LIMIT 1").fetchone()
                     for t in ("sets", "jobs", "mocks"))
        if legacy:
            for table in ("sets", "jobs", "mocks"):
                conn.execute(f"UPDATE {table} SET level = ? WHERE level IS NULL", (levels.LEGACY_LEVEL,))
            if not current_level(conn):
                conn.execute("INSERT OR REPLACE INTO meta VALUES ('level', ?)", (levels.LEGACY_LEVEL,))
        # Mock plans from before levels used {key, label}; rename to {subtype}.
        for m in conn.execute("SELECT id, plan FROM mocks").fetchall():
            plan = json.loads(m["plan"])
            if plan and "subtype" not in plan[0]:
                for slot in plan:
                    slot["subtype"] = slot.pop("label", slot.get("key"))
                    slot.pop("key", None)
                conn.execute("UPDATE mocks SET plan = ? WHERE id = ?", (json.dumps(plan), m["id"]))


def current_level(conn):
    """The level the student picked in the app, or None before the first choice."""
    r = conn.execute("SELECT value FROM meta WHERE key = 'level'").fetchone()
    return r[0] if r and r[0] in levels.LEVELS else None


# Settings changed in the app (Settings page → Preferences), stored as JSON in meta 'settings'.
MODELS = ("opus", "sonnet", "haiku")
STOCK = {"small": 0.5, "normal": 1.0, "large": 2.0}      # multiplier for how many unseen questions to keep ready
DEFAULT_SETTINGS = {
    "model": "opus",
    "pause_5h": 85,            # % of the 5-hour limit that pauses automatic questions; None = never pause
    "pause_week": 90,          # same for the weekly limit
    "review_steps": [10, 1440, 4320],   # minutes to wait before each review (1–5 steps)
    "stock": "normal",
    "schedule": "studying",    # "studying" = write questions only while the app is in use; "always"
}
MAX_STEP = 365 * 1440


def clean_settings(raw):
    """Validated settings from user input; raises ValueError with a readable message."""
    s = dict(DEFAULT_SETTINGS)
    raw = raw or {}
    if "model" in raw:
        if raw["model"] not in MODELS:
            raise ValueError("Unknown model.")
        s["model"] = raw["model"]
    for key in ("pause_5h", "pause_week"):
        if key in raw:
            v = raw[key]
            if v is not None and not (isinstance(v, int) and 10 <= v <= 100):
                raise ValueError("Pause limits must be between 10% and 100%, or off.")
            s[key] = v
    if "review_steps" in raw:
        steps = raw["review_steps"]
        if not (isinstance(steps, list) and 1 <= len(steps) <= 5
                and all(isinstance(x, int) and 1 <= x <= MAX_STEP for x in steps)):
            raise ValueError("Review needs 1 to 5 steps, each between 1 minute and 1 year.")
        s["review_steps"] = steps
    if "stock" in raw:
        if raw["stock"] not in STOCK:
            raise ValueError("Unknown stock size.")
        s["stock"] = raw["stock"]
    if "schedule" in raw:
        if raw["schedule"] not in ("studying", "always"):
            raise ValueError("Unknown schedule.")
        s["schedule"] = raw["schedule"]
    return s


def get_settings(conn):
    r = conn.execute("SELECT value FROM meta WHERE key = 'settings'").fetchone()
    try:
        return clean_settings(json.loads(r[0]) if r else {})
    except ValueError:
        return dict(DEFAULT_SETTINGS)


def save_settings(conn, raw):
    s = clean_settings(raw)
    conn.execute("INSERT OR REPLACE INTO meta VALUES ('settings', ?)", (json.dumps(s),))
    return s
