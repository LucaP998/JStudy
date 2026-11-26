#!/usr/bin/env bash
# JStudy setup: checks requirements, creates .env, starts the web app, pulls the
# VOICEVOX image and installs the worker as a systemd user service.
# Safe to run again (it keeps an existing .env and learner.md).
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

ok()   { printf '  \033[32m✓\033[0m %s\n' "$*"; }
warn() { printf '  \033[33m!\033[0m %s\n' "$*"; }
die()  { printf '  \033[31m✗\033[0m %s\n' "$*"; exit 1; }

echo "Checking requirements"
command -v docker >/dev/null || die "docker is not installed (https://docs.docker.com/engine/install/)"
docker compose version >/dev/null 2>&1 || die "the docker compose plugin is missing"
docker info >/dev/null 2>&1 || die "your user cannot use docker: add it to the docker group (sudo usermod -aG docker \$USER, then log in again)"
ok "docker and docker compose"
command -v python3 >/dev/null || die "python3 is not installed"
python3 -c 'import sys; sys.exit(sys.version_info < (3, 9))' || die "python3 3.9 or newer is needed"
ok "python3 $(python3 -c 'import platform; print(platform.python_version())')"
command -v ffmpeg >/dev/null || die "ffmpeg is not installed (e.g. sudo apt install ffmpeg)"
ok "ffmpeg"

CLAUDE_BIN="$(command -v claude || true)"
[ -z "$CLAUDE_BIN" ] && [ -x "$HOME/.local/bin/claude" ] && CLAUDE_BIN="$HOME/.local/bin/claude"
[ -n "$CLAUDE_BIN" ] || die "Claude Code is not installed (https://claude.com/claude-code), then run 'claude' once and log in"
AUTH="$("$CLAUDE_BIN" auth status 2>/dev/null || true)"
if echo "$AUTH" | grep -q '"loggedIn": *true'; then
  if echo "$AUTH" | grep -q '"authMethod": *"claude.ai"' && [ -z "${ANTHROPIC_API_KEY:-}" ]; then
    ok "Claude Code logged in with a Claude subscription (questions use its usage limits)"
  else
    warn "Claude Code uses an API key: every generated question is billed per token."
  fi
else
  die "Claude Code is not logged in: run '$CLAUDE_BIN' once and log in, then run this script again"
fi

echo "Settings"
if [ ! -f .env ]; then
  sed -e "s/^JSTUDY_UID=.*/JSTUDY_UID=$(id -u)/" -e "s/^JSTUDY_GID=.*/JSTUDY_GID=$(id -g)/" .env.example > .env
  ok "created .env (edit it to change port, level or model)"
else
  ok ".env already exists, kept"
fi
mkdir -p data/audio
[ -f worker/learner.md ] && ok "worker/learner.md found" \
  || warn "optional: copy worker/learner.example.md to worker/learner.md and describe your strengths and weaknesses"

echo "Web app"
docker compose up -d
ok "running on port $(grep -E '^JSTUDY_PORT=' .env | cut -d= -f2)"

echo "VOICEVOX (listening audio, ~4.5 GB download the first time)"
docker compose --profile tts pull voicevox
ok "image ready (the worker starts it only when needed)"

echo "Worker"
if command -v systemctl >/dev/null && systemctl --user show-environment >/dev/null 2>&1; then
  UNIT_DIR="$HOME/.config/systemd/user"
  mkdir -p "$UNIT_DIR"
  cat > "$UNIT_DIR/jstudy-worker.service" <<UNIT
[Unit]
Description=JStudy worker (Claude question writer + VOICEVOX audio)
After=network-online.target

[Service]
ExecStart=$(command -v python3) -u $ROOT/worker/worker.py
WorkingDirectory=$ROOT
Environment=PATH=$(dirname "$CLAUDE_BIN"):$PATH
Restart=always
RestartSec=30

[Install]
WantedBy=default.target
UNIT
  systemctl --user daemon-reload
  systemctl --user enable --now jstudy-worker >/dev/null
  systemctl --user restart jstudy-worker
  ok "systemd user service jstudy-worker running (logs: journalctl --user -u jstudy-worker -f)"
  if [ "$(loginctl show-user "$USER" -p Linger --value 2>/dev/null)" != "yes" ]; then
    warn "to keep the worker running when you are logged out, run: sudo loginctl enable-linger $USER"
  fi
else
  warn "no systemd user session: start the worker yourself and keep it running:"
  echo "      python3 $ROOT/worker/worker.py"
fi

echo
echo "Done. Open http://$(hostname):$(grep -E '^JSTUDY_PORT=' .env | cut -d= -f2) (or http://localhost:… on this machine)."
