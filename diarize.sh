#!/usr/bin/env bash
set -uo pipefail

# Meeting Recorder — run speaker identification on a recording (in-room/phone).
#
#   bash diarize.sh                 # your most recent in-room recording
#   bash diarize.sh "/path/to/session-folder"

cd "$(dirname "$0")"
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"

if [ -f .env ]; then
  set -a
  # shellcheck disable=SC1091
  source ./.env
  set +a
fi

if [ ! -d .venv ] || ! .venv/bin/python -c "import pyannote.audio" >/dev/null 2>&1; then
  echo "Speaker identification isn't installed yet. Run:  bash setup_diarization.sh" >&2
  exit 1
fi

exec .venv/bin/python diarize.py "$@"
