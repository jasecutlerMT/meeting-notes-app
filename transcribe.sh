#!/usr/bin/env bash
set -euo pipefail

# Meeting Recorder — run the transcription step.
#
# The first time you run this, it creates a small private Python environment and
# installs the local Whisper transcriber (mlx-whisper, GPU-accelerated on Apple
# Silicon). After that it just transcribes.
#
# Usage:
#   bash transcribe.sh                 # transcribe your most recent recording
#   bash transcribe.sh "/path/to/session-folder"   # or a specific one

cd "$(dirname "$0")"

# Create the Python environment if it doesn't exist yet.
if [ ! -d .venv ]; then
  echo "First-time setup: creating a Python environment."
  python3 -m venv .venv
  .venv/bin/pip install --quiet --upgrade pip
fi

# Make sure the transcriber is installed (also handles upgrading from a previous engine).
# Install the package by name directly (robust against the requirements file being
# renamed by a download, e.g. losing its hyphen).
if ! .venv/bin/python -c "import mlx_whisper" >/dev/null 2>&1; then
  echo "Installing the transcriber (one time, a few minutes)..."
  .venv/bin/pip install --quiet --upgrade pip
  .venv/bin/pip install --quiet mlx-whisper
  echo "Transcriber installed."
  echo ""
fi

exec .venv/bin/python transcribe.py "$@"
