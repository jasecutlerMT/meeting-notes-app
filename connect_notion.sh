#!/usr/bin/env bash
set -uo pipefail

# Meeting Recorder — connect your Notion (one time).
#
# Saves your Notion integration token and the page/database where notes should go,
# then tests it by saving your most recent meeting to Notion.

cd "$(dirname "$0")"

echo "Connect Notion"
echo "--------------"
echo "You'll need two things (see the setup steps Claude gave you):"
echo "  1) Your Notion integration secret (starts with 'ntn_' or 'secret_')"
echo "  2) The link to the Notion page/database where notes should be saved"
echo ""
printf "Paste your Notion integration secret: "
read -r NTOKEN
printf "Paste the Notion page link (URL): "
read -r NPAGE

if [ -z "${NTOKEN:-}" ] || [ -z "${NPAGE:-}" ]; then
  echo "Both are required. Nothing was saved." >&2
  exit 1
fi

# Pull the clean page/database id out of the link (avoids special characters like ? and &).
PAGE_ID=$(python3 -c 'import sys,re
s=sys.argv[1]
m=re.findall(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}", s) or re.findall(r"[0-9a-fA-F]{32}", s)
print(m[0] if m else "")' "$NPAGE")

if [ -z "$PAGE_ID" ]; then
  echo "Could not find a Notion id in that link. Please paste the full page URL." >&2
  exit 1
fi

# Save into .env, keeping any existing keys (like your Anthropic key) intact.
touch .env
chmod 600 .env
{ grep -v '^NOTION_TOKEN=' .env 2>/dev/null | grep -v '^NOTION_PARENT_PAGE_ID=' ; } > .env.new || true
mv .env.new .env 2>/dev/null || true
printf 'NOTION_TOKEN=%s\n' "$NTOKEN" >> .env
printf 'NOTION_PARENT_PAGE_ID=%s\n' "$PAGE_ID" >> .env
chmod 600 .env

echo ""
echo "Saved (id: $PAGE_ID). Testing by saving your most recent meeting to Notion..."
echo ""

export NOTION_TOKEN="$NTOKEN"
export NOTION_PARENT_PAGE_ID="$PAGE_ID"

if python3 notion_sync.py; then
  echo ""
  echo "✅ Success — check your Notion page; a new note should be there."
  echo "   Restart the app (Control-C, then  bash start_ui.sh ) so it picks up Notion,"
  echo "   and from then on every meeting is saved to Notion automatically."
else
  echo ""
  echo "⚠️  That didn't work — the message above says why. The most common reason is"
  echo "    that the integration isn't connected to the page yet: open the page in"
  echo "    Notion, click ••• (top-right) > Connections > add your integration, then"
  echo "    run  bash connect_notion.sh  again."
fi
