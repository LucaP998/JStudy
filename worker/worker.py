#!/usr/bin/env python3
"""JStudy background worker (runs on the host, e.g. as a systemd user service,
because headless Claude needs the Claude Code login of the person running it).

Loop, every few seconds:
  1. Mock exam jobs and "Generate more" requests run first.
  2. While the student is studying (UI used in the last ACTIVE_WINDOW), or at
     any time if Settings say so, each category of the current JLPT level is
     topped up so there are always unseen questions. Every batch sees the student's recent mistakes, so new
     questions target exactly what went wrong.
  0. "Ask Claude" questions about an answered question are answered at once,
     in their own thread (they don't wait behind a batch of new questions).
  3. Listening sets get their audio from VOICEVOX (started on demand with
     docker compose, stopped again after ENGINE_IDLE of inactivity).

Claude runs with no tools, no MCP, no skills, from an empty directory. With a
Claude subscription login the calls count against that subscription's usage
limits; with an API key they are billed per token (the app shows a warning).

Machine settings come from the environment or the repo's .env file (see
.env.example); study settings (model, pause limits, ...) are edited in the app.

  worker.py            run forever (the service)
  worker.py --once     one pass, then exit (testing)
"""
import io
import json
import os
import random
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
import wave
from datetime import datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "app"))
import levels  # noqa: E402
import store   # noqa: E402


def load_env(path):
    """Minimal .env reader (KEY=value lines); real environment variables win."""
    if not path.is_file():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.split(" #")[0].strip().strip('"').strip("'"))


load_env(ROOT / ".env")

DB_PATH = Path(os.environ.get("JSTUDY_DB", ROOT / "data" / "jstudy.db"))
AUDIO_DIR = DB_PATH.parent / "audio"
COMPOSE = ROOT / "compose.yaml"
CLAUDE_CWD = HERE / "claude_cwd"     # empty dir: no project files/instructions loaded
CLAUDE = (os.environ.get("JSTUDY_CLAUDE_BIN") or shutil.which("claude")
          or str(Path.home() / ".local" / "bin" / "claude"))
DOCKER = shutil.which("docker") or "docker"
FFMPEG = shutil.which("ffmpeg") or "ffmpeg"
VOICEVOX = os.environ.get("JSTUDY_VOICEVOX_URL", "http://127.0.0.1:50021").rstrip("/")
MANAGE_VOICEVOX = os.environ.get("JSTUDY_MANAGE_VOICEVOX", "1") != "0"   # start/stop it with docker compose

CLAUDE_TIMEOUT = 600
CLAUDE_LOCKDOWN = [
    "--tools", "", "--strict-mcp-config", "--disable-slash-commands",
    "--disallowedTools", "Bash,Edit,Write,Read,NotebookEdit,WebFetch,WebSearch,Agent,Skill",
    "--permission-mode", "default", "--no-session-persistence",
]

POLL = 5                                  # seconds between passes
ACTIVE_WINDOW = timedelta(minutes=45)     # "studying" = UI used within this window
ENGINE_IDLE = timedelta(minutes=10)       # stop VOICEVOX after this long unused
AUTH_CHECK_EVERY = 3600                   # seconds between Claude login checks
# Per category: sets per Claude call, and auto top-up (refill to `fill` when
# fewer than `low` unseen + in-progress sets remain).
BATCH = {"vocab": 6, "grammar": 6, "order": 5, "textgrammar": 1, "reading": 2, "listening": 4}
TOPUP = {"vocab": (8, 14), "grammar": (5, 10), "order": (4, 8), "textgrammar": (1, 2),
         "reading": (2, 4), "listening": (5, 10)}
USAGE_MAX_AGE = timedelta(minutes=10)     # refresh the usage reading this often while studying
RECENT_MISTAKES = 8                       # mistakes shown to Claude per batch (distinct targets)
REPRACTICE = 2                            # newer sets per missed target before it stops being re-practised
AVOID_TARGETS = 150                       # recent targets Claude should not repeat

# VOICEVOX voices (speaker style ids). Narrator: No.7 アナウンス.
NARRATOR = 30
MALE = [13, 11, 21]       # 青山龍星, 玄野武宏, 剣崎雌雄
FEMALE = [14, 20, 10]     # 冥鳴ひまり, もち子さん, 雨晴はう
PAUSE_LINE, PAUSE_BLOCK = 0.6, 1.4        # seconds of silence between lines / sections

