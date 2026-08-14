#!/usr/bin/env python3
"""
Meeting Recorder — speaker identification (diarization).

For an in-room / phone-on-speaker recording (room.wav), works out who spoke when
and relabels the transcript with "Speaker 1", "Speaker 2", … — like Notion.

Reads transcript.json (made by transcribe.py), runs pyannote.audio on room.wav,
assigns each line to the speaker with the most time overlap, then rewrites
transcript.json and transcript.txt.

Needs a one-time setup (setup_diarization.sh): installs pyannote.audio and saves
your Hugging Face token (HF_TOKEN) to .env.

Usage:
  bash diarize.sh                 # your most recent in-room recording
  bash diarize.sh "/path/to/session-folder"
"""

import json
import os
import sys
import time
from pathlib import Path

# If a single operation isn't supported on the Apple GPU, let PyTorch run just that
# operation on the CPU instead of failing the whole job. Must be set before torch loads.
os.environ.setdefault("PYTORCH_ENABLE_MPS_FALLBACK", "1")

BASE = Path.home() / "MeetingNotes"
MODEL = os.environ.get("DIAR_MODEL", "pyannote/speaker-diarization-community-1")
# "auto" tries the Apple GPU (mps) first — usually 2-4x faster — then falls back to
# the CPU if anything goes wrong. Set DIAR_DEVICE=cpu in .env to force the old path.
DEVICE = os.environ.get("DIAR_DEVICE", "auto")


def fmt_ts(seconds: float) -> str:
    seconds = max(0, int(seconds))
    return f"{seconds // 60:02d}:{seconds % 60:02d}"


class _FileProgressHook:
    """Best-effort: write diarization progress (0–1) to a file so the app can show it.

    pyannote runs these steps, in this order: segmentation -> speaker_counting ->
    embeddings -> clustering/discrete_diarization. Embeddings is the long, CPU-heavy
    step and is the one that reports completed/total, so we make it the big 0.40->0.93
    ramp. We only mark the progress "reliable" once a step gives us a real
    completed/total (so a bare step with no count can't pin the bar high) — otherwise
    the app falls back to a time estimate instead of showing a wrong number.
    """

    # step-name fragment -> (band low, band high), in pyannote's real running order
    BANDS = [
        ("segment", 0.00, 0.40),   # sliding-window segmentation
        ("count", 0.40, 0.40),     # speaker counting (instant, no measurable progress)
        ("embed", 0.40, 0.93),     # embeddings — the long, measurable ramp
        ("cluster", 0.93, 0.99),
        ("discrete", 0.93, 0.99),
        ("diariz", 0.93, 0.99),
    ]

    def __init__(self, path: Path):
        self.path = path
        self.last = 0.0
        self.reliable = False

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def _band(self, step_name):
        s = (step_name or "").lower()
        for frag, lo, hi in self.BANDS:
            if frag in s:
                return lo, hi
        return None

    def __call__(self, step_name, step_artifact=None, file=None, total=None, completed=None):
        try:
            band = self._band(step_name)
            if band is None:
                return  # unrecognised step — leave the file alone; the app times-estimates
            lo, hi = band
            measurable = bool(total and completed is not None and total > 0)
            if measurable:
                frac = lo + (hi - lo) * min(1.0, max(0.0, completed / total))
            else:
                frac = lo  # only nudge up to this step's floor; not trustworthy on its own
            if frac > self.last:
                self.last = frac
            if measurable:
                self.reliable = True  # we've seen genuine completed/total progress
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(
                {"fraction": round(self.last, 4), "reliable": self.reliable, "step": step_name}))
            os.replace(tmp, self.path)
        except Exception:
            pass


def find_latest_room(base: Path):
    if not base.exists():
        return None
    c = [p for p in base.iterdir()
         if p.is_dir() and (p / "room.wav").exists() and (p / "transcript.json").exists()]
    return max(c, key=lambda p: p.stat().st_mtime) if c else None


def line_for(r: dict) -> str:
    if r.get("speaker"):
        return f"[{fmt_ts(r['start'])}] {r['speaker']}: {r['text']}"
    return f"[{fmt_ts(r['start'])}] {r['text']}"


