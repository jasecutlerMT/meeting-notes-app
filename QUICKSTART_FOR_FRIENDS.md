# Meeting Notes — Quick Start (for friends)

A personal meeting recorder: it records your Mac's calls (or an in-person / phone-on-speaker
meeting), transcribes them **locally and free** with OpenAI Whisper, and writes a summary +
action items with Claude. Your notes stay on your Mac (and optionally sync to **your** Notion).
You can also **import audio you already have** — upload a file (meeting, podcast, interview)
or paste a **YouTube link** — and it transcribes and summarizes that too.

## What you need
- A Mac with **Apple Silicon** (M1 / M2 / M3 / M4) on a recent macOS.
- **Homebrew** installed — get it at <https://brew.sh>.
- About 10 minutes for a one-time setup using the **Terminal** app (copy-paste, one line at a time).
- Your own accounts (see "Your own keys" below) — recording & transcription are free; only
  summaries cost a little.

## Setup (one time)
1. Put this `meeting-recorder` folder somewhere simple, e.g. your **Downloads**.
2. Open **Terminal** (press ⌘-Space, type "Terminal", Enter) and go to the folder:
   ```
   cd ~/Downloads/meeting-recorder
   ```
3. Install the bits it needs (ffmpeg + the audio helper):
   ```
   bash setup.sh
   ```
4. Make a clickable icon on your Desktop:
   ```
   bash make_icon.sh
   ```
5. Double-click **Meeting Notes** on your Desktop to launch it. The first time you record,
   macOS will ask for **Microphone** and **Screen & System Audio Recording** permission — click **Allow**.

## Your own keys (don't use anyone else's!)
- **Summaries (Claude):** get your **own** Anthropic API key at <https://console.anthropic.com>
  → add a few dollars of credit. The app asks for it the first time you make a summary.
  (Roughly a few tens of cents per meeting.)
- **Optional — save to your Notion:** `bash connect_notion.sh` (see "Save to Notion" in README.md).
- **Optional — label who's speaking:** `bash setup_diarization.sh` (needs a free Hugging Face token).

## Keeping it up to date
The version you're on is shown at the bottom of the page. When a newer one exists, the
**Update** button (next to Quit app) turns blue — click it, and the app updates itself and
restarts. Your recordings, notes and keys are never touched.

If an update ever causes trouble, double-click **Restore Previous Version** on your Desktop,
or simply launch the app again — it puts itself right automatically.

## Please note
- **Never share API keys or tokens.** Everyone uses their own; each person pays only for their own use.
- This is a do-it-yourself project, not an App Store app — if a step looks unfamiliar, the full
  **README.md** in this folder explains everything in plain English.