# Format rules per category (the same at every level; the level's subtypes,
# lengths and shares are added from app/levels.py).
CATEGORY_RULES = {
    "vocab": """Category: 語彙 (vocabulary). Each set has exactly one item. Subtypes (use the exact name as the
subtype field):""",
    "grammar": """Category: 文法 (grammar form). Each set has exactly one item. Cover a wide range of the level's
grammar points. Subtypes (exact name as the subtype field):""",
    "order": """Category: 文の組み立て (sentence ordering). Each set has exactly one item.
The question is a sentence with four blanks written ＿＿＿ and one of them marked ★,
for example: 「この[店|みせ]は ＿＿＿ ＿＿＿ ★ ＿＿＿ [人気|にんき]がある。」 (the ★ may be any
of the four positions). The 4 options are the four chunks (in any order); `answer` is the
chunk that goes in the ★ position. The explanation must write out the full correct sentence
(do not refer to option numbers).
ONE order only: the four chunks must fit together in exactly one natural, grammatical order, as on
the real exam. Do not use a chunk that could also sit somewhere else, such as a free-floating adverb
(実際に, もっと, ちゃんと, ずっと…) or a time/place phrase that could modify more than one word:
「[料理|りょうり]が / [実際|じっさい]に / おいしい / かどうかは」 fails, because 実際に can go before おいしい or
before the verb that follows. Tie each chunk grammatically to its neighbours (particles, conjugated
endings, set patterns). Before finishing, try the other orders of the four chunks: if any other order
is also acceptable Japanese, or puts a different chunk in the ★ position, rewrite the item.
Subtypes (exact name as the subtype field):""",
    "textgrammar": """Category: 文章の文法 (text grammar). One set = a passage (a student's essay, a letter,
a blog post) put in `passage`, with blanks marked （1）（2）… in the text. One item per blank, in
order; each item's `question` is just the blank label, e.g. 「（1）」. Test connectives (しかし,
それで, つまり), demonstratives (この/その/あの/そういう), sentence-final forms and grammar in
context. Subtypes (exact name as the subtype field):""",
    "reading": """Category: 読解 (reading). Use \\n line breaks inside `passage`. Use realistic traps
(e.g. 〜そうになった = almost happened, negatives, conditions that only apply to some people).
Subtypes (exact name as the subtype field):""",
    "listening": """Category: 聴解 (listening). The student hears `script` as audio (VOICEVOX TTS) and does NOT
see it until after answering. Script lines: speaker "N" = exam narrator, "M" = man, "F" = woman.
Natural spoken Japanese for the level (casual forms between friends, polite forms at work and
shops, fillers like えっと, あのう, そうですね), realistic distractors (plans that change during
the conversation, things mentioned but not the answer). Each set has ONE item; its `question` is
the question as the narrator says it (for 即時応答: 「[最|もっと]もよい[返事|へんじ]を[選|えら]んでください。」).
Subtypes (exact name as the subtype field):""",
}

SCHEMA_JSON = {
    "type": "object",
    "properties": {"sets": {"type": "array", "items": {
        "type": "object",
        "properties": {
            "subtype": {"type": "string"},
            "target": {"type": "string"},
            "passage": {"type": "string"},
            "script": {"type": "array", "items": {
                "type": "object",
                "properties": {"speaker": {"type": "string", "enum": ["N", "M", "F"]},
                               "text": {"type": "string"}},
                "required": ["speaker", "text"]}},
            "items": {"type": "array", "minItems": 1, "items": {
                "type": "object",
                "properties": {
                    "question": {"type": "string"},
                    "options": {"type": "array", "items": {"type": "string"}, "minItems": 3, "maxItems": 4},
                    "answer": {"type": "integer"},
                    "audio_options": {"type": "boolean"},
                    "explanation": {"type": "string"}},
                "required": ["question", "options", "answer", "explanation"]}}},
        "required": ["subtype", "target", "items"]}}},
    "required": ["sets"],
}

RUBY = re.compile(r"\[([^|\]]+)\|[^\]]+\]")
TIME_FMT = "%Y-%m-%d %H:%M:%S"


def plain(text):
    return RUBY.sub(r"\1", text)


def log(msg):
    print(f"{datetime.now():%H:%M:%S} {msg}", flush=True)


def db():
    return store.connect(DB_PATH)


def set_meta(conn, key, value):
    conn.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (key, value))
    conn.commit()


