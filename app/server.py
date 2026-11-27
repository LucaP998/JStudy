"""JStudy web server (runs in the jstudy-web container).

Serves the single-page UI, the listening mp3s and a small JSON API on top of
the SQLite database that worker/worker.py (host, headless Claude + VOICEVOX)
fills with questions. Python stdlib only. Everything shown is for the current
JLPT level (meta 'level'); other levels' data stays in the database.
"""
import json
import os
import random
import re
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import levels
import store

HERE = Path(__file__).resolve().parent
DB_PATH = os.environ.get("DB_PATH", "/data/jstudy.db")
AUDIO_DIR = Path(os.environ.get("AUDIO_DIR", "/data/audio"))
PORT = int(os.environ.get("PORT", "8097"))

CATEGORIES = levels.CATEGORIES
# Relative share of each category in mixed mode, multiplied by the recent error rate.
MIX_WEIGHTS = {"vocab": 3, "grammar": 2, "order": 1, "textgrammar": 1, "reading": 2, "listening": 3}
# A set answered wrong (or "unsure") comes back after the review steps chosen in
# Settings; each correct review moves it one step; after the last step it is retired.
ASK_MAX = 1000                # characters of a question to Claude about an answered question
PROFILE_MAX = 4000            # characters of the learner profile sent with every Claude request
REVIEW_SHARE = 0.35          # chance to show a due review instead of a new set
FMT = "%Y-%m-%d %H:%M:%S"


def db():
    return store.connect(DB_PATH)


def now():
    return datetime.now().strftime(FMT)


def touch(conn):
    """Heartbeat read by the worker: the student is studying right now."""
    conn.execute("INSERT OR REPLACE INTO meta VALUES ('last_active', ?)", (now(),))


def review_state(conn, level):
    """{set_id: (due, last_answered, due_items)} for the level's usable sets in the
    review cycle. Each question of a set is tracked on its own, so a reading text
    with one missed question comes back with only that question. Every answer to a
    question is an attempt; wrong or unsure ones restart its review steps."""
    attempts = {}
    for r in conn.execute("""SELECT a.set_id, a.item, a.correct, a.unsure, a.answered_at
                             FROM answers a JOIN sets s ON s.id = a.set_id
                             WHERE s.level = ? AND s.status = 'ready' AND NOT COALESCE(s.no_review, 0)
                             ORDER BY a.id""", (level,)):
        attempts.setdefault((r["set_id"], r["item"]), []).append(
            ((not r["correct"]) or bool(r["unsure"]), r["answered_at"]))
    t = datetime.now()
    steps = [timedelta(minutes=m) for m in store.get_settings(conn)["review_steps"]]
    out = {}
    for (sid, item), lst in attempts.items():
        bads = [i for i, (bad, _) in enumerate(lst) if bad]
        if not bads:
            continue
        goods_after = len(lst) - 1 - bads[-1]
        if goods_after >= len(steps):
            continue
        last = datetime.strptime(lst[-1][1], FMT)
        due, prev_last, items = out.get(sid, (False, last, []))
        if t - last >= steps[goods_after]:
            due = True
            items = sorted(items + [item])
        out[sid] = (due, max(last, prev_last), items)
    return out


def error_rates(conn, level):
    rates = {}
    for r in conn.execute(
            """SELECT s.category, AVG(CASE WHEN a.correct AND NOT a.unsure THEN 0.0 ELSE 1.0 END) AS err
               FROM (SELECT * FROM answers ORDER BY id DESC LIMIT 1000) a JOIN sets s ON s.id = a.set_id
               WHERE s.level = ? GROUP BY s.category""", (level,)):
        rates[r["category"]] = r["err"]
    return rates


def set_payload(row, reason, only_items=None):
    """A set as sent to the page. For a review, only_items keeps just the questions that
    are due; item_map gives their original numbers (answers are stored under those)."""
    data = json.loads(row["data"])
    if only_items:
        data["item_count"] = len(data["items"])
        data["items"] = [data["items"][i] for i in only_items]
        data["item_map"] = only_items
    return {"id": row["id"], "level": row["level"], "category": row["category"], "subtype": row["subtype"],
            "target": row["target"], "audio": row["audio"], "origin": row["origin"],
            "reason": reason, **data}


