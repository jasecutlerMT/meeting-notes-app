#!/usr/bin/env python3
"""
Meeting Recorder — Piece 2: transcription (Apple Silicon, fastest engine).

Turns a recording session into a readable, time-ordered transcript using OpenAI
Whisper locally and free via `mlx-whisper` (Apple MLX — GPU-accelerated).

Two kinds of session:
  • "Call on this Mac"  -> you.wav (mic) + them.wav (system audio).  Labelled You / Them.
  • "In the room/phone" -> room.wav (mic only, the whole room incl. a phone on speaker).
                           One stream, no You/Them split.

Outputs, written into the same session folder:
  transcript.txt   – readable, time-ordered transcript
  transcript.json  – the same content as data, for the summary step (Piece 3)

Usage:
  bash transcribe.sh                 # transcribe your most recent recording
  bash transcribe.sh "/path/to/session-folder"
"""

import argparse
import array
import datetime as dt
import json
import math
import sys
import wave
from pathlib import Path

DEFAULT_MODEL = "mlx-community/whisper-large-v3-turbo"  # near-best accuracy, fastest on Apple Silicon
DEFAULT_BASE = Path.home() / "MeetingNotes"

# --- safety net: catch sections Whisper skipped -------------------------------
# Whisper never makes a segment longer than ~30s, so a long stretch of audio with no
# words at all is either real silence or content it skipped. If that stretch is LOUD
# (someone is talking), we re-transcribe just that piece with a clean slate. This is
# what stops whole sections quietly vanishing from a transcript.
GAP_SEC = 45           # a hole this long with no words is worth checking
SUSPECT_SEC = 60       # speech this long we still couldn't make out gets reported to the user
MIN_SPEECH_SEC = 15    # ignore brief noises — only re-check real stretches of talking
PAUSE_MERGE_SEC = 5    # a pause this short is part of the same stretch of talking
GAP_PAD_SEC = 2.0      # transcribe slightly around the hole so words aren't clipped
MAX_GAPS = 20          # don't spend forever on a badly damaged file
RMS_FLOOR = 200.0      # absolute quiet threshold (16-bit samples)