def get_meta(conn, key):
    r = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return r[0] if r else None


def import_env_settings(conn):
    """Older installs set the model and the 5-hour pause in .env (JSTUDY_MODEL,
    JSTUDY_AUTO_PAUSE); the app now edits them. Carry them over once."""
    if get_meta(conn, "settings") is not None:
        return
    raw = {}
    if os.environ.get("JSTUDY_MODEL"):
        raw["model"] = os.environ["JSTUDY_MODEL"].strip().lower()
    if os.environ.get("JSTUDY_AUTO_PAUSE"):
        try:
            raw["pause_5h"] = round(float(os.environ["JSTUDY_AUTO_PAUSE"]) * 100)
        except ValueError:
            pass
    if raw:
        try:
            store.save_settings(conn, raw)
            conn.commit()
            log(f"imported settings from .env: {raw}")
        except ValueError as exc:
            log(f"ignored .env settings ({exc})")


def import_learner_file(conn):
    """Older installs kept the learner profile in worker/learner.md; the app now edits it
    (meta 'learner_profile'). Copy the file in once, if the app has no profile yet."""
    f = HERE / "learner.md"
    if get_meta(conn, "learner_profile") is None and f.is_file() and f.read_text().strip():
        text = f.read_text().strip()
        if text.startswith("# "):             # drop the template's "# About the student" title
            text = text.split("\n", 1)[-1].strip()
        set_meta(conn, "learner_profile", text)
        log("imported worker/learner.md into the app's learner profile")


# ---------------------------------------------------------------- claude

def claude_call(prompt, model, schema=None):
    """Headless Claude in stream-json mode, so the rate_limit_event (the
    subscription's 5-hour / weekly usage) comes back too. Returns the result
    message; the usage reading is saved to meta 'usage' for the dashboard."""
    cmd = [CLAUDE, "-p", "--model", model, *CLAUDE_LOCKDOWN, "--output-format", "stream-json", "--verbose"]
    if schema:
        cmd += ["--json-schema", json.dumps(schema)]
    CLAUDE_CWD.mkdir(exist_ok=True)
    proc = subprocess.run(cmd, input=prompt, capture_output=True, text=True,
                          timeout=CLAUDE_TIMEOUT, cwd=CLAUDE_CWD)
    result = None
    for line in proc.stdout.splitlines():
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        if msg.get("type") == "rate_limit_event":
            save_usage(msg.get("rate_limit_info") or {})
        elif msg.get("type") == "result":
            result = msg
    if result is None:
        raise RuntimeError(f"claude exit {proc.returncode}: {(proc.stderr or proc.stdout)[-300:]}")
    if proc.returncode != 0 or result.get("is_error"):
        raise RuntimeError(str(result.get("result") or proc.stderr)[:300])
    return result


def run_claude(prompt, model):
    result = claude_call(prompt, model, SCHEMA_JSON).get("structured_output")
    if not isinstance(result, dict) or not isinstance(result.get("sets"), list):
        raise RuntimeError("no structured output")
    return result["sets"]


def check_auth(conn):
    """Warn in the app when Claude Code is not logged in, or is logged in with
    an API key (every question is then billed per token)."""
    last = float(get_meta(conn, "auth_checked") or 0)
    if time.time() - last < AUTH_CHECK_EVERY:
        return
    set_meta(conn, "auth_checked", str(time.time()))
    warning = ""
    try:
        out = subprocess.run([CLAUDE, "auth", "status"], capture_output=True, text=True, timeout=60)
        info = json.loads(out.stdout or "{}")
        if not info.get("loggedIn"):
            warning = "Claude Code is not logged in on the server: run `claude` once and log in."
        elif info.get("authMethod") != "claude.ai" or os.environ.get("ANTHROPIC_API_KEY"):
            warning = ("Claude Code uses an API key here, so every generated question is billed per token. "
                       "Log in with a Claude subscription to use its usage limits instead.")
    except Exception as exc:  # old CLI without `auth status`, or not installed
        warning = f"Could not check the Claude login ({str(exc)[:120]})."
    if warning:
        log("warning: " + warning)
    set_meta(conn, "worker_warning", warning)


