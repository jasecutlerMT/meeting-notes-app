#!/usr/bin/env bash
set -euo pipefail

# Meeting Recorder — run the summary step (Piece 3).
#
# Reads your most recent transcript and uses the Claude API to write:
#   summary.md             – summary, action items, adaptive sections
#   transcript-refined.txt – the full transcript, AI-cleaned for accuracy
#
# The first time, it installs the Claude client and asks for your API key (saved
# privately on this Mac, in .env, used only to talk to Anthropic).
#
# Usage:
#   bash summarize.sh                 # summarize your most recent transcript
#   bash summarize.sh "/path/to/session-folder"
#   bash summarize.sh --model claude-sonnet-4-6    # cheaper model

cd "$(dirname "$0")"

# Reuse the same Python environment as the transcriber; create it if needed.
if [ ! -d .venv ]; then
  python3 -m venv .venv
  .venv/bin/pip install --quiet --upgrade pip
fi
if ! .venv/bin/python -c "import anthropic" >/dev/null 2>&1; then
  echo "Installing the Claude client (one time)..."
  .venv/bin/pip install --quiet anthropic
  echo "Installed."
  echo ""
fi

# Get the API key the first time and store it privately in .env.
if [ ! -f .env ]; then
  echo "To create summaries you need an Anthropic API key (it starts with 'sk-ant-')."
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

# Load the key into the environment for this run.
set -a
# shellcheck disable=SC1091
source ./.env
set +a

exec .venv/bin/python summarize.py "$@"