def find_latest_session(base: Path):
    """Most recently modified folder under `base` that has a recording."""
    if not base.exists():
        return None
    candidates = [
        p for p in base.iterdir()
        if p.is_dir() and any((p / f).exists() for f in ("room.wav", "you.wav", "them.wav"))
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


def fmt_ts(seconds: float) -> str:
    seconds = max(0, int(seconds))
    return f"{seconds // 60:02d}:{seconds % 60:02d}"


def transcribe_track(model_repo: str, path: Path, speaker: str):
    """Transcribe one .wav file; return a list of segment dicts tagged with the speaker."""
    if not path.exists() or path.stat().st_size < 2000:
        if speaker:
            print(f"   (skipping {speaker} — {path.name} is missing or empty)")
        return []
    who = speaker or "the conversation"
    print(f"   Transcribing {who} ({path.name})... this can take a little while.")
    import mlx_whisper
    result = mlx_whisper.transcribe(str(path), path_or_hf_repo=model_repo, verbose=False)
    rows = []
    for seg in result.get("segments", []):
        text = (seg.get("text") or "").strip()
        if text:
            rows.append({"start": float(seg.get("start", 0.0)),
                         "end": float(seg.get("end", 0.0)),
                         "speaker": speaker, "text": text})
    print(f"      done — {len(rows)} lines, language detected: {result.get('language', '?')}")
    return rows


def per_second_rms(path: Path):
    """Loudness of each second of audio. Returns (list_of_rms, duration_sec).

    Returns (None, duration) if the file isn't 16-bit PCM (then we can't judge loudness
    and simply leave the transcript alone rather than guess)."""
    try:
        with wave.open(str(path), "rb") as w:
            fr, ch, sw = w.getframerate(), w.getnchannels(), w.getsampwidth()
            duration = w.getnframes() / float(fr) if fr else 0.0
            if sw != 2 or fr <= 0:
                return None, duration
            levels = []
            while True:
                raw = w.readframes(fr)  # one second at a time
                if not raw:
                    break
                a = array.array("h")
                a.frombytes(raw[: len(raw) - (len(raw) % 2)])
                if ch > 1:
                    a = a[::ch]          # first channel only
                a = a[::8]               # every 8th sample is plenty to gauge loudness
                if not a:
                    break
                levels.append(math.sqrt(sum(v * v for v in a) / len(a)))
            return levels, duration
    except Exception:
        return None, 0.0


def loud_spans(levels, start: float, end: float, floor: float, min_len: float):
    """The parts of a stretch where someone is actually talking.

    A hole in a transcript often contains both real silence and skipped speech, so we
    pick out just the talking — that way we don't re-transcribe silence, and we never
    tell the user that a quiet passage is 'damaged'."""
    spans, cur = [], None
    for sec in range(max(0, int(start)), min(int(math.ceil(end)), len(levels))):
        if levels[sec] > floor:
            if cur and sec - cur[1] <= PAUSE_MERGE_SEC:
                cur[1] = sec + 1          # same stretch of talking, just a short pause
            else:
                if cur:
                    spans.append(cur)
                cur = [sec, sec + 1]
    if cur:
        spans.append(cur)
    out = []
    for a, b in spans:
        a, b = max(float(a), start), min(float(b), end)
        if b - a >= min_len:
            out.append((a, b))
    return out


def find_gaps(rows, duration: float, min_gap: float):
    """Stretches of audio with no transcribed words at all."""
    gaps, cursor = [], 0.0
    for r in sorted(rows, key=lambda r: r["start"]):
        if r["start"] - cursor >= min_gap:
            gaps.append((cursor, r["start"]))
        cursor = max(cursor, r["end"])
    if duration - cursor >= min_gap:
        gaps.append((cursor, duration))
    return gaps


def write_slice(src: Path, dst: Path, start: float, end: float) -> bool:
    """Copy one stretch of a wav out to its own file, so we can re-transcribe just that."""
    try:
        with wave.open(str(src), "rb") as w:
            fr = w.getframerate()
            first = max(0, int(start * fr))
            count = int((end - start) * fr)
            if count <= 0:
                return False
            w.setpos(min(first, w.getnframes()))
            frames = w.readframes(count)
            if not frames:
                return False
            with wave.open(str(dst), "wb") as o:
                o.setnchannels(w.getnchannels())
                o.setsampwidth(w.getsampwidth())
                o.setframerate(fr)
                o.writeframes(frames)
        return True
    except Exception:
        return False


def fill_gaps(model_repo: str, path: Path, speaker: str, rows: list):
    """Find stretches Whisper skipped and transcribe them again on their own.

    Returns (extra_rows, recovered_spans, suspect_spans). Re-running a short slice with a
    clean slate (condition_on_previous_text=False) reliably recovers content that the
    long-form pass dropped after hitting damaged audio."""
    levels, duration = per_second_rms(path)
    if levels is None or duration <= 0:
        return [], [], []   # can't measure — leave everything exactly as it was

    speech = [v for v in levels if v > RMS_FLOOR]
    # Adapt to quiet recordings, but never trust a level below the absolute floor.
    floor = max(RMS_FLOOR, 0.10 * (sum(speech) / len(speech))) if speech else RMS_FLOOR

    # Look inside every long hole, and pick out only the parts where someone is talking.
    gaps = []
    for a, b in find_gaps(rows, duration, GAP_SEC):
        gaps += loud_spans(levels, a, b, floor, MIN_SPEECH_SEC)
    if not gaps:
        return [], [], []

    who = f" in {speaker}" if speaker else ""
    print(f"   [!] {len(gaps)} stretch(es){who} had speech but no words — re-checking them…")

    import mlx_whisper
    extra, recovered, tmp = [], [], path.parent / f"_gapcheck_{path.stem}.wav"
    for start, end in gaps[:MAX_GAPS]:
        lo = max(0.0, start - GAP_PAD_SEC)
        hi = min(duration, end + GAP_PAD_SEC)
        if not write_slice(path, tmp, lo, hi):
            continue
        try:
            res = mlx_whisper.transcribe(str(tmp), path_or_hf_repo=model_repo, verbose=False,
                                         condition_on_previous_text=False)
        except Exception as e:
            print(f"       (couldn't re-check {fmt_ts(start)}–{fmt_ts(end)}: {str(e)[:80]})")
            continue
        finally:
            try:
                tmp.unlink()
            except Exception:
                pass
        got = []
        for seg in res.get("segments", []):
            text = (seg.get("text") or "").strip()
            s = lo + float(seg.get("start", 0.0))
            e = lo + float(seg.get("end", 0.0))
            # Keep only what belongs to the hole itself — the padding is context, and its
            # words are already in the transcript.
            if text and start <= (s + e) / 2 <= end:
                got.append({"start": s, "end": e, "speaker": speaker, "text": text})
        if got:
            extra += got
            recovered.append(f"{fmt_ts(start)}–{fmt_ts(end)}")
            print(f"       recovered {len(got)} line(s) at {fmt_ts(start)}–{fmt_ts(end)}")

    # Any substantial stretch of talking still missing from the transcript is worth
    # warning the user about — that's audio we genuinely couldn't understand.
    merged = rows + extra
    suspect = []
    for a, b in find_gaps(merged, duration, GAP_SEC):
        suspect += [f"{fmt_ts(s)}–{fmt_ts(e)}"
                    for s, e in loud_spans(levels, a, b, floor, SUSPECT_SEC)]
    return extra, recovered, suspect


def line_for(r: dict) -> str:
    if r["speaker"]:
        return f"[{fmt_ts(r['start'])}] {r['speaker']}: {r['text']}"
    return f"[{fmt_ts(r['start'])}] {r['text']}"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Transcribe a recorded meeting into a readable transcript.")
    parser.add_argument("session", nargs="?", default=None)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--base", default=str(DEFAULT_BASE))
    args = parser.parse_args()

    if args.session:
        session = Path(args.session).expanduser()
        if not session.is_dir():
            print(f"ERROR: that folder doesn't exist: {session}", file=sys.stderr)
            return 1
    else:
        session = find_latest_session(Path(args.base).expanduser())
        if session is None:
            print(f"ERROR: no recordings found in {args.base}. Record one first.", file=sys.stderr)
            return 1

    room = session / "room.wav"
    is_room = room.exists() and room.stat().st_size >= 2000

    print(f"\n[*] Transcribing session: {session.name}")
    print(f"[*] Using Whisper model '{args.model}' (Apple MLX — GPU-accelerated).")
    print("    The FIRST time, this downloads the model (~1.5 GB) — please be patient.\n")

    rows, recovered, suspect = [], [], []

    def add_track(path: Path, speaker: str):
        """Transcribe one track, then double-check nothing was skipped."""
        got = transcribe_track(args.model, path, speaker)
        if got or (path.exists() and path.stat().st_size >= 2000):
            try:
                extra, rec, sus = fill_gaps(args.model, path, speaker, got)
            except Exception as e:  # a safety net must never break the transcript
                print(f"   (gap check skipped: {str(e)[:100]})")
                extra, rec, sus = [], [], []
            got += extra
            recovered.extend(rec)
            suspect.extend(sus)
        rows.extend(got)

    if is_room:
        add_track(room, "")  # single mic stream, no You/Them
        mode_note = "# Single microphone (in-person / phone on speaker)\n"
    else:
        add_track(session / "you.wav", "You")
        add_track(session / "them.wav", "Them")
        mode_note = "# You = your microphone   Them = the call audio\n"

    if not rows:
        print("\n[!] No speech was transcribed. The recording may have been silent.")
        return 1

    rows.sort(key=lambda r: r["start"])

    # Record what the safety net found, so the app can tell the user plainly.
    try:
        (session / "coverage.json").write_text(
            json.dumps({"recovered": recovered, "suspect": suspect}), encoding="utf-8")
    except Exception:
        pass
    if recovered:
        print(f"\n[*] Recovered {len(recovered)} section(s) the first pass had missed.")
    if suspect:
        print(f"[!] No words were picked up in these stretches, though there is sound there "
              f"(music/noise is normal; otherwise the audio may be damaged): {', '.join(suspect)}")

    transcript_txt = session / "transcript.txt"
    transcript_json = session / "transcript.json"

    header = (
        f"# Meeting transcript — {session.name}\n"
        f"# Transcribed: {dt.datetime.now().strftime('%Y-%m-%d %H:%M')}\n"
        + mode_note + "\n"
    )
    lines = [line_for(r) for r in rows]
    transcript_txt.write_text(header + "\n".join(lines) + "\n", encoding="utf-8")
    transcript_json.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"\n[OK] Transcript saved:\n   {transcript_txt}")
    print("\n----- preview (first lines) -----")
    for line in lines[:8]:
        print("  " + line)
    if len(lines) > 8:
        print(f"  ... ({len(lines) - 8} more lines)")
    print("---------------------------------")
    print(f'\nOpen the full transcript with:\n   open "{transcript_txt}"')
    return 0


if __name__ == "__main__":
    sys.exit(main())
