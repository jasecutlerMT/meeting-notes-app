#!/usr/bin/env bash
set -euo pipefail

# Meeting Recorder — one-time setup (run this on your Mac).
#
# Installs / builds everything Piece 1 (the recorder) needs:
#   * ffmpeg   – via Homebrew (records the mic, writes .wav files)
#   * AudioTee – built from source (taps macOS system audio)

cd "$(dirname "$0")"

echo "==> Checking your system..."

# macOS only
if [[ "$(uname)" != "Darwin" ]]; then
  echo "ERROR: This tool runs on macOS only." >&2
  exit 1
fi

# Homebrew (a popular Mac package installer)
if ! command -v brew >/dev/null 2>&1; then
  echo "ERROR: Homebrew is not installed." >&2
  echo "Install it first (one line, from https://brew.sh):" >&2
  echo '  /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"' >&2
  echo "Then run ./setup.sh again." >&2
  exit 1
fi

# Swift / Xcode Command Line Tools (needed to build AudioTee)
if ! command -v swift >/dev/null 2>&1; then
  echo "==> Swift not found. Installing Xcode Command Line Tools..."
  echo "    A system dialog may pop up — click Install, let it finish, then run ./setup.sh again."
  xcode-select --install || true
  exit 1
fi

# ffmpeg
if ! command -v ffmpeg >/dev/null 2>&1; then
  echo "==> Installing ffmpeg (this can take a couple of minutes)..."
  brew install ffmpeg
else
  echo "==> ffmpeg already installed."
fi

# AudioTee — download + build
mkdir -p vendor
if [[ ! -d vendor/audiotee/.git ]]; then
  echo "==> Downloading AudioTee..."
  rm -rf vendor/audiotee
  git clone https://github.com/makeusabrew/audiotee.git vendor/audiotee
fi
echo "==> Building AudioTee (the first build can take a minute)..."
( cd vendor/audiotee && swift build -c release )

BIN="vendor/audiotee/.build/release/audiotee"
if [[ -x "$BIN" ]]; then
  echo ""
  echo "✅ Setup complete. AudioTee built at: $BIN"
  echo ""
  echo "Next steps:"
  echo "  1) Find your microphone index:   python3 record.py --list-devices"
  echo "  2) Record a quick test:          python3 record.py --title \"Test\""
  echo ""
  echo "  The first time you record, macOS will ask for Microphone and"
  echo "  System/Screen Audio Recording permission for your terminal app."
  echo "  Grant both, then run the test again if the first attempt was blocked."
else
  echo "ERROR: AudioTee did not build a binary at $BIN" >&2
  echo "If swift complained, try installing full Xcode from the App Store, then re-run." >&2
  exit 1
fi
