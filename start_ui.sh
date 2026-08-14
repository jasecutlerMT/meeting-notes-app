#!/usr/bin/env bash
set -euo pipefail

# Meeting Recorder — launch the app (the button interface).
#
# Run this, and a page opens in your browser with a Start/Stop button.
# Leave this Terminal window open while you use the app; press Control-C to quit.

cd "$(dirname "$0")"

# ----- put things back if an update didn't finish -----------------------------
# Updates swap the app's files in place. If the Mac slept, lost power, or this window
# was closed part-way through, this puts the previous version back before anything
# starts. It runs before Python, so it works even when the app itself can't start.
UPD=".update"
restore_backup() {
  local b f
  b="$(cat "$UPD/BACKUP_DIR" 2>/dev/null || true)"
  [ -n "$b" ] && [ -f "$b/BACKUP_READY" ] || return 1
  for f in "$b"/*; do
    [ -f "$f" ] || continue
    [ "$(basename "$f")" = "BACKUP_READY" ] && continue
    cp -p "$f" "./$(basename "$f")" || return 1
  done
  chmod +x ./*.sh 2>/dev/null || true
  rm -rf __pycache__
  return 0
}

if [ -f "$UPD/IN_PROGRESS" ]; then
  if [ -f "$UPD/SWAP_STARTED" ]; then
    echo "The last update didn't finish. Putting your previous version back..."
    if restore_backup; then
      echo "Done — your previous version is back. Starting normally."
    else
      echo "Couldn't restore automatically."
      echo "Double-click 'Restore Previous Version' on your Desktop."
    fi
    echo ""
  fi
  rm -f "$UPD/IN_PROGRESS" "$UPD/SWAP_STARTED"
fi

# If a freshly-updated version can't get as far as showing you the page, it never
# clears this marker. After a couple of tries we put the previous version back, so
# "double-click the icon again" is all anyone ever has to know.
if [ -f "$UPD/PENDING_VERIFY" ]; then
  n="$(cat "$UPD/PENDING_VERIFY" 2>/dev/null || echo 0)"
  case "$n" in (*[!0-9]*|'') n=0 ;; esac
  n=$((n + 1))
  if [ "$n" -ge 3 ]; then
    echo "The updated version isn't starting properly. Putting the previous one back..."
    restore_backup || echo "Double-click 'Restore Previous Version' on your Desktop."
    rm -f "$UPD/PENDING_VERIFY"
    echo ""
  else
    printf '%s\n' "$n" > "$UPD/PENDING_VERIFY"
  fi
fi

# Make sure Homebrew tools (ffmpeg) and Claude Code (~/.local/bin) are found even
# when launched from a double-click (a Finder-launched app has a minimal PATH).
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"

# Python environment (shared with the transcriber/summarizer).
if [ ! -d .venv ]; then
  python3 -m venv .venv
  .venv/bin/pip install --quiet --upgrade pip
fi

# Make sure everything the app needs is installed.
need_install=""
.venv/bin/python -c "import flask"       >/dev/null 2>&1 || need_install="$need_install flask"
.venv/bin/python -c "import mlx_whisper"  >/dev/null 2>&1 || need_install="$need_install mlx-whisper"
.venv/bin/python -c "import anthropic"    >/dev/null 2>&1 || need_install="$need_install anthropic"
.venv/bin/python -c "import yt_dlp"       >/dev/null 2>&1 || need_install="$need_install yt-dlp"
if [ -n "$need_install" ]; then
  echo "Installing app components (one time):$need_install"
  .venv/bin/pip install --quiet --upgrade pip
  # shellcheck disable=SC2086
  .venv/bin/pip install --quiet $need_install
  echo "Done."
  echo ""
fi

# Keep the YouTube downloader current. YouTube changes how it serves audio every few
# weeks, and an out-of-date downloader is the usual reason a download comes back damaged.
# Checked at most once a week, with a short timeout, so it never delays the app starting
# (and does nothing at all when you're offline).
YTDLP_STAMP=".venv/.yt-dlp-checked"
if [ ! -f "$YTDLP_STAMP" ] || [ -n "$(find "$YTDLP_STAMP" -mtime +7 2>/dev/null)" ]; then
  echo "Checking for a YouTube downloader update (a moment)..."
  if .venv/bin/pip install --quiet --upgrade --timeout 8 --retries 1 yt-dlp >/dev/null 2>&1; then
    touch "$YTDLP_STAMP"
  else
    echo "  (skipped — couldn't reach the internet; the app works as normal)"
  fi
fi

# Anthropic API key (for the summary step) — ask once, store privately in .env.
if [ ! -f .env ]; then
  echo "To create summaries you need an Anthropic API key (starts with 'sk-ant-')."
  echo "It is saved only on this Mac in a private file (.env) and sent only to Anthropic."
  echo ""
  printf "Paste your API key and press Enter: "
  read -r APIKEY
  if [ -z "${APIKEY:-}" ]; then
    echo "No key entered. Run this again when you have your key." >&2
    exit 1
  fi
  printf 'ANTHROPIC_API_KEY=%s\n' "$APIKEY" > .env
  chmod 600 .env
  echo "Key saved."
  echo ""
fi
set -a
# shellcheck disable=SC1091
source ./.env
set +a

exec .venv/bin/python app.py
