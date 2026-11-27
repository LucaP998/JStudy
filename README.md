# JStudy

A self-hosted study app for the **JLPT (N5–N1)** that never runs out of questions: Claude writes
new exam-style questions while you study, adapts them to your mistakes, and VOICEVOX turns the
listening scripts into audio. Phone-first web UI with big buttons, made to be used from your
couch over your home network.

> Questions are written by an AI. They are usually good, but they can contain mistakes: use the
> **Report a problem** link on any question that looks wrong. JStudy is not affiliated with the
> Japan Foundation or JEES, the organisers of the JLPT.

## Features

- **Every JLPT question type** except the picture-based listening task (発話表現), for all levels:
  語彙 (漢字読み, 表記, 語形成, 文脈規定, 言い換え類義, 用法), 文法 (文法形式の判断, 文の組み立て, 文章の文法),
  読解 (短文, 中文, 長文, 統合理解, 主張理解, 情報検索) and 聴解 (課題理解, ポイント理解, 概要理解, 即時応答,
  統合理解), each level with its own real set of types.
- **Endless, adaptive questions.** While you study, a background worker keeps every section stocked
  with unseen questions. Each batch sees your latest mistakes, so new questions practise exactly
  what you got wrong, in new sentences. "Generate more" lets you ask for extra questions with a
  focus of your own ("onomatopoeia for feelings", "only 即時応答", "〜ばかり vs 〜だけ").
- **Listening with real audio.** Dialogues are voiced by VOICEVOX with a narrator and different
  male/female voices; playback speed 0.8×–1.15×; full transcript after answering.
- **Review of mistakes.** Wrong or 🤔 "not sure" answers come back after 10 minutes, 1 day and
  3 days (1 to 5 steps of your choice in Settings). In a reading text or listening set, only the
  questions you missed come back; *Don't review again* drops a question from your reviews.
- **Mock exams.** Half or full mock in the real structure of your level, no feedback until the
  end, then a score per scored section with the official pass marks and a review of every mistake.
- **Explanations in English** for every question, with readings, why the distractors are wrong
  and mnemonics for look-alike words.