def save_usage(info):
    windows = info.get("unifiedWindows") or {}
    usage = {k: {"used": w.get("utilization"), "resets_at": w.get("resetsAt"), "status": w.get("status")}
             for k, w in windows.items()}
    if not usage and info.get("rateLimitType"):       # older event shape
        usage = {info["rateLimitType"]: {"used": info.get("utilization"), "resets_at": info.get("resetsAt"),
                                         "status": info.get("status")}}
    if usage:
        usage["checked_at"] = int(time.time())
        conn = db()
        set_meta(conn, "usage", json.dumps(usage))
        conn.close()


def usage_reading(conn):
    try:
        return json.loads(get_meta(conn, "usage") or "{}")
    except ValueError:
        return {}


def refresh_usage(conn):
    """While studying, keep the dashboard's usage reading fresh (it also moves
    with the user's other Claude sessions) with a tiny Haiku call. Only
    subscription logins report usage; with an API key this is skipped."""
    if get_meta(conn, "worker_warning"):
        return
    checked = usage_reading(conn).get("checked_at", 0)
    if time.time() - checked < USAGE_MAX_AGE.total_seconds():
        return
    try:
        claude_call("Reply with just: ok", "haiku")
    except Exception as exc:
        log(f"usage refresh failed: {exc}")
    u = usage_reading(conn)
    if time.time() - u.get("checked_at", 0) >= USAGE_MAX_AGE.total_seconds():
        u["checked_at"] = int(time.time())       # no reading came back: don't retry every pass
        set_meta(conn, "usage", json.dumps(u))


def auto_paused(conn, settings):
    """Why automatic questions are paused ("" = not paused): a usage window is above
    the limit chosen in Settings."""
    u = usage_reading(conn)
    for key, setting, name in (("five_hour", "pause_5h", "5-hour"), ("seven_day", "pause_week", "weekly")):
        limit, w = settings[setting], u.get(key) or {}
        used, resets = w.get("used"), w.get("resets_at") or 0
        if limit is not None and used is not None and used * 100 >= limit and time.time() < resets:
            at = datetime.fromtimestamp(resets).strftime("%a %H:%M" if key == "seven_day" else "%H:%M")
            return (f"Automatic questions paused: Claude {name} usage is {used:.0%}, above your "
                    f"{limit}% limit (resets {at}). Your own requests and mock exams still run.")
    return ""


# ------------------------------------------------------------- ask Claude

ASK_PROMPT = """You are a friendly, precise Japanese teacher. A student preparing for the JLPT {level} has just
answered the practice question below in a study app and asks you about it. Answer their question.

- Reply in the language the student writes in. Be concise: usually 3–8 sentences; longer only if they ask
  for a detailed explanation. Give the reading and meaning of the Japanese words you discuss.
- Plain text only: no markdown headings, bold, tables or code. Short paragraphs separated by a blank line;
  simple "- " lists are fine.
- Write furigana with the markup [漢字|かんじ], one word per bracket, okurigana outside: [食|た]べる.
- The options are numbered 1–4 in the order the student saw them; quote the option text as well.
- The question was written by an AI and can be wrong. If the student points out a problem (two correct
  options, a wrong answer key, unnatural Japanese), check it honestly. If they are right, say so plainly
  and suggest they use "Report a problem with this question".
- Stay on this question and on Japanese; politely decline anything unrelated."""


def ask_context(s, item, chosen):
    data = json.loads(s["data"])
    it = data["items"][item]
    lines = [f"Section: {s['category']} / {s['subtype']}"]
    if data.get("passage"):
        lines.append("Passage:\n" + data["passage"])
    if data.get("script"):
        who = {"N": "Narrator", "M": "Man", "F": "Woman"}
        lines.append("Listening script (the student heard this as audio):\n"
                     + "\n".join(f"{who.get(l['speaker'], l['speaker'])}: {l['text']}" for l in data["script"]))
    lines.append(f"Question: {it['question']}")
    lines.append("Options:\n" + "\n".join(f"{i + 1}. {o}" for i, o in enumerate(it["options"])))
    lines.append(f"Correct answer: {it['answer'] + 1}. {it['options'][it['answer']]}")
    if chosen is not None and 0 <= chosen < len(it["options"]):
        lines.append(f"The student chose: {chosen + 1}. {it['options'][chosen]}"
                     + (" (correct)" if chosen == it["answer"] else " (wrong)"))
    lines.append(f"Explanation the app showed: {it['explanation']}")
    return "\n\n".join(lines)


