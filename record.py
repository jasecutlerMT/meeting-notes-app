#!/usr/bin/env python3
"""
Meeting Recorder — Piece 1: the recorder.

Captures TWO audio sources on your Mac at the same time and saves them as two
separate 16 kHz mono WAV files inside a timestamped session folder:

  you.wav   – your microphone (your own voice)
  them.wav  – the system audio (everyone else on the call), tapped via AudioTee

Why two separate files? Keeping "you" and "them" on their own tracks means the
later transcript step can label who spoke (you vs. the room) for free.

This script only uses Python's standard library. It runs two helper programs:
  * AudioTee  – tiny tool that taps macOS system audio (built by ./setup.sh)
  * ffmpeg    – records the mic and writes the .wav files

Run ./setup.sh once before using this.
"""

import argparse
import datetime as dt
import signal
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT_AUDIOTEE = HERE / "vendor" / "audiotee" / ".build" / "release" / "audiotee"
SAMPLE_RATE = "16000"  # 16 kHz mono — the format Whisper expects later


def list_devices() -> int:
    """Print the audio inputs ffmpeg can see, so you can find your microphone's index."""
    print("Audio inputs ffmpeg can see (look under 'AVFoundation audio devices'):\n")
    # ffmpeg prints the device list to stderr and exits non-zero on purpose — that's fine.
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-f", "avfoundation",
         "-list_devices", "true", "-i", ""],
        check=False,
    )
    print("\nThe number in [ ] next to your microphone is the value for --mic (default is 0).")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Record a Mac meeting (your mic + the call's system audio).")
    parser.add_argument("--title", default="", help="optional short name for this meeting")
    parser.add_argument("--mic", default="0",
                        help="microphone device index (run --list-devices to find it)")
    parser.add_argument("--out", default=str(Path.home() / "MeetingNotes"),
                        help="folder to save recordings in (default: ~/MeetingNotes)")
    parser.add_argument("--audiotee", default=str(DEFAULT_AUDIOTEE),
                        help="path to the audiotee binary (built by ./setup.sh)")
    parser.add_argument("--list-devices", action="store_true",
                        help="list audio inputs and exit")
    args = parser.parse_args()

    if args.list_devices:
        return list_devices()

    audiotee = Path(args.audiotee)
    if not audiotee.exists():
        print(f"ERROR: AudioTee not found at {audiotee}\n"
              f"Run ./setup.sh first to build it.", file=sys.stderr)
        return 1

    # Make a timestamped session folder, e.g. ~/MeetingNotes/2026-06-29_141500_Team-Sync
    stamp = dt.datetime.now().strftime("%Y-%m-%d_%H%M%S")
    safe_title = "_".join(args.title.split())
    folder_name = f"{stamp}_{safe_title}" if safe_title else stamp
    session = (Path(args.out).expanduser() / folder_name)
    session.mkdir(parents=True, exist_ok=True)

    you_wav = session / "you.wav"
    them_wav = session / "them.wav"
    audiotee_log = session / "audiotee.log"

    print(f"\n[*] Saving to: {session}")
    print("[*] Starting recording...\n")

    log_fh = open(audiotee_log, "wb")

    # 1) System audio ("them"): AudioTee streams raw PCM on stdout -> ffmpeg wraps it as WAV.
    p_audiotee = subprocess.Popen(
        [str(audiotee), "--sample-rate", SAMPLE_RATE],
        stdout=subprocess.PIPE, stderr=log_fh,
    )
    p_them = subprocess.Popen(
        ["ffmpeg", "-loglevel", "error", "-y",
         "-f", "s16le", "-ar", SAMPLE_RATE, "-ac", "1", "-i", "pipe:0",
         str(them_wav)],
        stdin=p_audiotee.stdout,
    )
    # Hand the pipe to ffmpeg and close our copy so end-of-stream propagates on stop.
    if p_audiotee.stdout:
        p_audiotee.stdout.close()

    # 2) Microphone ("you"): ffmpeg reads the mic directly via macOS AVFoundation.
    p_you = subprocess.Popen(
        ["ffmpeg", "-loglevel", "error", "-y",
         "-f", "avfoundation", "-i", f":{args.mic}",
         "-ar", SAMPLE_RATE, "-ac", "1",
         str(you_wav)],
        stdin=subprocess.PIPE,
    )

    # Give them a moment to start, then make sure nothing died instantly
    # (usually a missing permission or a wrong --mic index).
    time.sleep(1.0)
    for label, proc in (("AudioTee (system audio)", p_audiotee),
                        ("system-audio recorder", p_them),
                        ("microphone recorder", p_you)):
        if proc.poll() is not None:
            print(f"\nERROR: {label} stopped immediately.\n"
                  f"  - Check that you granted Microphone AND System/Screen Audio Recording\n"
                  f"    permission to your terminal app (System Settings > Privacy & Security).\n"
                  f"  - Try a different --mic index (run: python3 record.py --list-devices).\n"
                  f"  - Details: {audiotee_log}", file=sys.stderr)
            _stop_all(p_audiotee, p_them, p_you)
            log_fh.close()
            return 1

    print("[REC] Recording. Press ENTER to stop.\n")
    try:
        input()
    except (KeyboardInterrupt, EOFError):
        print("\nStopping...")

    _stop_all(p_audiotee, p_them, p_you)
    log_fh.close()

    print("\n[OK] Done. Files saved:")
    ok = True
    for f in (you_wav, them_wav):
        size = f.stat().st_size if f.exists() else 0
        print(f"   {f}  ({size/1024:.0f} KB)")
        if size < 2000:
            ok = False
    if not ok:
        print("\n[!] One of the files looks empty. Most likely a missing permission or the\n"
              "    wrong --mic index. See the Troubleshooting section in README.md.")
    return 0


def _stop_all(p_audiotee, p_them, p_you) -> None:
    """Stop everything cleanly so the .wav files get a proper ending (trailer)."""
    # Ask the mic recorder to quit gracefully (ffmpeg responds to 'q' on stdin).
    try:
        if p_you.poll() is None and p_you.stdin:
            p_you.communicate(input=b"q", timeout=5)
    except Exception:
        pass
    # Stopping AudioTee closes the pipe, so the system-audio ffmpeg finalises on its own;
    # send SIGINT to anything still running as a belt-and-braces finish.
    for proc in (p_audiotee, p_them, p_you):
        try:
            if proc.poll() is None:
                proc.send_signal(signal.SIGINT)
        except Exception:
            pass
    for proc in (p_audiotee, p_them, p_you):
        try:
            proc.wait(timeout=5)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass


if __name__ == "__main__":
    sys.exit(main())