def review_payload(row, state):
    return set_payload(row, "review", state[row["id"]][2])


def pick_next(conn, level, mode, exclude):
    cats = CATEGORIES if mode in ("mix", "review") else [mode]
    marks = ",".join("?" * len(cats))
    no_mock = "AND origin != 'mock'" if active_mock(conn) else ""
    ready = {r["id"]: r for r in conn.execute(
        f"SELECT * FROM sets WHERE status = 'ready' AND level = ? AND category IN ({marks}) {no_mock}",
        (level, *cats))}
    state = review_state(conn, level)
    due = sorted((last, sid) for sid, (is_due, last, _) in state.items()
                 if is_due and sid in ready and sid not in exclude)
    if mode == "review":
        if due:
            return review_payload(ready[due[0][1]], state), None
        pending = [sid for sid, (is_due, _, _) in state.items() if not is_due and sid in ready]
        return None, ("No reviews due right now." if not pending else
                      f"No reviews due right now ({len(pending)} waiting for their next review).")

    answered = {r[0] for r in conn.execute("SELECT DISTINCT set_id FROM answers")}
    unseen = [r for sid, r in ready.items() if sid not in answered and sid not in exclude]
    if due and (not unseen or random.random() < REVIEW_SHARE):
        return review_payload(ready[due[0][1]], state), None
    if not unseen:
        return None, "No new questions left here. Claude is generating more; this page checks again by itself."
    if mode == "mix":
        rates = error_rates(conn, level)
        avail = sorted({r["category"] for r in unseen})
        weights = [MIX_WEIGHTS[c] * (0.5 + rates.get(c, 0.5)) for c in avail]
        cat = random.choices(avail, weights)[0]
        unseen = [r for r in unseen if r["category"] == cat]
    # Requested sets first (the student asked for them), then oldest first.
    unseen.sort(key=lambda r: (r["origin"] != "request", r["id"]))
    return set_payload(unseen[0], "new"), None


# ------------------------------------------------------------------ mock exams

def active_mock(conn):
    return conn.execute("SELECT * FROM mocks WHERE status = 'active' ORDER BY id DESC LIMIT 1").fetchone()


def assemble(conn, mock):
    """Fill the mock's empty slots with unseen ready sets of the right type
    (sets Claude wrote for this mock first). Returns the updated plan."""
    plan = json.loads(mock["plan"])
    changed = False
    for slot in plan:
        need = slot["count"] - len(slot["set_ids"])
        if need <= 0:
            continue
        rows = conn.execute(
            """SELECT id FROM sets WHERE status = 'ready' AND level = ? AND category = ? AND subtype = ?
               AND id NOT IN (SELECT set_id FROM answers)
               ORDER BY origin = 'mock' DESC, id LIMIT ?""",
            (mock["level"], slot["category"], slot["subtype"], need)).fetchall()
        for r in rows:
            conn.execute("UPDATE sets SET status = 'mock' WHERE id = ?", (r["id"],))
            slot["set_ids"].append(r["id"])
            changed = True
    if changed:
        conn.execute("UPDATE mocks SET plan = ? WHERE id = ?", (json.dumps(plan), mock["id"]))
    return plan


def start_mock(conn, level, size):
    plan = [dict(s, set_ids=[]) for s in levels.mock_plan(level, size)]
    cur = conn.execute("INSERT INTO mocks (level, size, plan) VALUES (?, ?, ?)", (level, size, json.dumps(plan)))
    mock = conn.execute("SELECT * FROM mocks WHERE id = ?", (cur.lastrowid,)).fetchone()
    plan = assemble(conn, mock)
    # Ask Claude for whatever is missing, in exam order (the worker runs jobs by id).
    for slot in plan:
        need = slot["count"] - len(slot["set_ids"])
        if need > 0:
            conn.execute("INSERT INTO jobs (level, category, count, focus, origin) VALUES (?, ?, ?, ?, 'mock')",
                         (level, slot["category"], need,
                          f"Mock exam: write ONLY sets of the subtype 「{slot['subtype']}」, and use exactly "
                          f"that text as the subtype field."))


