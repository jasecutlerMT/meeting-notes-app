#!/usr/bin/env bash
set -uo pipefail

# Switch meeting summaries to your Claude subscription (Pro/Max) instead of
# API credits, using Anthropic's official Claude Code app.
#
# Run from inside the meeting-recorder folder:
#   bash use_subscription.sh

cd "$(dirname "$0")"
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"

echo ""
echo "This switches your meeting summaries to your Claude subscription (Pro/Max),"
echo "so meetings stop using pay-per-use API credits."
echo ""

# 1) Install Claude Code if it's missing.
if command -v claude >/dev/null 2>&1; then
  echo "[1/3] Claude Code is already installed. ✓"
else
  echo "[1/3] Installing Claude Code (Anthropic's official installer)…"
  curl -fsSL https://claude.ai/install.sh | bash
  export PATH="$HOME/.local/bin:$PATH"
  if ! command -v claude >/dev/null 2>&1; then
    echo "❌ The install didn't finish. Close this window, open a NEW Terminal"
    echo "   window, and run this script again."
    exit 1
  fi
  echo "      Installed. ✓"
fi

# 2) Make sure Claude Code is logged in with the Claude subscription.
#
# Check it the same way the app runs it — with any saved API key removed from the
# environment. An API key takes precedence over the claude.ai login, so checking with
# the key present would test the wrong thing entirely and report "logged in" when the
# subscription isn't usable.
claude_sub() { env -u ANTHROPIC_API_KEY -u ANTHROPIC_AUTH_TOKEN claude "$@"; }

echo "[2/3] Checking that Claude Code is logged in…"
if claude_sub -p "Reply with exactly: ok" >/dev/null 2>&1; then
  echo "      Logged in. ✓"
else
  echo ""
  echo "      Claude Code will open now. When it asks how to log in, choose the"
  echo "      option for your **Claude account / subscription** (NOT the API key"
  echo "      option) and finish the login in your browser."
  echo "      When you're back at the Claude Code screen, type /exit and press Return."
  echo ""
  read -r -p "      Press Return to open Claude Code… " _
  claude_sub || true
  if ! claude_sub -p "Reply with exactly: ok" >/dev/null 2>&1; then
    echo "❌ Claude Code still isn't logged in. Run this script again after logging in."
    exit 1
  fi
  echo "      Logged in. ✓"
fi

# 3) Flip the app into subscription mode.
touch .env
if grep -q '^USE_CLAUDE_SUBSCRIPTION=' .env; then
  sed -i '' 's/^USE_CLAUDE_SUBSCRIPTION=.*/USE_CLAUDE_SUBSCRIPTION=1/' .env
else
  printf 'USE_CLAUDE_SUBSCRIPTION=1\n' >> .env
fi
echo "[3/3] Done ✅"
echo ""
echo "Meeting summaries now run on your Claude subscription — no API credits."
echo "Quit the Meeting Notes app (⏻ button) and relaunch it to take effect."
echo ""
echo "To switch back to API credits later: open .env in this folder and delete"
echo "the USE_CLAUDE_SUBSCRIPTION line."
