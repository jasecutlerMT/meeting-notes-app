#!/usr/bin/env bash
set -uo pipefail

# Meeting Recorder — set up speaker identification (run once).
#
# Installs pyannote.audio (PyTorch is already present from the transcriber) and
# saves your Hugging Face token. After this, in-room / phone recordings get
# "Speaker 1 / Speaker 2 …" labels automatically.

cd "$(dirname "$0")"
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"

if [ ! -d .venv ]; then
  python3 -m venv .venv
  .venv/bin/pip install --quiet --upgrade pip
fi

if ! .venv/bin/python -c "import pyannote.audio" >/dev/null 2>&1; then
  echo "Installing speaker-identification components (pyannote.audio)."
  echo "This is a one-time, somewhat larger install — please be patient."
  echo ""
  .venv/bin/pip install --upgrade pip >/dev/null 2>&1 || true
  if ! .venv/bin/pip install "pyannote.audio"; then
    echo ""
    echo "ERROR: the install failed. Please paste the messages above to Claude." >&2
    exit 1
  fi
  echo ""
  echo "Installed."
fi

# Hugging Face token (needed to download the speaker model).
if ! grep -q '^HF_TOKEN=' .env 2>/dev/null; then
  echo ""
  echo "Paste your Hugging Face access token (starts with 'hf_')."
  echo "It is saved only on this Mac in a private file (.env)."
  printf "Token: "
  read -r HFTOK
  if [ -z "${HFTOK:-}" ]; then
    echo "No token entered. Run this again when you have it." >&2
    exit 1
  fi
  touch .env
  chmod 600 .env
  printf 'HF_TOKEN=%s\n' "$HFTOK" >> .env
  chmod 600 .env
  echo "Token saved."
fi

echo ""
echo "✅ Speaker identification is set up."
echo "   It runs automatically on 'In the room / phone' recordings."
echo "   Restart the app (Quit, then reopen) so it picks this up."
echo "   To test it now on your most recent in-room recording:  bash diarize.sh"