def end_mock(conn, mock, status):
    plan = json.loads(mock["plan"])
    ids = [i for s in plan for i in s["set_ids"]]
    if ids:
        marks = ",".join("?" * len(ids))
        conn.execute(f"UPDATE sets SET status = 'ready' WHERE status = 'mock' AND id IN ({marks})", ids)
    # Sets still being written for it simply join the normal pool.
    conn.execute("UPDATE sets SET origin = 'auto' WHERE origin = 'mock'")
    conn.execute("UPDATE jobs SET origin = 'auto' WHERE origin = 'mock' AND status IN ('pending', 'running')")
    conn.execute("UPDATE mocks SET status = ?, finished_at = ? WHERE id = ?", (status, now(), mock["id"]))


def discard_mock(conn, mock):
    """Remove a mock and every answer given in it, as if it never happened.
    Its questions go back to the unseen pool."""
    if mock["status"] == "active":
        # Questions still to be written for it are not wanted either: don't spend Claude usage on them.
        conn.execute("""UPDATE jobs SET status = 'cancelled', finished_at = datetime('now', 'localtime')
                        WHERE origin = 'mock' AND status = 'pending'""")
        end_mock(conn, mock, "abandoned")
    conn.execute("DELETE FROM answers WHERE mock_id = ?", (mock["id"],))
    conn.execute("DELETE FROM mocks WHERE id = ?", (mock["id"],))


def load_sets(conn, mock):
    ids = [i for s in json.loads(mock["plan"]) for i in s["set_ids"]]
    if not ids:
        return {}
    rows = conn.execute(f"SELECT * FROM sets WHERE id IN ({','.join('?' * len(ids))})", ids).fetchall()
    return {r["id"]: set_payload(r, "mock") for r in rows}


def mock_scores(conn, mock, sets):
    """Per section and per slot: correct / total items (unanswered items count as wrong)."""
    got = {(r["set_id"], r["item"]): r for r in conn.execute(
        "SELECT * FROM answers WHERE mock_id = ? ORDER BY id", (mock["id"],))}
    sections = {sec: {"correct": 0, "total": 0} for sec in levels.SCORING[mock["level"]]["sections"]}
    slots = []
    for slot in json.loads(mock["plan"]):
        ok = total = 0
        for sid in slot["set_ids"]:
            for i in range(len(sets[sid]["items"])):
                total += 1
                a = got.get((sid, i))
                ok += bool(a and a["correct"])
        sec = sections[levels.section_of(mock["level"], slot["category"])]
        sec["correct"] += ok
        sec["total"] += total
        slots.append({"subtype": slot["subtype"], "category": slot["category"], "correct": ok, "total": total})
    return sections, slots, got


def scoring_info(level):
    s = levels.SCORING[level]
    return {"sections": list(s["sections"]), "max": s["max"], "pass_total": s["pass_total"],
            "pass_section": s["pass_section"]}


def mock_view(conn, level):
    history = []
    for m in conn.execute("SELECT * FROM mocks WHERE status = 'done' AND level = ? ORDER BY id DESC LIMIT 10",
                          (level,)).fetchall():
        history.append({"id": m["id"], "size": m["size"], "finished_at": m["finished_at"],
                        "sections": mock_scores(conn, m, load_sets(conn, m))[0]})
    info = {"level": level, "scoring": scoring_info(level), "history": history,
            "sizes": {sz: levels.mock_items(level, sz) for sz in ("half", "full")}}
    mock = active_mock(conn)
    if not mock:
        return {"mock": None, **info}
    plan = assemble(conn, mock)
    mock = conn.execute("SELECT * FROM mocks WHERE id = ?", (mock["id"],)).fetchone()
    sets = load_sets(conn, mock)
    answered = {f"{r['set_id']}:{r['item']}": r["chosen"] for r in conn.execute(
        "SELECT set_id, item, chosen FROM answers WHERE mock_id = ?", (mock["id"],))}
    # Still on the way: sets Claude has yet to write, and written listening sets still being recorded.
    pending = conn.execute("SELECT COALESCE(SUM(count - made), 0) FROM jobs WHERE origin = 'mock' "
                           "AND status IN ('pending', 'running')").fetchone()[0]
    pending += conn.execute("SELECT COUNT(*) FROM sets WHERE status = 'needs_audio' AND level = ?",
                            (mock["level"],)).fetchone()[0]
    total = sum(s["count"] * s.get("items", 1) for s in plan)
    return {**info, "mock": {"id": mock["id"], "level": mock["level"], "size": mock["size"],
                             "created_at": mock["created_at"], "total_items": total},
            "slots": [{"subtype": s["subtype"], "category": s["category"], "count": s["count"],
                       "filled": len(s["set_ids"])} for s in plan],
            "sets": [sets[i] for s in plan for i in s["set_ids"]],
            "answered": answered, "pending": pending}