def answer_ask(conn, ask):
    s = conn.execute("SELECT * FROM sets WHERE id = ?", (ask["set_id"],)).fetchone()
    earlier = conn.execute("""SELECT question, answer FROM asks WHERE set_id = ? AND item = ? AND id < ?
                              AND status = 'done' ORDER BY id DESC LIMIT 6""",
                           (ask["set_id"], ask["item"], ask["id"])).fetchall()[::-1]
    parts = [ASK_PROMPT.replace("{level}", s["level"] or ""), "# The question\n" + ask_context(s, ask["item"], ask["chosen"])]
    if earlier:
        parts.append("# Earlier in this conversation\n" + "\n\n".join(f"Student: {e['question']}\nYou: {e['answer']}"
                                                                     for e in earlier))
    parts.append("# The student's question\n" + ask["question"])
    result = claude_call("\n\n".join(parts), store.get_settings(conn)["model"])
    text = str(result.get("result") or "").strip()
    if not text:
        raise RuntimeError("empty answer")
    return text


def ask_loop():
    """Thread: answer pending "Ask Claude" questions, oldest first."""
    conn = db()
    conn.execute("UPDATE asks SET status = 'pending' WHERE status = 'running'")   # interrupted by a restart
    conn.commit()
    while True:
        try:
            ask = conn.execute("SELECT * FROM asks WHERE status = 'pending' ORDER BY id LIMIT 1").fetchone()
            if not ask:
                time.sleep(1)
                continue
            conn.execute("UPDATE asks SET status = 'running' WHERE id = ?", (ask["id"],))
            conn.commit()
            t0 = time.time()
            try:
                text = answer_ask(conn, ask)
                conn.execute("""UPDATE asks SET status = 'done', answer = ?, answered_at = datetime('now', 'localtime')
                                WHERE id = ?""", (text, ask["id"]))
                log(f"ask {ask['id']}: answered in {time.time() - t0:.0f}s")
            except Exception as exc:
                conn.execute("UPDATE asks SET status = 'failed', error = ? WHERE id = ?", (str(exc)[:300], ask["id"]))
                log(f"ask {ask['id']}: failed ({exc})")
            conn.commit()
        except Exception as exc:
            log(f"ask loop error: {exc}")
            time.sleep(5)


# ---------------------------------------------------------------- generation

def recent_mistakes(conn, level, category):
    """Recent wrong/unsure answers whose target has not yet been re-practised
    enough: once REPRACTICE newer sets test the same target, it is left out
    (otherwise every batch keeps re-testing the same few words)."""
    rows = conn.execute(
        """SELECT s.subtype, s.target, s.data, a.item, a.chosen, a.correct, a.unsure,
                  (SELECT COUNT(*) FROM sets s2 WHERE s2.target = s.target AND s2.id > a.set_id
                   AND s2.level = s.level AND s2.status != 'retired') AS newer
           FROM answers a JOIN sets s ON s.id = a.set_id
           WHERE s.level = ? AND s.category = ? AND (NOT a.correct OR a.unsure)
           ORDER BY a.id DESC LIMIT 60""", (level, category)).fetchall()
    seen, kept = set(), []
    for r in rows:
        if r["target"] in seen or r["newer"] >= REPRACTICE:
            continue
        seen.add(r["target"])
        kept.append(r)
    out = []
    for r in kept[:RECENT_MISTAKES]:
        it = json.loads(r["data"])["items"][r["item"]]
        chosen = it["options"][r["chosen"]] if r["chosen"] is not None and r["chosen"] < len(it["options"]) else "?"
        status = "correct but unsure" if r["correct"] else f"WRONG, chose 「{plain(chosen)}」"
        out.append(f"- [{r['subtype']}] target {r['target']}: 「{plain(it['question'])}」 "
                   f"answer 「{plain(it['options'][it['answer']])}」, {status}")
    return out


