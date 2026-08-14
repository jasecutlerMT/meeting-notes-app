#!/usr/bin/env bash
set -uo pipefail

# Meeting Recorder — make a clickable launcher (run once):
#
#   bash make_icon.sh
#
# Creates "Meeting Notes" on your Desktop. Double-click it to launch the app: a
# small status window opens and your browser shows the app. To stop the app, use
# the "Quit app" button on the page (or close that small window).

DIR="$(cd "$(dirname "$0")" && pwd)"

if [ ! -f "$DIR/start_ui.sh" ]; then
  echo "ERROR: run this from inside the meeting-recorder folder." >&2
  exit 1
fi

# Remove any older versions of the launcher.
rm -rf "$HOME/Desktop/Meeting Notes.app"
CMD="$HOME/Desktop/Meeting Notes.command"

cat > "$CMD" <<LAUNCH
#!/bin/bash
# Launch the Meeting Notes app.
export PATH="/opt/homebrew/bin:/usr/local/bin:\$PATH"
cd "$DIR" || exit 1
clear
echo "Starting Meeting Notes…  your browser will open in a moment."
echo "Keep this small window open while you use the app."
echo "(To stop the app: click 'Quit app' on the page, or just close this window.)"
echo ""
bash start_ui.sh
LAUNCH

chmod +x "$CMD"

# Give the launcher its microphone icon (best-effort; needs the bundled PNG).
ICON="$DIR/MeetingIcon.png"
if [ -f "$ICON" ]; then
  MN_ICON="$ICON" MN_FILE="$CMD" osascript -l JavaScript >/dev/null 2>&1 <<'JXA'
ObjC.import('AppKit');
var env = $.NSProcessInfo.processInfo.environment;
var icon = env.objectForKey('MN_ICON').js;
var file = env.objectForKey('MN_FILE').js;
var img = $.NSImage.alloc.initWithContentsOfFile(icon);
if (img && img.isValid) {
  $.NSWorkspace.sharedWorkspace.setIconForFileOptions(img, file, 0);
  $.NSWorkspace.sharedWorkspace.noteFileSystemChanged(file);
}
JXA
fi

echo ""
echo "✅ Created: $CMD"
echo ""
echo "   Double-click 'Meeting Notes' on your Desktop to launch the app."
echo "   (If macOS asks the first time, choose Open.)"
echo "   A small status window opens and your browser shows the app."