def mock_result(conn, mock_id):
    mock = conn.execute("SELECT * FROM mocks WHERE id = ?", (mock_id,)).fetchone()
    if not mock:
        return None
    sets = load_sets(conn, mock)
    sections, slots, got = mock_scores(conn, mock, sets)
    review = []
    for s in json.loads(mock["plan"]):
        for sid in s["set_ids"]:
            st = sets[sid]
            marks = [{"chosen": got[(sid, i)]["chosen"] if (sid, i) in got else None,
                      "correct": bool(got.get((sid, i)) and got[(sid, i)]["correct"]),
                      "unsure": bool(got.get((sid, i)) and got[(sid, i)]["unsure"])}
                     for i in range(len(st["items"]))]
            if any(not m["correct"] or m["unsure"] for m in marks):
                review.append({**st, "marks": marks})
    return {"id": mock["id"], "level": mock["level"], "size": mock["size"], "created_at": mock["created_at"],
            "finished_at": mock["finished_at"], "scoring": scoring_info(mock["level"]),
            "sections": sections, "slots": slots, "review": review}


def stats(conn, level):
    cats = {c: {"answered": 0, "correct": 0, "unsure": 0, "week_answered": 0, "week_correct": 0,
                "unseen": 0, "generating": 0} for c in CATEGORIES}
    week = (datetime.now() - timedelta(days=7)).strftime(FMT)
    for r in conn.execute(
            """SELECT s.category c, COUNT(*) n, SUM(a.correct AND NOT a.unsure) ok, SUM(a.unsure) uns,
                      SUM(a.answered_at >= ?) wn, SUM(a.answered_at >= ? AND a.correct AND NOT a.unsure) wok
               FROM answers a JOIN sets s ON s.id = a.set_id WHERE s.level = ? GROUP BY s.category""",
            (week, week, level)):
        if r["c"] in cats:
            cats[r["c"]].update(answered=r["n"], correct=r["ok"], unsure=r["uns"],
                                week_answered=r["wn"], week_correct=r["wok"])
    for r in conn.execute(
            """SELECT category c, COUNT(*) n FROM sets WHERE status = 'ready' AND level = ?
               AND id NOT IN (SELECT set_id FROM answers) GROUP BY category""", (level,)):
        if r["c"] in cats:
            cats[r["c"]]["unseen"] = r["n"]
    for r in conn.execute(
            """SELECT category c, SUM(count - made) n FROM jobs WHERE status IN ('pending', 'running')
               AND level = ? GROUP BY category""", (level,)):
        if r["c"] in cats:
            cats[r["c"]]["generating"] = r["n"]
    for r in conn.execute("SELECT category c, COUNT(*) n FROM sets WHERE status = 'needs_audio' AND level = ? "
                          "GROUP BY category", (level,)):
        if r["c"] in cats:
            cats[r["c"]]["generating"] += r["n"]
    today = datetime.now().strftime("%Y-%m-%d")
    t = conn.execute("""SELECT COUNT(*) n, SUM(a.correct AND NOT a.unsure) ok FROM answers a
                        JOIN sets s ON s.id = a.set_id WHERE a.answered_at >= ? AND s.level = ?""",
                     (today, level)).fetchone()
    mistakes = [dict(r) for r in conn.execute(
        """SELECT s.id, s.category, s.subtype, s.target, a.correct, a.unsure, a.answered_at
           FROM answers a JOIN sets s ON s.id = a.set_id
           WHERE (NOT a.correct OR a.unsure) AND s.level = ? ORDER BY a.id DESC LIMIT 25""", (level,))]
    # Same order the worker uses: running first, then mock/requests, then the current level, oldest first.
    queue = [dict(r) for r in conn.execute(
        """SELECT * FROM jobs WHERE status IN ('pending', 'running')
           ORDER BY status = 'running' DESC, origin NOT IN ('request', 'mock'), level IS NOT ?, id""", (level,))]
    recent = [dict(r) for r in conn.execute(
        "SELECT * FROM jobs WHERE status NOT IN ('pending', 'running') ORDER BY id DESC LIMIT 8")]
    audio_waiting = conn.execute("SELECT COUNT(*) FROM sets WHERE status = 'needs_audio'").fetchone()[0]
    meta = {r["key"]: r["value"] for r in conn.execute("SELECT * FROM meta")}
    state = review_state(conn, level)
    return {"level": level, "levels": levels.LEVELS,
            "categories": cats, "today": {"answered": t["n"], "correct": t["ok"] or 0},
            "reviews_due": sum(1 for d, _, _ in state.values() if d), "reviews_waiting": len(state),
            "mistakes": mistakes, "queue": queue, "recent_jobs": recent, "audio_waiting": audio_waiting,
            "flagged": conn.execute("SELECT COUNT(*) FROM sets WHERE status = 'flagged' AND level = ?",
                                    (level,)).fetchone()[0],
            "worker_seen": meta.get("worker_seen"), "worker_note": meta.get("worker_note"),
            "auto_paused": meta.get("auto_paused") or "", "settings": store.get_settings(conn),
            "worker_warning": meta.get("worker_warning"),
            "engine": meta.get("engine"), "usage": json.loads(meta.get("usage") or "{}")}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def send_json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def read_json(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def do_GET(self):
        url = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        if url.path in ("/", "/index.html"):
            body = (HERE / "index.html").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif url.path.startswith("/static/") or url.path == "/favicon.ico":
            self.send_static("icon-32.png" if url.path == "/favicon.ico" else url.path[len("/static/"):])
        elif url.path.startswith("/audio/"):
            self.send_audio(url.path[len("/audio/"):])
        elif url.path == "/api/next":
            mode = q.get("mode", "mix")
            if mode not in CATEGORIES + ["mix", "review"]:
                return self.send_json({"error": "bad mode"}, 400)
            exclude = {int(x) for x in q.get("exclude", "").split(",") if x.isdigit()}
            with db() as conn:
                level = store.current_level(conn)
                if not level:
                    return self.send_json({"set": None, "need_level": True})
                touch(conn)
                s, msg = pick_next(conn, level, mode, exclude)
            self.send_json({"set": s, "message": msg})
        elif url.path == "/api/mock":
            with db() as conn:
                level = store.current_level(conn)
                if not level:
                    return self.send_json({"mock": None, "need_level": True})
                touch(conn)
                self.send_json(mock_view(conn, level))
        elif url.path == "/api/mock/result":
            with db() as conn:
                r = mock_result(conn, int(q.get("id", 0)))
            self.send_json(r or {"error": "not found"}, 200 if r else 404)
        elif url.path == "/api/profile":
            with db() as conn:
                r = conn.execute("SELECT value FROM meta WHERE key = 'learner_profile'").fetchone()
            self.send_json({"text": r[0] if r else ""})
        elif url.path == "/api/ask":
            # One question to Claude (polled by the page until it is answered).
            with db() as conn:
                r = conn.execute("SELECT id, status, answer, error FROM asks WHERE id = ?",
                                 (int(q.get("id", 0) or 0),)).fetchone()
            self.send_json(dict(r) if r else {"error": "not found"}, 200 if r else 404)
        elif url.path == "/api/asks":
            # Earlier questions about this question, oldest first.
            with db() as conn:
                rows = conn.execute("""SELECT id, question, answer, status, error FROM asks WHERE set_id = ? AND item = ?
                                       ORDER BY id""", (int(q.get("set_id", 0) or 0), int(q.get("item", 0) or 0)))
                self.send_json({"asks": [dict(r) for r in rows]})
        elif url.path == "/api/settings":
            with db() as conn:
                self.send_json(store.get_settings(conn))
        elif url.path == "/api/stats":
            with db() as conn:
                self.send_json(stats(conn, store.current_level(conn)))
        else:
            self.send_json({"error": "not found"}, 404)

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            body = self.read_json()
        except ValueError:
            return self.send_json({"error": "bad json"}, 400)
        with db() as conn:
            level = store.current_level(conn)
            if path in ("/api/mock/start", "/api/generate") and not level:
                return self.send_json({"error": "Choose your JLPT level first."}, 409)
            if path == "/api/answer":
                row = conn.execute("SELECT data FROM sets WHERE id = ?", (body.get("set_id"),)).fetchone()
                if not row:
                    return self.send_json({"error": "no such set"}, 404)
                items = json.loads(row["data"])["items"]
                i = int(body.get("item", 0))
                if not 0 <= i < len(items):
                    return self.send_json({"error": "bad item"}, 400)
                chosen = body.get("chosen")
                correct = int(chosen == items[i]["answer"])
                cur = conn.execute("INSERT INTO answers (set_id, item, chosen, correct, unsure, mock_id) "
                                   "VALUES (?,?,?,?,?,?)", (body["set_id"], i, chosen, correct,
                                                            int(bool(body.get("unsure"))), body.get("mock_id")))
                touch(conn)
                self.send_json({"correct": bool(correct), "answer": items[i]["answer"], "answer_id": cur.lastrowid})
            elif path == "/api/undo":
                # Mis-tap: remove the answer just given (only the latest one, and only for a few minutes).
                n = conn.execute("""DELETE FROM answers WHERE id = ? AND id = (SELECT MAX(id) FROM answers)
                                    AND answered_at >= datetime('now', 'localtime', '-10 minutes')""",
                                 (body.get("answer_id"),)).rowcount
                self.send_json({"ok": bool(n)})
            elif path == "/api/level":
                new = str(body.get("level", "")).upper()
                if new not in levels.LEVELS:
                    return self.send_json({"error": "bad level"}, 400)
                if active_mock(conn):
                    return self.send_json({"error": "Finish or abandon the running mock exam first."}, 409)
                conn.execute("INSERT OR REPLACE INTO meta VALUES ('level', ?)", (new,))
                # Automatic top-ups for the previous level are no longer wanted (requests still run).
                conn.execute("""UPDATE jobs SET status = 'cancelled', finished_at = datetime('now', 'localtime')
                                WHERE origin = 'auto' AND status = 'pending' AND level != ?""", (new,))
                touch(conn)
                self.send_json({"ok": True, "level": new})
            elif path == "/api/mock/start":
                if active_mock(conn):
                    return self.send_json({"error": "a mock is already running"}, 409)
                if body.get("size") not in ("half", "full"):
                    return self.send_json({"error": "bad size"}, 400)
                start_mock(conn, level, body["size"])
                touch(conn)
                self.send_json({"ok": True})
            elif path in ("/api/mock/finish", "/api/mock/abandon"):
                mock = active_mock(conn)
                if not mock:
                    return self.send_json({"error": "no active mock"}, 404)
                if path.endswith("abandon") and body.get("discard"):
                    discard_mock(conn, mock)
                else:
                    end_mock(conn, mock, "done" if path.endswith("finish") else "abandoned")
                self.send_json({"ok": True, "id": mock["id"]})
            elif path == "/api/mock/delete":
                mock = conn.execute("SELECT * FROM mocks WHERE id = ? AND status != 'active'",
                                    (body.get("id"),)).fetchone()
                if not mock:
                    return self.send_json({"error": "no such finished mock"}, 404)
                discard_mock(conn, mock)
                self.send_json({"ok": True})
            elif path == "/api/profile":
                text = str(body.get("text", "")).strip()
                if len(text) > PROFILE_MAX:
                    return self.send_json({"error": f"Too long (max {PROFILE_MAX} characters)."}, 400)
                conn.execute("INSERT OR REPLACE INTO meta VALUES ('learner_profile', ?)", (text,))
                self.send_json({"ok": True})
            elif path == "/api/settings":
                try:
                    self.send_json(store.save_settings(conn, body))
                except ValueError as exc:
                    return self.send_json({"error": str(exc)}, 400)
            elif path == "/api/ask":
                text = str(body.get("question", "")).strip()
                if not text:
                    return self.send_json({"error": "Write your question first."}, 400)
                if len(text) > ASK_MAX:
                    return self.send_json({"error": f"Too long (max {ASK_MAX} characters)."}, 400)
                row = conn.execute("SELECT data FROM sets WHERE id = ?", (body.get("set_id"),)).fetchone()
                item = int(body.get("item", 0) or 0)
                if not row or not 0 <= item < len(json.loads(row["data"])["items"]):
                    return self.send_json({"error": "no such question"}, 404)
                if conn.execute("SELECT COUNT(*) FROM asks WHERE status IN ('pending', 'running')").fetchone()[0] >= 3:
                    return self.send_json({"error": "Claude is still answering your earlier questions."}, 429)
                chosen = body.get("chosen")
                cur = conn.execute("INSERT INTO asks (set_id, item, chosen, question) VALUES (?, ?, ?, ?)",
                                   (body["set_id"], item, chosen if isinstance(chosen, int) else None, text))
                touch(conn)
                self.send_json({"id": cur.lastrowid})
            elif path == "/api/review/drop":
                # "Don't review again": the set leaves the review cycle for good (its answers stay in the stats).
                n = conn.execute("UPDATE sets SET no_review = 1 WHERE id = ?", (body.get("set_id"),)).rowcount
                self.send_json({"ok": bool(n)})
            elif path == "/api/flag":
                conn.execute("UPDATE sets SET status = 'flagged', flag_note = ? WHERE id = ?",
                             (str(body.get("note", ""))[:500], body.get("set_id")))
                self.send_json({"ok": True})
            elif path == "/api/generate":
                cat = body.get("category")
                cats = CATEGORIES if cat == "mix" else [cat]
                if cat != "mix" and cat not in CATEGORIES:
                    return self.send_json({"error": "bad category"}, 400)
                count = max(1, min(30, int(body.get("count", 10))))
                focus = str(body.get("focus") or "").strip()[:500] or None
                gen_level = str(body.get("level") or level).upper()   # may differ from the level being studied
                if gen_level not in levels.LEVELS:
                    return self.send_json({"error": "bad level"}, 400)
                per = max(1, round(count / len(cats)))
                for c in cats:
                    conn.execute("INSERT INTO jobs (level, category, count, focus, origin) "
                                 "VALUES (?, ?, ?, ?, 'request')", (gen_level, c, per, focus))
                touch(conn)
                self.send_json({"ok": True})
            else:
                self.send_json({"error": "not found"}, 404)

    STATIC_TYPES = {".png": "image/png", ".svg": "image/svg+xml", ".webmanifest": "application/manifest+json"}

    def send_static(self, name):
        path = HERE / "static" / name
        ctype = self.STATIC_TYPES.get(path.suffix)
        if not re.fullmatch(r"[\w.-]+", name) or not ctype or not path.is_file():
            return self.send_json({"error": "not found"}, 404)
        body = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "max-age=86400")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_audio(self, name):
        # iOS Safari only plays media served with Range support (206).
        if not re.fullmatch(r"[\w.-]+\.mp3", name) or not (AUDIO_DIR / name).is_file():
            return self.send_json({"error": "not found"}, 404)
        data = (AUDIO_DIR / name).read_bytes()
        size, start, end = len(data), 0, len(data) - 1
        m = re.fullmatch(r"bytes=(\d*)-(\d*)", self.headers.get("Range", ""))
        if m and (m[1] or m[2]):
            if m[1]:
                start = int(m[1])
                end = min(int(m[2]), size - 1) if m[2] else size - 1
            else:
                start = max(0, size - int(m[2]))
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        else:
            self.send_response(200)
        self.send_header("Content-Type", "audio/mpeg")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Cache-Control", "max-age=31536000, immutable")
        self.send_header("Content-Length", str(end - start + 1))
        self.end_headers()
        self.wfile.write(data[start:end + 1])


if __name__ == "__main__":
    store.init(DB_PATH)
    print(f"JStudy on :{PORT}")
    ThreadingHTTPServer(("", PORT), Handler).serve_forever()