def build_prompt(conn, level, category, n, focus):
    targets = [r[0] for r in conn.execute(
        "SELECT target FROM sets WHERE level = ? AND category = ? AND target IS NOT NULL ORDER BY id DESC LIMIT ?",
        (level, category, AVOID_TARGETS))]
    parts = [(HERE / "prompt.md").read_text().replace("{LEVEL}", level),
             "# Level\n" + levels.SCOPE[level],
             CATEGORY_RULES[category] + "\n" + levels.subtype_lines(level, category)]
    profile = (get_meta(conn, "learner_profile") or "").strip()
    if profile:
        parts.append("# About the student (written by the student; adapt to it, but every question must "
                     "still look like a real JLPT " + level + " question)\n" + profile)
    mistakes = recent_mistakes(conn, level, category)
    if mistakes:
        parts.append("# The student's most recent mistakes in this category (newest first)\n"
                     + "\n".join(mistakes)
                     + "\n\nAt most a quarter of the new sets (and at most ONE set per mistake) should practise "
                       "these points or the words they were confused with, in NEW sentences. Do not copy these questions.")
    if targets:
        parts.append("# Targets already used recently: do NOT use any of these as a target again (only exception: "
                     "a word from the mistakes list above, once). Never use the same target twice in one batch.\n"
                     + "、".join(dict.fromkeys(targets)))
    if focus:
        parts.append(f"# The student asked for this focus (follow it)\n{focus}")
    parts.append(f"# Now write exactly {n} new set(s) for this category.")
    return "\n\n".join(parts)


def valid_set(category, s):
    items = s.get("items") or []
    if not items:
        return False
    if category in ("textgrammar", "reading") and not (s.get("passage") or "").strip():
        return False
    if category == "listening" and not s.get("script"):
        return False
    for it in items:
        opts = it.get("options") or []
        want = 3 if s.get("subtype") == "即時応答" else 4
        if len(opts) != want or len(set(opts)) != want or not 0 <= it.get("answer", -1) < want:
            return False
    return True


def shuffle_options(s):
    """LLMs favour some answer positions; shuffle so the key is uniform."""
    for it in s["items"]:
        order = list(range(len(it["options"])))
        random.shuffle(order)
        it["answer"] = order.index(it["answer"])
        it["options"] = [it["options"][i] for i in order]
        if s.get("subtype") == "即時応答":
            it["audio_options"] = True
    return s


def run_job(conn, job):
    cat, remaining = job["category"], job["count"] - job["made"]
    level = job["level"] or store.current_level(conn) or levels.LEGACY_LEVEL
    conn.execute("UPDATE jobs SET status = 'running' WHERE id = ?", (job["id"],))
    conn.commit()
    failures = 0
    while remaining > 0:
        if job["origin"] == "auto" and conn.execute("SELECT 1 FROM mocks WHERE status = 'active'").fetchone():
            log(f"job {job['id']}: mock exam started, pausing until it ends")
            conn.execute("UPDATE jobs SET status = 'pending' WHERE id = ?", (job["id"],))
            conn.commit()
            return
        if job["origin"] == "auto" and store.current_level(conn) != level:
            log(f"job {job['id']}: level switched away from {level}, stopping")
            conn.execute("UPDATE jobs SET status = 'cancelled', finished_at = datetime('now','localtime') "
                         "WHERE id = ?", (job["id"],))
            conn.commit()
            return
        n = min(BATCH[cat], remaining)
        set_meta(conn, "worker_note", f"Claude is writing {n} {level} {cat} set(s)…")
        log(f"job {job['id']}: asking Claude for {n} {level} {cat} set(s)")
        t0 = time.time()
        try:
            sets = run_claude(build_prompt(conn, level, cat, n, job["focus"]), store.get_settings(conn)["model"])
        except Exception as exc:
            failures += 1
            log(f"job {job['id']}: Claude failed ({exc})")
            if failures >= 2:
                conn.execute("UPDATE jobs SET status = 'failed', error = ?, finished_at = datetime('now','localtime') "
                             "WHERE id = ?", (str(exc)[:500], job["id"]))
                conn.commit()
                return
            time.sleep(20)
            continue
        for s in sets:
            s["subtype"] = levels.canonical_subtype(level, cat, s.get("subtype"))
        good = [shuffle_options(s) for s in sets if valid_set(cat, s)]
        # Re-read: a mock that ended meanwhile turns its jobs into normal ones.
        origin = conn.execute("SELECT origin FROM jobs WHERE id = ?", (job["id"],)).fetchone()[0]
        for s in good[:remaining]:
            data = {k: s.get(k) for k in ("passage", "script", "items") if s.get(k)}
            conn.execute(
                "INSERT INTO sets (level, category, subtype, target, data, origin, job_id, status) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (level, cat, s["subtype"], s["target"], json.dumps(data, ensure_ascii=False), origin, job["id"],
                 "needs_audio" if cat == "listening" else "ready"))
        made = min(len(good), remaining)
        remaining -= made
        conn.execute("UPDATE jobs SET made = made + ? WHERE id = ?", (made, job["id"]))
        conn.commit()
        log(f"job {job['id']}: got {len(sets)} set(s), {len(good)} valid, {time.time() - t0:.0f}s")
        if not good:
            failures += 1
            if failures >= 2:
                break
        if cat == "listening":
            make_pending_audio(conn)      # let the first listening sets be playable early
    conn.execute("UPDATE jobs SET status = ?, finished_at = datetime('now','localtime') WHERE id = ?",
                 ("done" if remaining <= 0 else "failed", job["id"]))
    conn.commit()


