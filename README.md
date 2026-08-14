# Meeting Recorder

A personal, mostly-local tool that records your meetings, transcribes them, and
writes up a summary + action items — a replacement for Notion's AI meeting notes.

It uses the same proven two-layer design Notion's tool uses:

1. **Transcription layer** — OpenAI **Whisper**, running **locally and free** on your Mac.
2. **Analysis layer** — a language model turns the transcript into a summary,
   action items, and headings that adapt to the meeting. Starts on the **Claude API**
   (a few cents per meeting), built to be swappable to a free local model later.

Everything is saved as plain files you own, in `~/MeetingNotes/`.

> **Platform:** macOS (built and tested for Apple Silicon, macOS 26 Tahoe).

---

## Easiest way: the app

After the one-time setup below, just run:

```bash
bash start_ui.sh
```

A page opens in your browser with a big **Start / Stop** button. Click **Start**
when your meeting begins and **Stop** when it ends — it records, transcribes, and
summarizes automatically, then shows your notes. Past meetings are listed there too.

**Can I close things while it works?** The **browser window** — yes, any time; the
work runs in the app itself, and the page just shows progress (reopen it whenever).
The **Terminal window** the icon opens — no; that *is* the app, so minimise it
instead of closing it. While processing, the app also keeps your Mac awake so a
long transcription can't stall because the machine dozed off. And if the app or
laptop does get shut down mid-meeting, the next launch shows an *unfinished
meeting* notice — click **▶ Finish it now** and it resumes where it left off
(already-finished steps aren't redone).

**Pressed Start by accident, or no one picked up?** Use **✕ Cancel** (shown while
recording, and while a meeting is processing). It discards the meeting entirely —
the audio is deleted, nothing is transcribed or summarized, and no Claude credits
are spent. It asks you to confirm first, since a discarded recording can't be
recovered.

**Prefer a clickable icon (no Terminal)?** Run this once:
```bash
bash make_icon.sh
```
It puts a **Meeting Notes** app on your Desktop. Double-click it to launch (drag it
to the Dock if you like). Stop the app with the **Quit app** button on the page,
or quit it from the Dock. (Run `make_icon.sh` again if you ever move the folder.)

**Keep the audio:** on any meeting's results page, **💾 Save audio to Desktop**
exports the recording as a single `.m4a` file into a Desktop folder called
**Meeting & Calls audio files** (for calls, your mic and the call are merged into
one recording). The file is named after the meeting, existing exports are never
overwritten, and Finder opens to show you the saved file.

Before you start, you can add **Context** (optional) — who's in the meeting and
what it's about (e.g. "Me (Jason) and Sarah from Finance; quarterly budget review").
It makes the summary sharper and helps put real names to the speakers. After a
meeting, the results have two tabs: **Summary** and **Full transcript** (a tidy,
speaker-by-speaker view). Under the transcript there's **📄 Download as Word
document** — it saves the transcript as a `.docx` styled like the on-screen view
(each speaker's name in its own colour, timestamps, grouped turns), built with no
extra software needed.

**Too slow?** The single biggest time sink is **speaker identification** ("Identify
who's speaking"). It now runs on the **Apple GPU automatically** (usually 2–4×
faster than the old CPU path) and falls back to the CPU by itself if the GPU path
ever fails — set `DIAR_DEVICE=cpu` in `.env` to force the old behaviour. If you
don't need Speaker 1/2 labels for a meeting, unticking the **Identify who's
speaking** checkbox is still the biggest saving. Also keep transcription on
**⚡ Fast** — the "Best accuracy" model is several times slower and only worth it
for Chinese or rough audio.

**Transcription quality** (pick before you press Start, applies to recordings and
imports): choose **⚡ Fast** (Whisper Turbo — the default; quickest, excellent for
English/Spanish) or **🎯 Best accuracy** (full Whisper large-v3 — the most accurate,
best for Chinese and tough audio, but several times slower with a one-time ~3 GB
model download). Your choice is remembered between launches.

**Two recording modes** (pick before you press Start):
- **Call on this Mac** — a Zoom/Teams/etc. call: captures the call's audio + your
  mic as two tracks (*You* / *Them*).
- **In the room / phone** — an in-person meeting, or a **phone call on speaker**:
  captures what your Mac's microphone hears (the whole room) as one conversation.
  Put the phone on speaker near your Mac.

**Import audio you already have:** below the Start button there's an
**or import audio you already have** section. You can **upload an audio or video
file** (a meeting, podcast, or interview — most formats work) or **paste a YouTube
link**, and it transcribes and summarizes it the same way. The import section has its
**own Name and Context fields** — describe who's speaking and what it's about and the
notes use that to name the speakers and sharpen the summary. See "Import existing audio" below.

**Save to Notion (optional):** after a one-time `bash connect_notion.sh`, every
meeting's notes are also created as a new page in your Notion. See "Save to Notion" below.

(The individual command-line steps below still work if you prefer them.)

---

## Updating the app

The version you're on is shown at the bottom of the page. Next to **Quit app** there's an
**Update** button:

- The app checks for a new version quietly when it opens. If one exists, the button turns
  blue and says *Update to 1.2.0*.
- Click it, and you'll see what's new before anything happens. Confirm, and the app
  updates itself and restarts. The page refreshes onto the new version by itself.
- Your recordings, notes, API keys (`.env`), and installed components (`.venv`, `vendor`)
  are never touched.

**If an update ever causes a problem**, double-click **Restore Previous Version** on your
Desktop — it puts the previous version back. You can also just launch the app again: if an
update didn't finish, or the new version won't start, it restores itself automatically.

Nothing is replaced until a download has been checked end to end (all files present, all
code valid, and the new app proven to load), and files are swapped in atomically — so a
failed or interrupted update leaves your working app exactly as it was.

### Publishing a new version (maintainer)

The app updates from a small **public mirror repo** (`jasecutlerMT/meeting-notes-app`),
which holds a copy of this folder at its root. To ship a release:

1. Bump `version` in `version.json` and put the user-facing changes in `notes`.
2. Commit here as usual, **then push the same files to the mirror repo's `main`** — the
   updater downloads that repo's tip. A release isn't live until the mirror is updated.
3. If you added a new Python dependency, add it to `start_ui.sh`'s install list in the
   same release, or the update will be refused as "doesn't load correctly" (by design).

Never commit `.env`, `.venv/` or `vendor/` to the mirror — an update archive containing
any of them is rejected outright.

---

## Roadmap (built in small, testable pieces)

| Piece | What it does | Status |
|------|---------------|--------|
| **1. Recorder** | Capture your mic + the call's system audio to two `.wav` tracks | ✅ done |
| **2. Transcription** | Run Whisper locally → a readable transcript (labelled *You* / *Them*) | ✅ done |
| **3. Summary + refined transcript** | Claude API → summary, action items, adaptive headings, and an accuracy-polished transcript | ✅ done |
| **4. App interface** | A browser Start/Stop button that runs the whole pipeline | ✅ done |
| _later_ | iPhone capture · speaker names · push to Notion | _not yet_ |

---

## Piece 1 — the recorder

Records **two** audio sources at once and saves them separately:

- `you.wav` — your microphone (your voice)
- `them.wav` — the call's system audio (everyone else), tapped with [AudioTee](https://github.com/makeusabrew/audiotee)

Keeping them on separate tracks is deliberate: it makes the later "who said what"
labelling easy, and lets Whisper transcribe each side cleanly.

### Prerequisites
- An Apple Silicon Mac on macOS 14.2+ (Tahoe is fine).
- [Homebrew](https://brew.sh) installed.

### Setup (run once)
```bash
cd meeting-recorder
./setup.sh
```
This installs `ffmpeg` and builds AudioTee. The first time, macOS may ask you to
install the Xcode Command Line Tools — click Install, let it finish, and run
`./setup.sh` again.

### Record a meeting
```bash
# 1) Find your microphone's index (one-time check)
python3 record.py --list-devices

# 2) Record (use the --mic number from step 1 if it isn't 0)
python3 record.py --title "Team Sync"
# ...press ENTER to stop.
```

The **first** time you record, macOS will prompt for **Microphone** and
**System/Screen Audio Recording** permission for your terminal app. Grant both
(System Settings ▸ Privacy & Security), then record again if the first try was blocked.

Files land in a timestamped folder like:
```
~/MeetingNotes/2026-06-29_141500_Team-Sync/
├── you.wav        ← your voice
├── them.wav       ← the other participants
└── audiotee.log   ← technical log (ignore unless troubleshooting)
```

### Verify it worked
Play the two files back (built-in macOS player):
```bash
cd ~/MeetingNotes/<the-folder-you-just-made>
afplay you.wav     # should be YOUR voice
afplay them.wav    # should be the OTHER audio (e.g. a video you played)
```
If you can hear the right thing in each, Piece 1 is working. ✅

---

## Troubleshooting

**A file is silent / nearly empty (a few KB).**
- Permissions: System Settings ▸ Privacy & Security ▸ **Microphone** and
  **Screen & System Audio Recording** — make sure your terminal app is ticked.
  After granting, fully quit and reopen the terminal, then record again.
- `you.wav` empty → wrong mic. Run `python3 record.py --list-devices` and pass the
  right number, e.g. `python3 record.py --mic 1`.
- `them.wav` empty → no audio was actually playing during the test, or System Audio
  Recording permission was denied. Play something audible and try again.

**A meeting recorded shorter than it really was.**
Run the bundled recovery tool — it finds audio trapped in a damaged file, repairs
it, and queues the meeting for fresh notes:
```bash
bash "Recover Meeting Audio.command"
```
While recording, the app also keeps your Mac awake and watches the recorder's
health — if capture ever dies mid-meeting, a red warning appears immediately so
you can restart instead of losing the meeting.

**`swift build` fails during setup.**
Install the full **Xcode** from the App Store, then re-run `./setup.sh`.

**AudioTee details:** it's an open-source helper that uses Apple's built-in
Core Audio "process taps" to capture system audio — no virtual audio device, no
extra drivers. Source: <https://github.com/makeusabrew/audiotee>.

---

## Piece 2 — transcription

Turns a recording into a readable, time-ordered transcript labelled **You** / **Them**,
using OpenAI Whisper locally and free via [`mlx-whisper`](https://github.com/ml-explore/mlx-examples/tree/main/whisper)
— Apple's MLX engine, GPU-accelerated on Apple Silicon (M-series). Same Whisper
model and accuracy as the CPU engine, just much faster on M-series Macs.

```bash
# Transcribe your most recent recording:
bash transcribe.sh

# ...or a specific session folder:
bash transcribe.sh "/Users/you/MeetingNotes/2026-06-29_141500_Team-Sync"
```

The **first** run creates a small Python environment (`.venv/`) and downloads the
Whisper model (~1.5 GB) — one-time, a few minutes. After that it's quick.

Outputs land in the same session folder:
```
transcript.txt    ← the readable transcript (open with: open transcript.txt)
transcript.json   ← same content as data, used by the summary step (Piece 3)
```

**Tip for cleaner transcripts:** in a real call, wear headphones. Then your mic
track (`you.wav`) only hears *you*, not the other participants leaking from your
speakers — which keeps the *You* / *Them* labels crisp.

Model choice: the default `mlx-community/whisper-large-v3-turbo` is near-best
accuracy and fastest. For absolute best accuracy on tough audio, use the full
large model (slower): `bash transcribe.sh --model mlx-community/whisper-large-v3-mlx`.

---

## Piece 3 — summary + AI-refined transcript

Reads a transcript and uses the **Claude API** to write a summary, action items,
headings that adapt to the meeting, and an AI-cleaned full transcript.

```bash
bash summarize.sh                              # summarize your most recent transcript
bash summarize.sh "/path/to/session-folder"    # or a specific one
bash summarize.sh --model claude-sonnet-4-6    # cheaper model (or claude-haiku-4-5 = cheapest)
bash summarize.sh --no-refine                  # summary only, skip the refined transcript (cheaper)
```

The first run installs the Claude client and asks for your **Anthropic API key**
(get one at <https://console.anthropic.com> → Settings → API Keys; add a little
prepaid credit under Billing). The key is stored privately on your Mac in `.env`
(git-ignored) and sent only to Anthropic.

Outputs land in the session folder:
```
summary.md                ← open with: open summary.md
transcript-refined.txt    ← the AI-cleaned full transcript
```

**Use your Claude subscription instead of credits (optional):** if you have a
Claude Pro/Max subscription, summaries can run through Anthropic's official
Claude Code app under that subscription — no API credits used. One-time setup:
```bash
bash use_subscription.sh
```
It installs Claude Code if needed, walks you through logging in with your Claude
account, and flips the app over (adds `USE_CLAUDE_SUBSCRIPTION=1` to `.env` —
delete that line to switch back). Same models and prompts, so the notes are the
same quality. Usage counts toward your plan's allowance (refreshes every 5 hours),
and the app's per-meeting cost line disappears since nothing is billed per meeting.

**Cost (the only paid part):** a few tens of cents per meeting on the default
`claude-opus-4-8` (more when the transcript is long, because the refined transcript
rewrites the whole thing). Use `--model claude-sonnet-4-6` for ~half, or
`claude-haiku-4-5` for ~a fifth. Transcription stays free.

**Cost tracking in the app:** after each meeting the app shows its exact Claude cost
(computed from the real token usage the API reports), and the footer shows a running
total of everything spent through the app, with a link to your balance in the Anthropic
Console. The API can't read your prepaid balance directly — that lives in the Console —
so the app tracks spend and links you there for the actual balance.

---

## Import existing audio (files & YouTube)

You don't have to record live — you can feed in audio you already have, straight
from the app's home screen (the **or import audio you already have** section):

- **Upload a file** — click **Upload an audio or video file** and pick any
  meeting, podcast, or interview. Most formats work (m4a, mp3, wav, mov, mp4, …);
  for a video file it just uses the audio track.
- **Paste a YouTube link** — paste the URL and click **Transcribe link**. It
  downloads the audio with [yt-dlp](https://github.com/yt-dlp/yt-dlp) and runs the
  same pipeline. Other sites yt-dlp supports generally work too.

Fill in the import section's own **Name** and **Context** if you like — they apply to imports
exactly as they do to recordings (context still helps with names and accuracy).
Imported audio is treated as one conversation, so if you've set up **speaker
identification** (below), it labels the speakers too.

**Good to know about YouTube links:**
- It needs an internet connection, and longer videos take longer (download +
  transcription). A podcast that's hours long is fine, just not instant.
- Occasionally YouTube asks the downloader to "confirm you're not a robot." This
  usually clears up if you try again shortly. If it keeps happening, sign in to
  YouTube in Chrome or Safari and re-launch the app with that browser set — add a
  line `YT_COOKIES_BROWSER=chrome` (or `safari`/`firefox`) to your `.env` file.
  Background: [yt-dlp FAQ](https://github.com/yt-dlp/yt-dlp/wiki/FAQ).
- yt-dlp occasionally needs updating when sites change. To update:
  `.venv/bin/pip install --upgrade yt-dlp`.
- Please only download content you have the right to, and respect each site's
  terms of service. This is for personal use.

The summary still uses Claude (the only paid part), so importing a long podcast
costs a bit more than a short meeting (it's priced by transcript length).

---

## Save to Notion (optional)

After each meeting, the notes can be created automatically as a new page in your
Notion. One-time setup:

1. **Create a Notion integration:** go to <https://www.notion.so/my-integrations> →
   **New integration** → name it (e.g. "Meeting Recorder") → copy the
   **Internal Integration Secret** (starts with `ntn_`).
2. **Pick where notes go:** create (or choose) a Notion **page**, e.g. "Meeting Notes".
3. **Connect the integration to that page:** open the page → **•••** (top-right) →
   **Connections** → add your "Meeting Recorder" integration.
4. **Connect it here:**
   ```bash
   bash connect_notion.sh
   ```
   Paste the integration secret and the page's link when asked. It saves them
   (privately, in `.env`) and tests by saving your most recent meeting.

After that, every meeting (via the app or `summarize` + `notion_sync`) is added to
that Notion page as a new sub-page. Uses Python's standard library only — no extra
installs. To sync an existing meeting manually:

**Dates are filled in automatically.** If your Notion target is a database with a
Date-type column (a "Date" column is preferred if there are several), each meeting's
date and time are written into it — so Notion's date grouping and sorting just work.
If there's no Date column, or your target is a plain page, a "📅 …" date line is
written at the top of the note instead.

**Untitled meetings name themselves.** If you leave the meeting name blank, Claude
titles the meeting from what was actually discussed (in the meeting's own language),
and that title is used in Notion and in the app's Recent meetings list. A title you
type yourself is never replaced.
```bash
python3 notion_sync.py            # most recent
python3 notion_sync.py "/path/to/session-folder"
```

---

## Speaker identification (optional)

For **In the room / phone** recordings, the app can label who spoke
(*Speaker 1 / Speaker 2 / …*) using [pyannote.audio](https://github.com/pyannote/pyannote-audio)
diarization. One-time setup:

1. **Hugging Face account + model access:** sign up at <https://huggingface.co>, then
   open <https://hf.co/pyannote/speaker-diarization-community-1> and **accept** the
   model's conditions.
2. **Create a token:** <https://hf.co/settings/tokens> → New token (type **Read**) →
   copy it (starts with `hf_`).
3. **Install + connect:**
   ```bash
   bash setup_diarization.sh
   ```
   It installs `pyannote.audio` (PyTorch is already present) and saves your token.

After that, in-room/phone recordings are labelled automatically. To re-run on an
existing recording:
```bash
bash diarize.sh                  # most recent in-room recording
bash diarize.sh "/path/to/session-folder"
```

Notes: runs locally on CPU (a few minutes per meeting). On phone-on-speaker audio,
telling **you** apart from **the phone** is reliable; telling several *remote*
people apart from each other is the hard case for any tool. Labels are anonymous
(*Speaker 1/2*), same as Notion.