def main() -> int:
    token = (os.environ.get("HF_TOKEN") or os.environ.get("HUGGINGFACE_TOKEN") or "").strip()

    session = Path(sys.argv[1]).expanduser() if len(sys.argv) > 1 else find_latest_room(BASE)
    if not session or not session.is_dir():
        print("ERROR: no in-room recording with a transcript found.", file=sys.stderr)
        return 1

    room = session / "room.wav"
    tjson = session / "transcript.json"
    if not room.exists():
        print("Diarization only applies to in-room / phone recordings (no room.wav). Skipping.")
        return 0
    if not tjson.exists():
        print(f"ERROR: no transcript.json in {session}. Run the transcription first.", file=sys.stderr)
        return 1
    if not token:
        print("ERROR: no Hugging Face token. Run setup_diarization.sh first.", file=sys.stderr)
        return 1

    rows = json.loads(tjson.read_text(encoding="utf-8"))
    if not rows:
        print("Nothing to diarize.")
        return 0

    print(f"[*] Identifying speakers in {session.name} — this runs on your Mac and can take a few minutes…")
    try:
        import torch
        from pyannote.audio import Pipeline
        try:
            pipeline = Pipeline.from_pretrained(MODEL, token=token)
        except TypeError:
            pipeline = Pipeline.from_pretrained(MODEL, use_auth_token=token)
        if pipeline is None:
            raise RuntimeError("could not load the model — check your token and that you accepted "
                               "the model's conditions on Hugging Face")

        if DEVICE == "auto":
            mps_ok = bool(getattr(torch.backends, "mps", None)
                          and torch.backends.mps.is_available())
            devices = ["mps", "cpu"] if mps_ok else ["cpu"]
        else:
            devices = [DEVICE]

        def run_on(dev):
            pipeline.to(torch.device(dev))
            hook = _FileProgressHook(session / "diar_progress.json")
            try:
                return pipeline(str(room), hook=hook)
            except TypeError as e:
                if "hook" not in str(e):
                    raise  # an unrelated error — don't waste minutes re-running the pipeline
                # older/newer pipeline without a hook argument — run without progress
                return pipeline(str(room))

        output = None
        for i, dev in enumerate(devices):
            label = "Apple GPU" if dev == "mps" else "CPU"
            try:
                t0 = time.time()
                output = run_on(dev)
                print(f"[*] Speaker identification ran on the {label} in {int(time.time() - t0)}s.")
                break
            except Exception as e:
                if i == len(devices) - 1:
                    raise
                print(f"[!] {label} run failed ({str(e)[:150]}) — retrying on the CPU…")
    except Exception as e:
        print(f"ERROR during speaker identification: {e}", file=sys.stderr)
        return 1

    diar = getattr(output, "speaker_diarization", output)
    turns = []
    try:
        for seg, _track, label in diar.itertracks(yield_label=True):
            turns.append((float(seg.start), float(seg.end), str(label)))
    except Exception:
        try:
            for seg, label in diar:
                turns.append((float(seg.start), float(seg.end), str(label)))
        except Exception as e:
            print(f"ERROR reading diarization result: {e}", file=sys.stderr)
            return 1

    order = {}

    def speaker_name(label):
        if label not in order:
            order[label] = f"Speaker {len(order) + 1}"
        return order[label]

    rows.sort(key=lambda r: r["start"])
    for r in rows:
        rs, rend = r["start"], r["end"]
        best, best_ov = None, 0.0
        for ts, te, lab in turns:
            ov = min(rend, te) - max(rs, ts)
            if ov > best_ov:
                best_ov, best = ov, lab
        r["speaker"] = speaker_name(best) if best is not None else (r.get("speaker") or "")

    header = (f"# Meeting transcript — {session.name}\n"
              f"# Speakers identified automatically (Speaker 1, 2, …)\n\n")
    lines = [line_for(r) for r in rows]
    (session / "transcript.txt").write_text(header + "\n".join(lines) + "\n", encoding="utf-8")
    tjson.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"[OK] Identified {len(order)} speaker(s); transcript updated.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
