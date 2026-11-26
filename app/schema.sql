-- JStudy database (SQLite). Shared by app/server.py (container) and worker/worker.py (host).
-- A "set" is one exam unit: a single question, or a passage/dialogue with
-- several items. Payload format: see README.md. Older databases are upgraded
-- by store.init() (missing columns are added there).
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS sets (
    id          INTEGER PRIMARY KEY,
    level       TEXT,               -- N5 … N1
    category    TEXT NOT NULL,      -- vocab | grammar | order | textgrammar | reading | listening
    subtype     TEXT,               -- e.g. 漢字読み, 文脈規定, 即時応答 (exact names from levels.py)
    target      TEXT,               -- word / grammar point tested (dedup, stats)
    data        TEXT NOT NULL,      -- JSON payload
    origin      TEXT NOT NULL DEFAULT 'auto',   -- auto | request | mock
    job_id      INTEGER,
    audio       TEXT,               -- mp3 file name in data/audio (listening)
    status      TEXT NOT NULL DEFAULT 'ready',  -- ready | needs_audio | audio_failed | flagged | mock | retired
    flag_note   TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

CREATE TABLE IF NOT EXISTS answers (
    id          INTEGER PRIMARY KEY,
    set_id      INTEGER NOT NULL REFERENCES sets(id),
    item        INTEGER NOT NULL DEFAULT 0,
    chosen      INTEGER,
    correct     INTEGER NOT NULL,
    unsure      INTEGER NOT NULL DEFAULT 0,
    mock_id     INTEGER,
    answered_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);
CREATE INDEX IF NOT EXISTS answers_set ON answers(set_id);

CREATE TABLE IF NOT EXISTS jobs (
    id          INTEGER PRIMARY KEY,
    level       TEXT,
    category    TEXT NOT NULL,
    count       INTEGER NOT NULL,
    focus       TEXT,
    origin      TEXT NOT NULL,      -- auto | request | mock
    status      TEXT NOT NULL DEFAULT 'pending',  -- pending | running | done | failed | cancelled
    made        INTEGER NOT NULL DEFAULT 0,
    error       TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    finished_at TEXT
);

-- Mock exams. plan = JSON list of slots in exam order:
-- [{category, subtype, count, items, set_ids: [...]}]. Sets taken by an active
-- mock have status 'mock'; they go back to 'ready' when it ends.
CREATE TABLE IF NOT EXISTS mocks (
    id          INTEGER PRIMARY KEY,
    level       TEXT,
    size        TEXT NOT NULL,          -- full | half
    status      TEXT NOT NULL DEFAULT 'active',   -- active | done | abandoned
    plan        TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    finished_at TEXT
);

CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