def studying(conn):
    last = get_meta(conn, "last_active")
    return bool(last) and datetime.now() - datetime.strptime(last, TIME_FMT) < ACTIVE_WINDOW


def top_up_plan(conn, level, settings):
    """{category: sets to queue} to bring each category back to its stock size."""
    k, plan = store.STOCK[settings["stock"]], {}
    for cat, (low, fill) in TOPUP.items():
        low, fill = max(1, round(low * k)), max(1, round(fill * k))
        unseen = conn.execute(
            """SELECT COUNT(*) FROM sets WHERE level = ? AND category = ? AND status IN ('ready', 'needs_audio')
               AND id NOT IN (SELECT set_id FROM answers)""", (level, cat)).fetchone()[0]
        coming = conn.execute("SELECT COALESCE(SUM(count - made), 0) FROM jobs WHERE level = ? AND category = ? "
                              "AND status IN ('pending', 'running')", (level, cat)).fetchone()[0]
        if unseen + coming < low:
            plan[cat] = fill - unseen - coming
    return plan


def top_up(conn, level, plan):
    for cat, n in plan.items():
        conn.execute("INSERT INTO jobs (level, category, count, origin) VALUES (?, ?, ?, 'auto')", (level, cat, n))
        conn.commit()
        log(f"top-up: queued {n} {level} {cat}")


# --------------------------------------------------------------------- audio

def compose(*args):
    subprocess.run([DOCKER, "compose", "-f", str(COMPOSE), "--profile", "tts", *args],
                   check=True, capture_output=True, timeout=300, cwd=ROOT)


def engine_up():
    try:
        urllib.request.urlopen(VOICEVOX + "/version", timeout=3).read()
        return True
    except OSError:
        return False


def ensure_engine(conn):
    if engine_up():
        return
    if not MANAGE_VOICEVOX:
        raise RuntimeError(f"VOICEVOX is not reachable at {VOICEVOX}")
    log("starting VOICEVOX")
    set_meta(conn, "worker_note", "Starting the voice engine…")
    compose("up", "-d", "voicevox")
    for _ in range(90):
        if engine_up():
            set_meta(conn, "engine", "running")
            return
        time.sleep(2)
    raise RuntimeError("VOICEVOX did not start")


def synth(text, speaker):
    q = urllib.parse.urlencode({"text": text, "speaker": speaker})
    req = urllib.request.Request(f"{VOICEVOX}/audio_query?{q}", method="POST")
    query = urllib.request.urlopen(req, timeout=120).read()
    req = urllib.request.Request(f"{VOICEVOX}/synthesis?speaker={speaker}", data=query, method="POST",
                                 headers={"Content-Type": "application/json"})
    with wave.open(io.BytesIO(urllib.request.urlopen(req, timeout=300).read())) as w:
        return w.getparams(), w.readframes(w.getnframes())


NUMBERS = ["いち", "に", "さん", "よん"]


def audio_segments(s):
    """[(speaker_id, text, pause_after)] for one listening set."""
    male, female = random.choice(MALE), random.choice(FEMALE)
    voice = {"N": NARRATOR, "M": male, "F": female}
    segs, script = [], s["script"]
    for i, line in enumerate(script):
        nxt = script[i + 1]["speaker"] if i + 1 < len(script) else None
        pause = PAUSE_BLOCK if "N" in (line["speaker"], nxt) else PAUSE_LINE
        segs.append((voice[line["speaker"]], plain(line["text"]), pause))
    item = s["items"][0]
    if item.get("audio_options"):
        if s.get("subtype") == "即時応答":
            first = script[0]["speaker"]
            reply = female if first == "M" else male
        else:
            reply = NARRATOR
        segs[-1] = (*segs[-1][:2], PAUSE_BLOCK)
        for n, opt in zip(NUMBERS, item["options"]):
            segs.append((NARRATOR, n, 0.5))
            segs.append((reply, plain(opt), PAUSE_LINE + 0.4))
    return segs


