#!/bin/bash
# Recover Meeting Audio — for a meeting that recorded shorter than expected.
#
# What it does, in plain English:
#   1. Shows your recent meetings and lets you pick one.
#   2. Checks each audio file: how long it PLAYS vs how much audio is REALLY inside.
#      (A crashed recorder often leaves a file that plays 1 minute but secretly
#      holds the whole meeting.)
#   3. Repairs any file like that (the original is kept as a backup).
#   4. Offers to redo the meeting's transcript & notes from the recovered audio.
#
# Run it by double-clicking, or with:  bash "Recover Meeting Audio.command"

export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
BASE="$HOME/MeetingNotes"
BPS=32000   # the app records 16,000 samples/sec x 2 bytes x 1 channel

pause_exit() { echo ""; read -r -p "Press Return to close." _; exit "${1:-0}"; }
fmt() { awk -v s="${1:-0}" 'BEGIN{printf "%dm %02ds", s/60, s%60}'; }
dur() { ffprobe -v error -show_entries format=duration -of default=nw=1:nk=1 "$1" 2>/dev/null | awk '{printf "%d", $1}'; }

command -v ffprobe >/dev/null 2>&1 || { echo "❌ ffmpeg/ffprobe not found — it should have been installed by the app's setup."; pause_exit 1; }
[ -d "$BASE" ] || { echo "❌ No MeetingNotes folder found at $BASE."; pause_exit 1; }

echo "Your recent meetings (newest first):"
i=0; sessions=()
while IFS= read -r d; do
  d="${d%/}"
  i=$((i+1)); sessions+=("$d")
  echo "  $i) $(basename "$d")"
  [ "$i" -ge 10 ] && break
done < <(ls -1dt "$BASE"/*/ 2>/dev/null)
[ "${#sessions[@]}" -eq 0 ] && { echo "❌ No meetings found."; pause_exit 1; }

echo ""
read -r -p "Which meeting should I check? [1] " pick
pick="${pick:-1}"
case "$pick" in (*[!0-9]*|'') echo "❌ Please answer with a number."; pause_exit 1;; esac
[ "$pick" -ge 1 ] && [ "$pick" -le "${#sessions[@]}" ] || { echo "❌ That wasn't one of the choices."; pause_exit 1; }
SES="${sessions[$((pick-1))]}"

echo ""
echo "Checking: $(basename "$SES")"
echo ""

found=0; repaired=0; longest=0
for f in "$SES/room.wav" "$SES/you.wav" "$SES/them.wav"; do
  [ -f "$f" ] || continue
  found=1
  name="$(basename "$f")"
  bytes=$(stat -f%z "$f" 2>/dev/null || stat -c%s "$f" 2>/dev/null); bytes="${bytes:-0}"
  real=$(( (bytes > 44 ? bytes - 44 : 0) / BPS ))
  header=$(dur "$f"); header="${header:-0}"
  [ "$real" -gt "$longest" ] && longest="$real"
  echo "• $name — plays as: $(fmt "$header") | audio actually inside: ~$(fmt "$real")"
  if [ "$real" -gt $((header + 10)) ]; then
    echo "  → Good news: this file holds more audio than it admits. Repairing…"
    tmp="$f.repairing.wav"
    if ffmpeg -v error -y -ignore_length 1 -i "$f" -c:a pcm_s16le "$tmp" </dev/null && [ -s "$tmp" ]; then
      mv "$f" "$f.before-repair"
      mv "$tmp" "$f"
      echo "  ✅ Repaired — $name now plays $(fmt "$(dur "$f")"). (Original kept as $name.before-repair)"
      repaired=1
    else
      rm -f "$tmp"
      echo "  ❌ The repair didn't work for this file."
    fi
  elif [ "$real" -lt 120 ]; then
    echo "  → This file genuinely holds only ~$(fmt "$real") of audio — the rest was never recorded."
  fi
done

[ "$found" -eq 1 ] || { echo "❌ That meeting has no audio files (it may have been discarded)."; pause_exit 1; }

echo ""
if [ "$repaired" -eq 1 ]; then
  echo "🎉 Hidden audio was recovered. Next: make fresh notes from it."
elif [ "$longest" -ge 120 ]; then
  echo "No repair was needed, but at least one file above holds $(fmt "$longest") of audio —"
  echo "if your notes look shorter than that, redoing them should help."
else
  echo "Sadly there is no hidden audio to recover — the recording really did stop early."
  echo "(The app now watches for this while recording and will warn you immediately.)"
fi

echo ""
echo "I can move this meeting's old notes aside so the app redoes the transcript"
echo "and summary from the audio on disk. (Redoing the summary uses Claude credits"
echo "as normal, and a redone meeting saves to Notion as a new page.)"
read -r -p "Redo this meeting's notes? [y/N] " yn
case "$yn" in
  [Yy]*)
    mkdir -p "$SES/old-notes"
    for g in summary.md transcript.txt transcript.json transcript-refined.txt cost.json diar_progress.json; do
      [ -f "$SES/$g" ] && mv "$SES/$g" "$SES/old-notes/"
    done
    echo ""
    echo "✅ Old notes tucked into the meeting's 'old-notes' folder."
    echo ""
    echo "LAST STEP: open Meeting Notes from your Desktop icon. You'll see"
    echo "'An unfinished meeting was found' at the top — click '▶ Finish it now'"
    echo "and it will re-transcribe and re-summarize from the recovered audio."
    ;;
  *) echo "OK — nothing else was changed." ;;
esac
pause_exit 0