- **Ask Claude** after answering: type a follow-up question ("why not option 2?", "can I also
  say …?") and Claude answers knowing the question, the passage or audio script, the right answer
  and the one you picked.
- **Furigana** off, on tap, or always on; stats per section, undo for mis-taps, installable on the phone
  home screen.
- **Claude usage meter**: your subscription's 5-hour and weekly usage in the header, and automatic
  generation pauses before it eats your whole 5-hour or weekly limit (limits of your choice).

## How it works

```
 phone / browser ──► jstudy-web (Docker, port 8097) ──► data/jstudy.db  ◄── worker (host)
                                                        data/audio/*.mp3       │
                                                                               ├─► claude -p   (writes questions)
                                                                               └─► VOICEVOX    (Docker, started on demand)
```

- `jstudy-web` (Python standard library, no dependencies) serves the UI and a small JSON API.
- The **worker** runs on the host, not in Docker, because it uses the Claude Code CLI with *your*
  login. It runs Claude with no tools, no MCP servers and no skills, from an empty directory: Claude
  can only read the prompt and return questions.
- **VOICEVOX** (CPU engine) is started by the worker only when there is audio to record and stopped
  after 10 idle minutes. It uses about 1–1.5 GB of RAM while running.

## Requirements

- A Linux machine that stays on (a home server, a NAS, a spare PC). Other systems may work but are
  untested.
- **Docker** with the **compose** plugin, usable by your user (member of the `docker` group).
- **Python 3.9+** and **ffmpeg** on the host.
- **[Claude Code](https://claude.com/claude-code)** installed and logged in on the host
  (run `claude` once). A Claude **Pro or Max subscription** is recommended: questions then count
  against your subscription's usage limits. With an **API key**, every question is billed per token;
  the app shows a warning in that case.
- About 6 GB of disk for the VOICEVOX image, a few MB per week for your data.

## Installation

```sh
git clone https://github.com/LucaP998/JStudy.git
cd JStudy
./scripts/setup.sh
```

The setup script checks the requirements and your Claude login, creates `.env` from
`.env.example`, starts the web app, downloads the VOICEVOX image (~4.5 GB) and installs the worker
as a systemd user service (`jstudy-worker`). It is safe to run again.

Then open `http://<your-machine>:8097` and choose your JLPT level. The first questions appear
within a minute or two. You can switch level any time with the level badge next to the title.

Two optional steps:

- **Keep the worker running when you are logged out:** `sudo loginctl enable-linger $USER`.
- **Tell Claude about yourself:** on the *Profile* page (*About you*), describe your background, strengths
  and weak points. It is added to every request.

On a phone, open the page and use *Add to Home Screen* (Safari) or *Install app* (Chrome) for a
full-screen app with its own icon.

## Configuration

**In the app** (☰ → *Settings* → *Preferences*): the Claude model, the 5-hour and weekly usage limits that
pause automatic questions (85% and 90% by default), the review steps, how many questions are kept
ready, and whether Claude writes them only while you study or anytime.

**Machine settings** live in `.env` (created by the setup script; see `.env.example` for comments).
After a change, run `docker compose up -d` and `systemctl --user restart jstudy-worker`.

| Setting | Default | Meaning |
|---|---|---|
| `JSTUDY_PORT` | `8097` | Port of the web app |
| `JSTUDY_BIND` | `0.0.0.0` | Address it listens on (`127.0.0.1` = this machine only) |
| `JSTUDY_UID` / `JSTUDY_GID` | your ids | Owner of the files in `data/` |
| `JSTUDY_CLAUDE_BIN` | found on `PATH` | Path to the `claude` CLI |
| `JSTUDY_VOICEVOX_URL` | `http://127.0.0.1:50021` | VOICEVOX engine to use |
| `JSTUDY_MANAGE_VOICEVOX` | `1` | `0` = use an engine you run yourself, don't start/stop it |

**Model choice** (in Settings). Opus writes the most reliable questions (correct answer keys, natural Japanese,
good distractors) and is recommended. Sonnet and Haiku use less of your usage limits but make more
mistakes, especially in explanations.

## Using it

- **Study:** *Mixed* draws from every section (more from the ones you get wrong), or pick one
  section. Tap 🤔 *not sure* before answering when you are guessing: unsure answers are reviewed
  like wrong ones. Keyboard: `1`–`4` answer, `Enter` next, `u` undo, `p` play audio.
- **Review:** only the questions due for review.
- **📝 Mock exam:** a half or full exam for your level. *Stop mock* lets you discard it (as if you never
  started) or keep your answers; a finished mock can be deleted from its results page.
- **Level badge** (next to the title): switch between N5 and N1.
- **☰ menu** (top right) switches between *Study*, *Profile* (today's numbers, accuracy per
  section, recent mistakes, *About you*) and *Settings* (Claude usage, *Generate more*, the
  generation queue, *Preferences*).

Each level keeps its own questions, statistics, reviews and mock exams; switching level never
deletes anything.

## Privacy and security

- **The web app has no login.** Anyone who can open it can generate questions with your Claude
  account. Keep it on your home network or a VPN such as Tailscale; do not expose it to the internet.
- Everything is stored locally in `data/`. What leaves your machine: the prompts sent to Claude
  (instructions, your level, your recent mistakes, your *About you* profile) and nothing else.
- `.env` and `data/` (which holds your profile and answers) are listed in `.gitignore`.

## Maintenance

```sh
journalctl --user -u jstudy-worker -f          # worker log
systemctl --user restart jstudy-worker         # restart the worker
docker compose logs web                        # web app log
sqlite3 data/jstudy.db ".backup backup.db"     # backup (data/audio can be regenerated)
git pull && docker compose up -d && systemctl --user restart jstudy-worker   # update
```

Troubleshooting:

- **"Worker offline"** on the Settings page: check the worker log; usually Claude Code is logged out
  (run `claude` and log in again).
- **Listening questions never arrive:** check that VOICEVOX starts with
  `docker compose --profile tts up voicevox` and look for errors.
- **Reported questions:**
  `sqlite3 data/jstudy.db "select id, level, subtype, flag_note from sets where status = 'flagged'"`.

## Project layout

```
app/server.py               web server and JSON API (stdlib only)
app/index.html              the whole UI (one file, no build step)
app/levels.py               JLPT structure per level: question types, mock plans, pass marks, scope
app/store.py                database setup and upgrades (shared with the worker)
app/schema.sql              SQLite schema
app/static/                 icons and web app manifest
worker/worker.py            question generation (Claude), top-up, audio (VOICEVOX + ffmpeg)
worker/prompt.md            rules Claude follows for every question
scripts/setup.sh            installer
compose.yaml                web app + VOICEVOX
```

Questions are stored as JSON in `sets.data`:
`{passage?, script?: [{speaker: N|M|F, text}], items: [{question, options, answer, explanation, audio_options?}]}`.
Text uses `[漢字|かんじ]` for furigana and `【 】` for the underlined part of a question.

## Credits

- Questions written by [Claude](https://www.anthropic.com/claude) (Anthropic).
- Speech synthesis: [VOICEVOX](https://voicevox.hiroshiba.jp/) — VOICEVOX:No.7, VOICEVOX:青山龍星,
  VOICEVOX:玄野武宏, VOICEVOX:剣崎雌雄, VOICEVOX:冥鳴ひまり, VOICEVOX:もち子さん, VOICEVOX:雨晴はう.
  Each voice has its own terms of use; check them before publishing any generated audio.
- App icon drawn with the [Yuji Syuku](https://fonts.google.com/specimen/Yuji+Syuku) font
  (SIL Open Font License).

## License

MIT, see [LICENSE](LICENSE). VOICEVOX voices and the Yuji Syuku font keep their own licenses.