def make_audio(set_row):
    s = {**json.loads(set_row["data"]), "subtype": set_row["subtype"]}
    params, frames = None, []
    for speaker, text, pause in audio_segments(s):
        p, f = synth(text, speaker)
        params = params or p
        frames += [f, b"\0" * int(p.framerate * p.sampwidth * p.nchannels * pause)]
    AUDIO_DIR.mkdir(parents=True, exist_ok=True)
    name = f"set{set_row['id']}.mp3"
    wav = io.BytesIO()
    with wave.open(wav, "wb") as w:
        w.setparams(params)
        w.writeframes(b"".join(frames))
    subprocess.run([FFMPEG, "-y", "-loglevel", "error", "-f", "wav", "-i", "pipe:0",
                    "-ac", "1", "-b:a", "64k", str(AUDIO_DIR / name)],
                   input=wav.getvalue(), check=True, timeout=120)
    return name


def make_pending_audio(conn):
    rows = conn.execute("SELECT * FROM sets WHERE status = 'needs_audio' ORDER BY id").fetchall()
    if not rows:
        return
    ensure_engine(conn)
    for r in rows:
        set_meta(conn, "worker_note", f"Recording listening audio ({len(rows)} left)…")
        try:
            name = make_audio(r)
            conn.execute("UPDATE sets SET audio = ?, status = 'ready' WHERE id = ?", (name, r["id"]))
            log(f"audio for set {r['id']} done")
        except Exception as exc:
            log(f"audio for set {r['id']} failed: {exc}")
            conn.execute("UPDATE sets SET status = 'audio_failed', flag_note = ? WHERE id = ?",
                         (str(exc)[:300], r["id"]))
        conn.commit()
    set_meta(conn, "engine_used", datetime.now().strftime(TIME_FMT))


def maybe_stop_engine(conn):
    used = get_meta(conn, "engine_used")
    if MANAGE_VOICEVOX and get_meta(conn, "engine") == "running" and used and \
            datetime.now() - datetime.strptime(used, TIME_FMT) > ENGINE_IDLE:
        log("stopping idle VOICEVOX")
        compose("stop", "voicevox")
        set_meta(conn, "engine", "stopped")


# ---------------------------------------------------------------------- main

def one_pass(conn):
    set_meta(conn, "worker_seen", datetime.now().strftime(TIME_FMT))
    check_auth(conn)
    paused = ""
    level = store.current_level(conn)      # None until the student picks a level in the app
    # During a mock exam only its questions (and explicit requests) are written: reserving
    # sets for the mock empties the normal pool, and refilling it can wait until the mock ends.
    in_mock = conn.execute("SELECT 1 FROM mocks WHERE status = 'active'").fetchone() is not None
    settings = store.get_settings(conn)
    if level and (studying(conn) or settings["schedule"] == "always"):
        plan = top_up_plan(conn, level, settings)
        if studying(conn) or plan:          # when away, only check usage if there is something to write
            refresh_usage(conn)
            paused = auto_paused(conn, settings)
            if plan and not paused and not in_mock:
                top_up(conn, level, plan)
    # Mock exam and "Generate more" jobs first (in order); above the usage
    # brake or during a mock only those run.
    job = conn.execute(f"""SELECT * FROM jobs WHERE status IN ('pending', 'running')
                           {"AND origin IN ('request', 'mock')" if paused or in_mock else ""}
                           ORDER BY origin NOT IN ('request', 'mock'), level IS NOT ?, id LIMIT 1""",
                       (level,)).fetchone()
    if job:
        run_job(conn, job)
    make_pending_audio(conn)
    maybe_stop_engine(conn)
    set_meta(conn, "auto_paused", paused)
    set_meta(conn, "worker_note", "")
    return bool(job)


def main():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    store.init(DB_PATH)
    conn = db()
    set_meta(conn, "auth_checked", "0")
    import_learner_file(conn)
    import_env_settings(conn)
    if "--once" not in sys.argv:
        threading.Thread(target=ask_loop, name="ask", daemon=True).start()
    log(f"worker started (model {store.get_settings(conn)['model']}, claude at {CLAUDE}, database {DB_PATH})")
    while True:
        try:
            busy = one_pass(conn)
        except Exception as exc:
            log(f"error: {exc}")
            busy = False
        if "--once" in sys.argv:
            break
        if not busy:
            time.sleep(POLL)


if __name__ == "__main__":
    main()
