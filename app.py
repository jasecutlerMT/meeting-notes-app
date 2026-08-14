#!/usr/bin/env python3
"""
Meeting Recorder — Piece 4: a beautiful local web interface.

Run it (via start_ui.sh) and a page opens in your browser with a big Start/Stop
button. Two recording modes:
  • "Call on this Mac"  – captures the call's system audio + your mic (You/Them).
  • "In the room/phone" – captures only your Mac's mic (the whole room, incl. a
                          phone on speaker), as one conversation.

When you hit Stop it transcribes and summarizes automatically, and — if you've
connected Notion (connect_notion.sh) — saves the notes to Notion too.

This is a LOCAL app: the server runs only on your own Mac (http://127.0.0.1:5037).
"""

import datetime as dt
import importlib.util
import json
import math
import os
import py_compile
import re
import shutil
import signal
import socket
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from io import BytesIO
from pathlib import Path

from flask import Flask, jsonify, request, send_file

import docx_export

HERE = Path(__file__).resolve().parent
AUDIOTEE = HERE / "vendor" / "audiotee" / ".build" / "release" / "audiotee"
BASE = Path.home() / "MeetingNotes"
SAMPLE_RATE = "16000"
PORT = 5037
AUDIO_EXPORT_DIR = Path.home() / "Desktop" / "Meeting & Calls audio files"

# Whisper model choice (accuracy vs. speed). The chosen one is passed to transcribe.py.
WHISPER_MODELS = {
    "fast": "mlx-community/whisper-large-v3-turbo",   # quickest; great for English/Spanish
    "best": "mlx-community/whisper-large-v3-mlx",      # full large-v3: most accurate, slower
}
DEFAULT_QUALITY = "fast"

# ----- version & self-update --------------------------------------------------
# Updates are published to a small public repo that mirrors this folder, so the app can
# fetch them with no password or token. Only the app's own files are ever touched — your
# recordings, your API keys (.env) and the installed components (.venv, vendor) are not.
UPDATE_REPO = os.environ.get("MEETING_NOTES_UPDATE_REPO", "jasecutlerMT/meeting-notes-app")
UPDATE_BRANCH = os.environ.get("MEETING_NOTES_UPDATE_BRANCH", "main")
UPDATE_BASE_URL = os.environ.get(
    "MEETING_NOTES_UPDATE_URL", f"https://raw.githubusercontent.com/{UPDATE_REPO}/{UPDATE_BRANCH}")
UPDATE_TARBALL_URL = os.environ.get(
    "MEETING_NOTES_UPDATE_TARBALL",
    f"https://codeload.github.com/{UPDATE_REPO}/tar.gz/refs/heads/{UPDATE_BRANCH}")

# Files the app needs to run. An update that doesn't bring all of these is rejected.
UPDATE_REQUIRED = ("app.py", "ui.html", "start_ui.sh", "record.py", "transcribe.py",
                   "summarize.py", "diarize.py", "notion_sync.py", "docx_export.py",
                   "version.json")
# Never replaced or removed by an update — your key, your installed components, your logs.
UPDATE_KEEP = {".env", ".venv", "vendor", "app.log", ".update", "__pycache__", ".git"}
# If an update archive contains any of these, something is badly wrong upstream: stop.
UPDATE_FATAL = {".env", ".venv", "vendor"}

# Staging lives INSIDE the app folder on purpose: files are then swapped in with an
# atomic rename (only possible on the same disk), so a file is always either completely
# the old version or completely the new one — never half-written.
UPD = HERE / ".update"
STAGED = UPD / "staged"
BACKUPS = UPD / "backups"
MAX_TARBALL = 100 * 1024 * 1024
MAX_MEMBER = 50 * 1024 * 1024
MIN_FREE_BYTES = 300 * 1024 * 1024


def read_version() -> str:
    """Which version of the app this is."""
    try:
        v = json.loads((HERE / "version.json").read_text(encoding="utf-8")).get("version")
        return str(v).strip() if v else "unknown"
    except Exception:
        return "unknown"


APP_VERSION = read_version()

app = Flask(__name__)
# Allow large uploads (long podcasts / video files). This is a local-only server.
app.config["MAX_CONTENT_LENGTH"] = 4 * 1024 * 1024 * 1024  # 4 GB

_lock = threading.Lock()
state = {
    "status": "idle",     # idle | recording | processing | done | error
    "session": None,
    "title": None,
    "mode": "mac",        # mac | room
    "started_at": None,
    "proc_started_at": None,  # when processing began (for the progress estimate)
    "step": "",
    "progress": 0,        # 0–100, how far through processing we are
    "eta": None,          # estimated seconds remaining (None = still estimating)
    "took": None,         # how long the finished meeting took to process (seconds)
    "phase_times": None,  # per-step seconds, e.g. {"transcribe": 320, "diarize": 900}
    "summary_md": None,
    "notion_url": None,
    "notion_error": None,
    "error": None,
}
_procs = {}
_plan = None  # processing phase plan {session, phases:[{key,label,est}], index, ...}; guarded by _lock

# ----- cancellation -------------------------------------------------------------
_cancel = threading.Event()   # set when the user cancels the current recording/processing
_pipeline = {"proc": None}    # the pipeline step currently running, so cancel can kill it


class _Cancelled(Exception):
    """Raised inside the processing pipeline when the user cancels."""


def run_tracked(cmd):
    """Run one pipeline step as a killable process, so a user cancel takes effect
    immediately instead of waiting for the step to finish."""
    if _cancel.is_set():
        raise _Cancelled()
    p = subprocess.Popen(cmd, cwd=str(HERE), env=dict(os.environ),
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    with _lock:
        _pipeline["proc"] = p
    try:
        out, err = p.communicate()
    finally:
        with _lock:
            _pipeline["proc"] = None
    if _cancel.is_set():
        raise _Cancelled()
    return subprocess.CompletedProcess(cmd, p.returncode, out, err)


def _pipeline_worker(fn, *args, **kwargs):
    """Run a pipeline job while keeping the Mac awake, so a long transcription
    doesn't stall because the machine went to sleep (best-effort, macOS only)."""
    caf = None
    try:
        caf = subprocess.Popen(["caffeinate", "-is"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        caf = None
    try:
        fn(*args, **kwargs)
    finally:
        try:
            if caf:
                caf.terminate()
        except Exception:
            pass


def has_audio(p: Path) -> bool:
    return any((p / f).exists() and (p / f).stat().st_size > 2000
               for f in ("room.wav", "you.wav", "them.wav"))


def _finish_cancel(session: Path):
    """Clean up after a user cancel: delete the session's files, go back to idle."""
    global _plan
    shutil.rmtree(session, ignore_errors=True)
    _cancel.clear()
    with _lock:
        _plan = None
        if state["session"] == str(session):
            state.update(status="idle", session=None, title=None, started_at=None,
                         proc_started_at=None, step="", progress=0, eta=None,
                         took=None, phase_times=None, summary_md=None, notion_url=None, notion_error=None, error=None)


def notion_configured():
    return bool(os.environ.get("NOTION_TOKEN") and os.environ.get("NOTION_PARENT_PAGE_ID"))


def notify(message: str, sound: str = "Glass"):
    """Pop a macOS notification banner with a sound (best-effort, never fatal)."""
    try:
        script = (f'display notification {json.dumps(message)} '
                  f'with title "Meeting Notes" sound name {json.dumps(sound)}')
        subprocess.run(["osascript", "-e", script], capture_output=True, timeout=10)
    except Exception:
        pass


def fmt_took(sec) -> str:
    sec = max(0, int(sec or 0))
    if sec < 60:
        return f"{sec}s"
    if sec < 3600:
        return f"{sec // 60}m {sec % 60:02d}s"
    return f"{sec // 3600}h {(sec % 3600) // 60:02d}m"


def new_session(title: str) -> Path:
    """Make a fresh timestamped session folder, e.g. 2026-06-29_141500_Team-Sync."""
    stamp = dt.datetime.now().strftime("%Y-%m-%d_%H%M%S")
    safe = "_".join(title.split())
    session = BASE / (f"{stamp}_{safe}" if safe else stamp)
    session.mkdir(parents=True, exist_ok=True)
    return session


def read_quality(session: Path) -> str:
    """The transcription quality the user picked for this session ('fast' or 'best')."""
    try:
        q = json.loads((session / "meta.json").read_text(encoding="utf-8")).get("quality")
        return q if q in WHISPER_MODELS else DEFAULT_QUALITY
    except Exception:
        return DEFAULT_QUALITY


def read_cost(session: Path):
    """The Claude cost recorded for this meeting (None if not summarized yet)."""
    p = session / "cost.json"
    if not p.exists():
        return None
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        return {"cost_usd": round(float(d.get("cost_usd") or 0.0), 4),
                "input_tokens": int(d.get("input_tokens") or 0),
                "output_tokens": int(d.get("output_tokens") or 0)}
    except Exception:
        return None


def read_coverage(session: Path):
    """What the transcript safety net found: sections it recovered, and any stretches
    where there's clearly speech but we still couldn't make out the words (damaged
    audio). None when there's nothing worth telling the user."""
    p = session / "coverage.json"
    if not p.exists():
        return None
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        rec = [str(x) for x in (d.get("recovered") or [])]
        sus = [str(x) for x in (d.get("suspect") or [])]
        return {"recovered": rec, "suspect": sus} if (rec or sus) else None
    except Exception:
        return None


def read_billing(session: Path):
    """Which wallet paid for this meeting's notes, in plain terms:
      'api'          -> charged to your Anthropic API credits (cost.json exists)
      'subscription' -> made with your Claude subscription (no API credits)
      None           -> not summarized yet / unknown
    So every meeting can say, without ambiguity, whether it cost credits."""
    try:
        if (session / "cost.json").exists():
            return "api"
        if (session / "billing.txt").exists():
            if (session / "billing.txt").read_text(encoding="utf-8").strip() == "subscription":
                return "subscription"
    except Exception:
        pass
    return None


# ----- audio devices ----------------------------------------------------------
def list_audio_inputs():
    try:
        res = subprocess.run(
            ["ffmpeg", "-hide_banner", "-f", "avfoundation",
             "-list_devices", "true", "-i", ""],
            capture_output=True, text=True, timeout=15)
    except Exception:
        return []
    devices, in_audio = [], False
    for line in res.stderr.splitlines():
        if "AVFoundation audio devices" in line:
            in_audio = True
            continue
        if "AVFoundation video devices" in line:
            in_audio = False
            continue
        if in_audio:
            m = re.search(r"\[(\d+)\]\s+(.+?)\s*$", line)
            if m:
                devices.append({"index": int(m.group(1)), "name": m.group(2).strip()})
    return devices


def default_mic_index(devices):
    for d in devices:
        if "macbook" in d["name"].lower():
            return d["index"]
    for d in devices:
        if "iphone" not in d["name"].lower():
            return d["index"]
    return devices[0]["index"] if devices else 0


# ----- recording --------------------------------------------------------------
def start_recording(session: Path, mic_index: int, mode: str):
    session.mkdir(parents=True, exist_ok=True)
    procs = {}
    if mode == "mac":
        log_fh = open(session / "audiotee.log", "wb")
        p_audiotee = subprocess.Popen(
            [str(AUDIOTEE), "--sample-rate", SAMPLE_RATE],
            stdout=subprocess.PIPE, stderr=log_fh)
        p_them = subprocess.Popen(
            ["ffmpeg", "-loglevel", "error", "-y",
             "-f", "s16le", "-ar", SAMPLE_RATE, "-ac", "1", "-i", "pipe:0",
             str(session / "them.wav")],
            stdin=p_audiotee.stdout)
        if p_audiotee.stdout:
            p_audiotee.stdout.close()
        procs.update(audiotee=p_audiotee, them=p_them, log=log_fh)
        mic_file = session / "you.wav"
    else:  # room: mic only
        mic_file = session / "room.wav"
    procs["mic"] = subprocess.Popen(
        ["ffmpeg", "-loglevel", "error", "-y",
         "-f", "avfoundation", "-i", f":{mic_index}",
         "-ar", SAMPLE_RATE, "-ac", "1", str(mic_file)],
        stdin=subprocess.PIPE)
    try:
        # Keep the Mac awake while recording, so sleep can't cut a meeting short.
        procs["caffeinate"] = subprocess.Popen(
            ["caffeinate", "-is"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass
    return procs


def stop_recording(procs):
    p = procs.get("mic")
    try:
        if p and p.poll() is None and p.stdin:
            p.communicate(input=b"q", timeout=5)
    except Exception:
        pass
    for key in ("audiotee", "them", "mic", "caffeinate"):
        p = procs.get(key)
        try:
            if p and p.poll() is None:
                p.send_signal(signal.SIGINT)
        except Exception:
            pass
    for key in ("audiotee", "them", "mic", "caffeinate"):
        p = procs.get(key)
        try:
            if p:
                p.wait(timeout=5)
        except Exception:
            try:
                p.kill()
            except Exception:
                pass
    lg = procs.get("log")
    try:
        if lg:
            lg.close()
    except Exception:
        pass


# ----- progress estimation ----------------------------------------------------
# Processing runs in phases (transcribe -> maybe diarize -> summarize -> maybe
# Notion). We estimate each phase's length from the audio duration, show a moving
# bar, and — for the slow diarization step — use its real progress to self-correct
# the time remaining. Whisper on Apple Silicon is fast; pyannote on CPU is the slow
# part, so these multipliers are deliberately conservative there.
def probe_duration(path: Path):
    """Length of an audio/video file in seconds, via ffprobe (None if unknown)."""
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", str(path)],
            capture_output=True, text=True, timeout=8)  # duration is a header read; keep it snappy
        return float(r.stdout.strip())
    except Exception:
        return None


def _asym(elapsed: float, est: float, cap: float = 0.95) -> float:
    """A fraction that rises quickly then eases toward `cap` — never sticks at 100%."""
    if est <= 0:
        est = 1.0
    return cap * (1.0 - math.exp(-max(0.0, elapsed) / est))


def diarize_wanted(session: Path) -> bool:
    """Did the user leave speaker identification on for this meeting? (default yes)"""
    try:
        return json.loads((session / "meta.json").read_text(encoding="utf-8")
                          ).get("diarize", True) is not False
    except Exception:
        return True


def will_diarize(session: Path) -> bool:
    return bool((session / "room.wav").exists() and os.environ.get("HF_TOKEN")
                and importlib.util.find_spec("pyannote.audio") is not None
                and diarize_wanted(session))


def build_plan(session: Path) -> dict:
    """Estimate each processing phase's duration from the recording's length."""
    def dur(name):
        p = session / name
        return (probe_duration(p) or 0.0) if (p.exists() and p.stat().st_size > 2000) else 0.0

    room_d = dur("room.wav")
    if room_d > 0:
        media, content = room_d, room_d            # one stream, transcribed once
    else:
        you_d, them_d = dur("you.wav"), dur("them.wav")
        media, content = (you_d + them_d), max(you_d, them_d)  # two streams, two passes
    if media <= 0:
        media = 180.0
    if content <= 0:
        content = media

    # The "best" (full large-v3) model is several times slower than turbo.
    quality = read_quality(session)
    tx_factor = 0.6 if quality == "best" else 0.15
    tx_label = ("Transcribing your meeting (best-accuracy model, slower)…" if quality == "best"
                else "Transcribing your meeting (Whisper, on your Mac)…")
    phases = [{"key": "transcribe", "label": tx_label, "est": max(20.0, media * tx_factor)}]
    if will_diarize(session):
        # Runs on the Apple GPU when possible (~2-4x faster than the old CPU path);
        # if it falls back to CPU, the self-correcting ETA adjusts as real progress arrives.
        phases.append({"key": "diarize", "label": "Identifying who's speaking…",
                       "est": max(30.0, content * 0.5)})
    phases.append({"key": "summarize", "label": "Writing your summary with Claude…",
                   "est": max(25.0, content * 0.22 + 15.0)})
    if notion_configured():
        phases.append({"key": "notion", "label": "Saving to Notion…", "est": 8.0})

    now = time.time()
    return {"session": str(session), "phases": phases, "index": 0,
            "phase_started": now, "started": now,
            "total_est": sum(p["est"] for p in phases)}


def advance_phase(session: Path, key: str):
    """Move the plan to a named phase (closing the timer on the previous one)."""
    with _lock:
        if not _plan or _plan["session"] != str(session):
            return
        now = time.time()
        times = _plan.setdefault("times", {})
        cur = _plan["phases"][_plan["index"]]["key"]
        if cur != key and _plan.get("phase_active"):
            times[cur] = times.get(cur, 0.0) + (now - _plan["phase_started"])
        for i, p in enumerate(_plan["phases"]):
            if p["key"] == key:
                _plan["index"] = i
                _plan["phase_started"] = now
                _plan["phase_active"] = True
                state["step"] = p["label"]
                break


def _close_phase_times(session: Path):
    """Stop the current phase's timer and return all recorded step durations."""
    with _lock:
        if not _plan or _plan["session"] != str(session):
            return {}
        times = _plan.setdefault("times", {})
        if _plan.get("phase_active"):
            cur = _plan["phases"][_plan["index"]]["key"]
            times[cur] = times.get(cur, 0.0) + (time.time() - _plan["phase_started"])
            _plan["phase_active"] = False
        return {k: int(v) for k, v in times.items()}


def diar_fraction(session_str: str):
    """Real diarization progress (0–1) if pyannote reported something we trust."""
    try:
        p = Path(session_str) / "diar_progress.json"
        if p.exists():
            d = json.loads(p.read_text())
            if d.get("reliable"):
                return max(0.0, min(0.99, float(d.get("fraction", 0.0))))
    except Exception:
        pass
    return None


def compute_progress(s: dict, plan, now: float):
    """Return (percent 0–100, eta_seconds or None) for the current processing state."""
    status = s.get("status")
    if status == "done":
        return 100, 0
    if status != "processing":
        return (s.get("progress", 0) if status == "error" else 0), None

    # Preparing / downloading, before the phase plan exists: creep up slowly, no ETA.
    if not plan or plan.get("session") != s.get("session"):
        elapsed = max(0.0, now - (s.get("proc_started_at") or now))
        return int(_asym(elapsed, 40.0, cap=0.08) * 100), None

    phases, idx, total = plan["phases"], plan["index"], (plan["total_est"] or 1.0)
    cur = phases[idx]
    cur_elapsed = max(0.0, now - plan["phase_started"])
    real = diar_fraction(plan["session"]) if cur["key"] == "diarize" else None
    frac = real if real is not None else _asym(cur_elapsed, cur["est"], cap=0.95)

    done_w = sum(p["est"] for p in phases[:idx])
    overall = (done_w + cur["est"] * frac) / total
    percent = int(max(0.0, min(0.99, overall)) * 100)

    # Time remaining: project the current phase, then add the phases not yet started.
    if real is not None and real > 0.03:
        rem_cur = cur_elapsed * (1.0 - real) / real        # self-correcting from real rate
    else:
        rem_cur = max(cur["est"] - cur_elapsed, cur["est"] * 0.15)
    rem_future = sum(p["est"] for p in phases[idx + 1:])
    eta = int(max(5, rem_cur + rem_future))
    return percent, eta


def clear_plan():
    global _plan
    with _lock:
        _plan = None


# ----- recording watchdog -------------------------------------------------------
_rec_health = {"files": {}, "warning": None}


def recording_health(session: Path, mode: str, started_at):
    """While recording, check that the recorder processes are alive and the audio
    files keep growing. Returns a plain-English warning if the recording has died,
    so the user finds out immediately instead of after the meeting."""
    now = time.time()
    dead = set()
    for key, label in (("mic", "microphone"),
                       ("audiotee", "call audio"), ("them", "call audio")):
        p = _procs.get(key)
        if p is not None and p.poll() is not None:
            dead.add(label)
    stalled = False
    for f in (("room.wav",) if mode == "room" else ("you.wav", "them.wav")):
        try:
            size = (session / f).stat().st_size
        except OSError:
            size = -1
        prev = _rec_health["files"].get(f)
        if prev is None or size > prev[0]:
            _rec_health["files"][f] = (size, now)
        elif now - prev[1] > 12:
            stalled = True
    if dead:
        return ("The " + " and ".join(sorted(dead)) + " recorder has stopped working — "
                "this meeting is no longer being captured. Press Stop, then start a new recording.")
    if stalled and started_at and now - started_at > 15:
        return ("The recording has stopped growing — this meeting may no longer be "
                "captured. Press Stop, then start a new recording.")
    return None


def process_session(session: Path):
    global _plan
    plan = build_plan(session)
    with _lock:
        _plan = plan
        state["step"] = plan["phases"][0]["label"]
        state["progress"] = max(state.get("progress", 0), 1)
    try:
        # Skip transcription if it already finished (e.g. resuming after a shutdown).
        if not (session / "transcript.json").exists():
            advance_phase(session, "transcribe")
            model = WHISPER_MODELS[read_quality(session)]
            r = run_tracked([sys.executable, str(HERE / "transcribe.py"), str(session),
                             "--model", model])
            if r.returncode != 0:
                raise RuntimeError(r.stderr.strip() or "transcription failed")

        # Identify speakers for single-stream audio (in-room / phone recordings and
        # imported files), if it's been set up. Those all produce a room.wav.
        if will_diarize(session):
            advance_phase(session, "diarize")
            # best-effort: if this fails, we keep the unlabelled transcript and carry on
            run_tracked([sys.executable, str(HERE / "diarize.py"), str(session)])

        advance_phase(session, "summarize")
        r = run_tracked([sys.executable, str(HERE / "summarize.py"), str(session)])
        if r.returncode != 0:
            raise RuntimeError(r.stderr.strip() or "summary failed")

        summary = session / "summary.md"
        summary_md = summary.read_text(encoding="utf-8") if summary.exists() else ""

        notion_url, notion_error = None, None
        if notion_configured():
            advance_phase(session, "notion")
            rn = run_tracked([sys.executable, str(HERE / "notion_sync.py"), str(session)])
            if rn.returncode == 0:
                notion_url = rn.stdout.strip()
            else:
                notion_error = (rn.stderr.strip() or "could not save to Notion")[:300]

        if _cancel.is_set():  # cancelled right at the end — still honour it
            raise _Cancelled()

        times = _close_phase_times(session)
        with _lock:
            started = state.get("proc_started_at") or time.time()
        took = int(time.time() - started)
        nice_title = "Your meeting"
        try:  # keep the timings with the meeting, so past meetings can show them
            meta_p = session / "meta.json"
            meta = json.loads(meta_p.read_text(encoding="utf-8")) if meta_p.exists() else {}
            meta["took"] = took
            meta["phase_times"] = times
            meta_p.write_text(json.dumps(meta), encoding="utf-8")
            nice_title = (meta.get("title") or "").strip() or nice_title
        except Exception:
            pass

        with _lock:
            _plan = None
            state["summary_md"] = summary_md
            state["notion_url"] = notion_url
            state["notion_error"] = notion_error
            state["took"] = took
            state["phase_times"] = times
            state["status"] = "done"
            state["step"] = ""
            state["progress"] = 100
            state["eta"] = 0
        notify(f"“{nice_title}” is ready — done in {fmt_took(took)}.")
    except _Cancelled:
        _finish_cancel(session)
    except Exception as e:
        with _lock:
            _plan = None
            state["status"] = "error"
            state["error"] = str(e)
            state["step"] = ""
        notify("Something went wrong while making your notes — open the app to see details.",
               sound="Basso")


# ----- importing existing audio (uploads + YouTube) ---------------------------
def convert_to_room_wav(src: Path, dst: Path):
    """Convert any audio/video file to the 16 kHz mono WAV the pipeline expects."""
    r = run_tracked(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(src),
         "-vn", "-ac", "1", "-ar", SAMPLE_RATE, str(dst)])
    if r.returncode != 0 or not dst.exists() or dst.stat().st_size < 1024:
        tail = [l for l in (r.stderr or "").strip().splitlines() if l.strip()]
        raise RuntimeError("couldn't read audio from that file — "
                           + (tail[-1] if tail else "the format may be unsupported or the file is empty"))


YT_SIGNIN_HINT = (
    "YouTube blocked the download — it asked to confirm you're not a robot. This often "
    "clears up if you try again in a minute. If it keeps happening, sign in to YouTube in "
    "Chrome or Safari, then re-launch the app with that browser set (see the README).")

# If a download comes back this much shorter than the video really is, it's damaged.
# Kept tight on purpose: on a 55-minute podcast, 1% is ~33 seconds — losing that much is
# hundreds of words. A real audio track never differs from the video by more than a second
# or two, so the 15s floor is generous headroom; the 60s ceiling stops the allowance from
# growing with length (on a 3-hour video, a bare 1% would quietly permit ~2 lost minutes).
DURATION_TOLERANCE_MIN_SEC = 15
DURATION_TOLERANCE_MAX_SEC = 60
DURATION_TOLERANCE_FRAC = 0.01  # 1%


def audio_duration(path: Path):
    """Length of an audio/video file in seconds (None if it can't be measured)."""
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
            capture_output=True, text=True, timeout=120)
        return float((r.stdout or "").strip())
    except Exception:
        return None


def _yt_media_files(session: Path):
    """The downloaded audio files — never the sidecar metadata yt-dlp writes alongside."""
    return sorted(p for p in session.glob("yt_source.*")
                  if not p.name.endswith((".info.json", ".part", ".ytdl", ".temp")))


def _yt_expected_duration(session: Path):
    """How long YouTube says the video is (from the .info.json sidecar)."""
    info = session / "yt_source.info.json"
    if not info.exists():
        return None
    try:
        d = json.loads(info.read_text(encoding="utf-8")).get("duration")
        return float(d) if d else None
    except Exception:
        return None


def download_youtube_audio(url: str, session: Path, safe_format: bool = False):
    """Download the best audio track from a link with yt-dlp.

    Returns (audio_file, expected_duration_seconds_or_None).

    Two protections against the silent-corruption failure that loses whole chunks of a
    transcript: we abort loudly if any piece of the audio can't be fetched (yt-dlp's
    default is to skip it and still exit 0 — leaving a full-length but garbled file),
    and we save the video's real duration so the caller can verify what we got.
    """
    for p in session.glob("yt_source.*"):  # clear anything from an earlier attempt
        try:
            p.unlink()
        except Exception:
            pass
    out_tmpl = str(session / "yt_source.%(ext)s")
    # Streams delivered in fragments (HLS) are the fragile ones; on a retry we ask for a
    # plain progressive audio stream instead.
    fmt = "ba[protocol!*=m3u8]/ba/b" if safe_format else "bestaudio/best"
    cmd = [sys.executable, "-m", "yt_dlp", "-f", fmt,
           "--no-playlist", "--no-progress",
           "--abort-on-unavailable-fragments",  # a missing piece must fail, never pass silently
           "--retries", "10", "--fragment-retries", "10",
           "--write-info-json",                 # gives us the true duration to check against
           "-o", out_tmpl]
    browser = os.environ.get("YT_COOKIES_BROWSER", "").strip()
    if browser:
        cmd += ["--cookies-from-browser", browser]
    cmd.append(url)
    r = run_tracked(cmd)
    files = _yt_media_files(session)
    if r.returncode != 0 or not files:
        err = (r.stderr or "").lower()
        if "sign in" in err or "not a bot" in err or "confirm" in err or "cookies" in err:
            raise RuntimeError(YT_SIGNIN_HINT)
        tail = [l for l in (r.stderr or "").strip().splitlines() if l.strip()]
        raise RuntimeError("couldn't download from that link — "
                           + (tail[-1] if tail else "check the link and your internet connection"))
    return files[0], _yt_expected_duration(session)


def _too_short(got, expected) -> bool:
    """Is the audio we ended up with meaningfully shorter than the real video?"""
    if not expected or expected <= 0 or got is None:
        return False  # nothing to compare against — don't block the user on a guess
    allowed = min(DURATION_TOLERANCE_MAX_SEC,
                  max(DURATION_TOLERANCE_MIN_SEC, expected * DURATION_TOLERANCE_FRAC))
    return (expected - got) > allowed


def _fetch_and_convert(session: Path, url: str, safe_format: bool):
    """One full YouTube attempt: download -> room.wav. Returns (got_sec, expected_sec)."""
    source_file, expected = download_youtube_audio(url, session, safe_format=safe_format)
    with _lock:
        state["step"] = "Preparing the audio…"
    room = session / "room.wav"
    convert_to_room_wav(Path(source_file), room)
    got = audio_duration(room)
    # The download and its metadata sidecar can be large — drop them now we have room.wav.
    for leftover in session.glob("yt_source.*"):
        try:
            leftover.unlink()
        except Exception:
            pass
    return got, expected


def process_import(session: Path, source_file: Path = None, youtube_url: str = None):
    """Turn an uploaded file or a YouTube link into room.wav, then run the pipeline."""
    clear_plan()  # the phase plan is built once room.wav exists (in process_session)
    try:
        if youtube_url:
            with _lock:
                state["step"] = "Downloading the audio from YouTube…"
            got, expected = _fetch_and_convert(session, youtube_url, safe_format=False)
            if _too_short(got, expected):
                # We got less audio than the video actually has. Rather than transcribe a
                # damaged file (and silently lose whole sections), try once more asking for
                # a sturdier stream.
                print(f"[!] Download looks short ({int(got or 0)}s of {int(expected)}s) — "
                      "retrying with a more reliable stream…")
                with _lock:
                    state["step"] = "The download looked incomplete — trying again…"
                got, expected = _fetch_and_convert(session, youtube_url, safe_format=True)
            if _too_short(got, expected):
                # Throw the damaged audio away. If we left it, the app would offer it back
                # as an "unfinished meeting" — one click from the incomplete transcript we
                # just refused to make.
                for leftover in list(session.glob("yt_source.*")) + [session / "room.wav"]:
                    try:
                        leftover.unlink()
                    except Exception:
                        pass
                raise RuntimeError(
                    f"only {fmt_took(int(got or 0))} of this {fmt_took(int(expected))} video "
                    "downloaded, so parts of it would be missing from your transcript. "
                    "Nothing was transcribed and the part-downloaded audio was deleted. "
                    "Please try the link again in a few minutes — or download the audio "
                    "yourself and use the Upload button, which always works.")
        else:
            with _lock:
                state["step"] = "Preparing the audio…"
            convert_to_room_wav(Path(source_file), session / "room.wav")
            try:  # the upload can be large — drop it now that we have room.wav
                Path(source_file).unlink()
            except Exception:
                pass
    except _Cancelled:
        _finish_cancel(session)
        return
    except Exception as e:
        with _lock:
            state["status"] = "error"
            state["error"] = str(e)
            state["step"] = ""
        notify("Something went wrong with your import — open the app to see details.",
               sound="Basso")
        return
    process_session(session)


# ----- self-update ------------------------------------------------------------
# The guiding rule here: if anything at all goes wrong, the app the user already has
# must keep working. Nothing installed is touched until a download has been fully
# checked, files are swapped in atomically, and there is always a way back.
_update_lock = threading.Lock()   # only ever one update at a time
_swap_lock = threading.Lock()     # held only during the (millisecond) file swap
_verified = False


def version_tuple(v):
    """Turn '1.10.2' into (1, 10, 2, 0) so versions sort properly — 1.10 is after 1.9.

    Padded to four parts so '1.1' and '1.1.0' count as the same version. Returns None
    if the text isn't a version we recognise at all."""
    if not isinstance(v, str):
        return None
    m = re.match(r"\s*v?(\d+(?:\.\d+)*)", v)
    if not m:
        return None
    parts = [int(p) for p in m.group(1).split(".")[:4]]
    return tuple(parts + [0] * (4 - len(parts)))


def is_newer(latest, current) -> bool:
    """Is `latest` genuinely a later version than `current`? Equal or older = no."""
    a = version_tuple(latest)
    if a is None:
        return False   # can't read the published version — never offer a mystery update
    b = version_tuple(current)
    if b is None:
        return True    # can't read our own version — offer it, so a damaged install
                       # can repair itself instead of being stuck forever
    return a > b


def _fetch(url: str, timeout: int, max_bytes: int) -> bytes:
    """Download something, refusing anything unreasonably large."""
    req = urllib.request.Request(url, headers={
        "User-Agent": "MeetingNotes", "Cache-Control": "no-cache", "Pragma": "no-cache"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        blob = resp.read(max_bytes + 1)
    if len(blob) > max_bytes:
        raise RuntimeError("the update download was unexpectedly large — nothing was changed")
    return blob


def fetch_latest_version(timeout: int = 15):
    """Ask the update site what the newest version is."""
    return json.loads(_fetch(f"{UPDATE_BASE_URL}/version.json", timeout, 64 * 1024).decode("utf-8"))


def _extract_staged(blob: bytes, dest: Path) -> Path:
    """Unpack the downloaded archive into `dest`, refusing anything suspicious.

    Written by hand rather than using tarfile's newer 'data' filter, because that isn't
    available on the older Python that ships with macOS. Anything that isn't a plain
    file or folder — a symlink, a device — stops the update rather than being skipped:
    in an app update it has no legitimate purpose."""
    dest.mkdir(parents=True, exist_ok=True)
    dest_res = dest.resolve()
    total = 0
    with tarfile.open(fileobj=BytesIO(blob), mode="r:gz") as tar:
        for m in tar.getmembers():
            name = m.name.replace("\\", "/")
            if m.issym() or m.islnk() or m.isdev() or m.ischr() or m.isblk() or m.isfifo():
                raise RuntimeError("the update archive contained an unexpected kind of file")
            if not (m.isfile() or m.isdir()):
                continue
            if name.startswith("/") or ".." in Path(name).parts:
                raise RuntimeError("the update archive contained an unsafe file path")
            parts = Path(name).parts
            if any(p in UPDATE_FATAL for p in parts):
                raise RuntimeError("the update archive contained files it should never "
                                   "contain — stopping to be safe")
            if any(p in ("__pycache__", ".DS_Store", ".git", ".github") for p in parts):
                continue
            target = (dest / name).resolve()
            if target != dest_res and dest_res not in target.parents:
                raise RuntimeError("the update archive contained an unsafe file path")
            if m.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            if m.size > MAX_MEMBER:
                raise RuntimeError("the update archive contained a file that is too large")
            total += m.size
            if total > MAX_TARBALL:
                raise RuntimeError("the update archive was unexpectedly large")
            src = tar.extractfile(m)
            if src is None:
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with open(target, "wb") as out:
                shutil.copyfileobj(src, out)
            # Set our own permissions rather than trusting whatever is in the archive.
            target.chmod(0o755 if target.suffix in (".sh", ".py", ".command") else 0o644)
    # GitHub wraps everything in one folder — find the app files inside it.
    if (dest / "version.json").exists():
        return dest
    for child in sorted(p for p in dest.iterdir() if p.is_dir()):
        if (child / "version.json").exists():
            return child
    raise RuntimeError("the update didn't contain the expected files")


def _smoke_test(root: Path):
    """Actually load the new app in a throwaway process before installing it.

    Checking that the files are valid Python only proves they parse. This proves they
    can actually be loaded — it catches a release that refers to something missing,
    which is the kind of fault that would otherwise leave the app unable to start."""
    try:
        r = subprocess.run([sys.executable, "-B", "-c", "import app"], cwd=str(root),
                           capture_output=True, text=True, timeout=90)
    except Exception:
        return   # couldn't run the check — don't block the update on it
    if r.returncode == 0:
        return
    err = (r.stderr or "").strip()
    m = re.search(r"ModuleNotFoundError: No module named '([\w.]+)'", err)
    if m:
        # A release may legitimately add a new component — but only one that the new
        # start_ui.sh actually installs on restart. Anything else really is missing,
        # and would leave the app unable to start, so it must not be installed.
        missing = m.group(1).split(".")[0]
        try:
            launcher = (root / "start_ui.sh").read_text(encoding="utf-8", errors="replace")
        except Exception:
            launcher = ""
        installed = set(re.findall(r'-c\s+["\']import\s+(\w+)', launcher))
        if missing in installed:
            return
    last = err.splitlines()[-1][:120] if err else "unknown problem"
    raise RuntimeError(f"the update doesn't load correctly ({last}) — nothing was changed")


def _verify_staged(root: Path, current: str) -> str:
    """Check a downloaded update thoroughly BEFORE anything installed is replaced.

    Returns the new version. Raises with a plain-English reason if anything is wrong —
    in which case the app on disk has not been touched at all."""
    for name in UPDATE_REQUIRED:
        f = root / name
        # A floor rather than 0: a truncated file or a download-failure placeholder is
        # small but not empty. version.json is legitimately tiny, so it gets its own.
        floor = 20 if name == "version.json" else 200
        if not f.is_file() or f.stat().st_size < floor:
            raise RuntimeError(f"the update is incomplete (missing {name}) — nothing was changed")
    try:
        new_version = str(json.loads((root / "version.json").read_text(encoding="utf-8"))["version"])
    except Exception:
        raise RuntimeError("the update's version file couldn't be read — nothing was changed")
    if not is_newer(new_version, current):
        raise RuntimeError("the update is still being published — please try again in a "
                           "few minutes. Nothing was changed")
    # The two files no Python check can validate, and both are fatal if broken.
    html = (root / "ui.html").read_text(encoding="utf-8", errors="replace")
    if len(html) < 5000 or "</html>" not in html.lower():
        raise RuntimeError("the update's app page looks damaged — nothing was changed")
    if "exec .venv/bin/python app.py" not in (root / "start_ui.sh").read_text(
            encoding="utf-8", errors="replace"):
        raise RuntimeError("the update's start-up file looks damaged — nothing was changed")
    # start_ui.sh carries the recovery logic that rescues a bad update, so a syntax
    # error in it would disable the very thing that puts problems right.
    for script in sorted(root.glob("*.sh")):
        try:
            r = subprocess.run(["bash", "-n", str(script)], capture_output=True,
                               text=True, timeout=30)
        except Exception:
            break   # no bash to check with — don't block the update on that
        if r.returncode != 0:
            raise RuntimeError(f"the update's {script.name} looks damaged — nothing was changed")
    # We install files, not folders. A release that adds one must fail loudly rather
    # than quietly leaving part of itself uninstalled.
    extra_dirs = [p.name for p in root.iterdir()
                  if p.is_dir() and p.name not in UPDATE_KEEP and not p.name.startswith(".")]
    if extra_dirs:
        raise RuntimeError(f"this update contains a folder ({extra_dirs[0]}) that this "
                           "version can't install — nothing was changed")
    # Every Python file must actually be valid Python, or the app won't start.
    pycdir = root / ".pyc-check"
    for py in sorted(root.glob("*.py")):
        try:
            py_compile.compile(str(py), cfile=str(pycdir / (py.stem + ".pyc")), doraise=True)
        except Exception as e:
            raise RuntimeError(f"the update looks damaged ({py.name}: {str(e)[:100]}) — "
                               "nothing was changed")
    shutil.rmtree(pycdir, ignore_errors=True)
    _smoke_test(root)
    return new_version


def _backup_current() -> Path:
    """Save the installed version so a bad update can be undone.

    Raises if a complete backup can't be made — we would far rather refuse to update
    than replace the app with no way back."""
    try:
        if shutil.disk_usage(HERE).free < MIN_FREE_BYTES:
            raise RuntimeError("there isn't enough free space on your Mac to update "
                               "safely — nothing was changed")
    except OSError:
        pass
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = BACKUPS / f"{APP_VERSION}-{stamp}"
    shutil.rmtree(backup, ignore_errors=True)
    backup.mkdir(parents=True, exist_ok=True)
    saved = set()
    for item in sorted(HERE.iterdir()):
        if item.name in UPDATE_KEEP or not item.is_file():
            continue
        shutil.copy2(item, backup / item.name)   # any failure propagates: no backup, no update
        saved.add(item.name)
    missing = [n for n in UPDATE_REQUIRED if n not in saved and (HERE / n).exists()]
    if missing:
        raise RuntimeError("couldn't save a full backup of your current version — "
                           "nothing was changed")
    # Written last: its presence is what marks the backup as complete and usable.
    (backup / "BACKUP_READY").write_text(APP_VERSION + "\n", encoding="utf-8")
    (UPD / "BACKUP_DIR").write_text(str(backup) + "\n", encoding="utf-8")
    return backup


def _prune_backups(keep: int = 3):
    try:
        old = sorted((p for p in BACKUPS.iterdir() if p.is_dir()),
                     key=lambda p: p.stat().st_mtime, reverse=True)[keep:]
        for p in old:
            shutil.rmtree(p, ignore_errors=True)
    except Exception:
        pass


RESTORE_SCRIPT = r"""#!/usr/bin/env bash
# Put the previous version of Meeting Notes back.
#
# Double-click this if an update caused a problem. Your recordings, your notes and
# your settings are not affected — only the app's own files are put back.
APP_DIR="__APP_DIR__"
pause_exit() { echo ""; read -n 1 -s -r -p "Press any key to close."; echo ""; exit "${1:-0}"; }
cd "$APP_DIR" 2>/dev/null || { echo "Can't find Meeting Notes at:"; echo "  $APP_DIR"; pause_exit 1; }

echo "Restoring the previous version of Meeting Notes"
echo "-----------------------------------------------"
BACKUP="$(cat .update/BACKUP_DIR 2>/dev/null || true)"
if [ -z "$BACKUP" ] || [ ! -f "$BACKUP/BACKUP_READY" ]; then
  echo ""
  echo "There is no saved previous version to restore."
  echo "You can download the app again from:"
  echo "  https://github.com/__REPO__"
  pause_exit 1
fi
echo ""
echo "This will put back version $(cat "$BACKUP/BACKUP_READY" 2>/dev/null | tr -d '\n')."
read -r -p "Go ahead? [y/N] " reply
case "$reply" in [Yy]*) ;; *) echo "Nothing was changed."; pause_exit 0 ;; esac

for f in "$BACKUP"/*; do
  [ -f "$f" ] || continue
  [ "$(basename "$f")" = "BACKUP_READY" ] && continue
  cp -p "$f" "./$(basename "$f")" || { echo "Couldn't restore $(basename "$f")."; pause_exit 1; }
done
chmod +x ./*.sh 2>/dev/null || true
rm -rf __pycache__
rm -f .update/IN_PROGRESS .update/SWAP_STARTED .update/PENDING_VERIFY
echo ""
echo "Done — your previous version is back."
read -r -p "Start Meeting Notes now? [Y/n] " go
case "$go" in [Nn]*) ;; *) open "$HOME/Desktop/Meeting Notes.command" 2>/dev/null || \
  open -a Terminal "$APP_DIR/start_ui.sh" 2>/dev/null || true ;; esac
pause_exit 0
"""


def _write_desktop_restore():
    """Put the way-back button on the Desktop, next to the app's own icon.

    It lives on the Desktop rather than inside the app folder because someone whose app
    just stopped working needs to find it without going hunting — and it must work with
    the app not running, which is exactly when it's needed."""
    body = RESTORE_SCRIPT.replace("__APP_DIR__", str(HERE)).replace("__REPO__", UPDATE_REPO)
    desktop = Path.home() / "Desktop"
    try:
        desktop.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    # The Desktop is where someone will look first; the app folder is the fallback, so
    # the way back always exists somewhere even on an unusual setup.
    for target in (desktop / "Restore Previous Version.command",
                   HERE / "Restore Previous Version.command"):
        try:
            target.write_text(body, encoding="utf-8")
            target.chmod(0o755)
            return target
        except Exception:
            continue
    return None


def _defer_exit(signum, frame):
    """Don't let the app die in the middle of swapping files.

    Closing the Terminal window sends a signal. If that landed between two file swaps
    the app would be left half-new; the swap takes milliseconds, so we simply wait."""
    _swap_lock.acquire(timeout=20)
    os._exit(0)


def _swap_in(root: Path, names):
    """Move the checked new files over the installed ones.

    os.replace is an atomic rename: each file is either entirely the old version or
    entirely the new one, never half-written. Staging sits inside the app folder so
    these are same-disk renames — no copying, and nothing to interrupt."""
    (UPD / "SWAP_STARTED").write_text("1", encoding="utf-8")
    with _swap_lock:
        for name in names:
            dest = HERE / name
            os.replace(root / name, dest)
            if dest.suffix in (".sh", ".py", ".command"):
                try:
                    dest.chmod(0o755)
                except Exception:
                    pass
    (UPD / "SWAP_STARTED").unlink(missing_ok=True)
    shutil.rmtree(HERE / "__pycache__", ignore_errors=True)  # never trust a stale .pyc


def _swap_order(root: Path):
    """Which files to install, and in what order.

    app.py and then version.json go last, so that if anything ever did interrupt the
    swap the app under-reports its version — it then offers the update again and puts
    itself right, rather than claiming to be updated when it isn't."""
    names = [p.name for p in sorted(root.iterdir())
             if p.is_file() and p.name not in UPDATE_KEEP and p.name not in ("app.py", "version.json")]
    return names + ["app.py", "version.json"]


RELAUNCH_SCRIPT = r"""#!/bin/bash
# Wait for the old app to let go of its port, then start the updated one.
cd "$1" || exit 1
for _ in $(seq 1 60); do
  /usr/bin/nc -z 127.0.0.1 __PORT__ >/dev/null 2>&1 || break
  sleep 0.5
done
sleep 1.5
if [ -f "$HOME/Desktop/Meeting Notes.command" ]; then
  open "$HOME/Desktop/Meeting Notes.command"
else
  nohup bash "$1/start_ui.sh" >/dev/null 2>&1 &
fi
"""


def _schedule_relaunch():
    """Start the updated app once this one has exited."""
    try:
        # The page that asked for the update reloads itself, so the restarted app must
        # not also open a second tab — two identical windows look like something broke.
        (UPD / "RELAUNCHED").write_text("1", encoding="utf-8")
    except Exception:
        pass
    try:
        helper = Path(tempfile.gettempdir()) / "meeting_notes_relaunch.sh"
        helper.write_text(RELAUNCH_SCRIPT.replace("__PORT__", str(PORT)), encoding="utf-8")
        helper.chmod(0o755)
        subprocess.Popen(["bash", str(helper), str(HERE)],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
        return True
    except Exception:
        return False


def port_is_open(port=PORT, host="127.0.0.1", timeout=0.25) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        return s.connect_ex((host, port)) == 0


def wait_for_port_free(seconds: float = 25.0) -> bool:
    """Give a previous copy time to finish closing — after an update it's on its way out."""
    deadline = time.time() + seconds
    while time.time() < deadline:
        if not port_is_open():
            return True
        time.sleep(0.4)
    return not port_is_open()


def _clear_pending_verify():
    """The page has loaded and talked to us, so this version demonstrably works."""
    global _verified
    if _verified:
        return
    _verified = True
    try:
        (UPD / "PENDING_VERIFY").unlink(missing_ok=True)
    except Exception:
        pass


def read_bad_version():
    """A version that was rolled back because it wouldn't start, if any.

    Recorded by start_ui.sh when it restores a backup, so the app doesn't offer the
    same broken version straight back and loop for ever."""
    try:
        p = UPD / "BAD_VERSION"
        return p.read_text(encoding="utf-8").strip() if p.exists() else ""
    except Exception:
        return ""


def note_startup_version():
    """On start-up, work out whether we've just been installed by an update.

    The version that RUNS an update is the old one, so it can't know how to leave a
    note for its successor. This lets the newly-installed version notice for itself —
    from the marker the swap leaves behind, or simply from the version having changed
    since last launch — so the confirmation appears however the update was done."""
    last_file = UPD / "LAST_RUN_VERSION"
    try:
        last = last_file.read_text(encoding="utf-8").strip() if last_file.exists() else ""
    except Exception:
        last = ""
    just_swapped = (UPD / "PENDING_VERIFY").exists()
    changed = bool(last) and last != APP_VERSION
    # A first-ever run has neither signal, so it correctly says nothing.
    if (just_swapped or changed) and not (UPD / "JUST_UPDATED").exists():
        try:
            info = json.loads((HERE / "version.json").read_text(encoding="utf-8"))
            UPD.mkdir(parents=True, exist_ok=True)
            (UPD / "JUST_UPDATED").write_text(json.dumps({
                "version": str(info.get("version") or APP_VERSION),
                "notes": [str(n) for n in (info.get("notes") or [])][:8],
            }), encoding="utf-8")
        except Exception:
            pass
    try:
        UPD.mkdir(parents=True, exist_ok=True)
        last_file.write_text(APP_VERSION, encoding="utf-8")
    except Exception:
        pass


def take_just_updated():
    """The 'you were just updated' note, if there is one. Read once, then cleared.

    Delivered from the server rather than the browser so that whichever tab the user
    ends up looking at shows the confirmation."""
    p = UPD / "JUST_UPDATED"
    if not p.exists():
        return None
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        d = None
    try:
        p.unlink()
    except Exception:
        pass
    if not d or not d.get("version"):
        return None
    return {"version": str(d["version"]), "notes": [str(n) for n in (d.get("notes") or [])][:8]}


def _verify_after_uptime(seconds: int = 120):
    """Safety net: count a version as working once it has simply run for a while.

    Normally the page loads and clears this within a second. But if the browser didn't
    open — say it wasn't running — a perfectly good version would otherwise look like a
    failed start, and after a few launches get rolled back for no reason."""
    def _later():
        time.sleep(seconds)
        _clear_pending_verify()
    threading.Thread(target=_later, daemon=True).start()


def perform_update():
    """Download, check and install the newest version.

    Returns (ok, message, version, swapped). `swapped` says whether files had already
    started being replaced when something went wrong — so we never tell the user
    "nothing was changed" if that isn't true."""
    swapped = False
    try:
        if not os.access(HERE, os.W_OK):
            return False, ("This Mac won't let the app update itself where it is. Try moving "
                           "the Meeting Notes folder out of Downloads. Nothing was changed."), None, False
        shutil.rmtree(STAGED, ignore_errors=True)
        UPD.mkdir(parents=True, exist_ok=True)
        BACKUPS.mkdir(parents=True, exist_ok=True)
        blob = _fetch(UPDATE_TARBALL_URL, 90, MAX_TARBALL)
        root = _extract_staged(blob, STAGED)
        new_version = _verify_staged(root, APP_VERSION)
        # Everything checked out — only now is anything installed touched.
        (UPD / "IN_PROGRESS").write_text("1", encoding="utf-8")
        _backup_current()
        _write_desktop_restore()   # the way back exists BEFORE we take any risk
        swapped = True
        _swap_in(root, _swap_order(root))
        # Leave a note for the restarted app to show: "Updated to 1.0.1 ✓", with what
        # changed. Without this the update finishes and leaves no trace at all, which
        # looks exactly like nothing happened.
        try:
            fresh = json.loads((HERE / "version.json").read_text(encoding="utf-8"))
            (UPD / "JUST_UPDATED").write_text(json.dumps({
                "version": str(fresh.get("version") or new_version),
                "notes": [str(n) for n in (fresh.get("notes") or [])][:8],
            }), encoding="utf-8")
        except Exception:
            pass
        (UPD / "PENDING_VERIFY").write_text("0", encoding="utf-8")
        (UPD / "IN_PROGRESS").unlink(missing_ok=True)
        shutil.rmtree(STAGED, ignore_errors=True)
        _prune_backups()
        return True, f"Updated to version {new_version}.", new_version, True
    except urllib.error.URLError as e:
        return False, ("Couldn't reach the update site — this usually just means you're not "
                       "online. Nothing was changed."), None, False
    except Exception as e:
        msg = str(e)
        if swapped:
            # Be honest: files were already being replaced.
            return False, ("The update was interrupted. Close this window and open Meeting Notes "
                           "again from your Desktop icon — it will put your previous version "
                           "back by itself."), None, True
        shutil.rmtree(STAGED, ignore_errors=True)
        # We never got as far as replacing anything, so clear the marker — otherwise the
        # Update button would refuse to try again until the app was restarted.
        try:
            (UPD / "IN_PROGRESS").unlink(missing_ok=True)
        except Exception:
            pass
        if "nothing was changed" not in msg.lower():
            msg = f"{msg} — nothing was changed."
        return False, (msg[:1].upper() + msg[1:] if msg else msg), None, False


# ----- routes -----------------------------------------------------------------
@app.route("/")
def index():
    return send_file(HERE / "ui.html")


@app.route("/api/devices")
def devices():
    devs = list_audio_inputs()
    return jsonify({"devices": devs, "default": default_mic_index(devs)})


@app.route("/api/start", methods=["POST"])
def api_start():
    data = request.get_json(silent=True) or {}
    title = (data.get("title") or "").strip()
    context = (data.get("context") or "").strip()
    mode = "room" if data.get("mode") == "room" else "mac"
    quality = "best" if data.get("quality") == "best" else "fast"
    diarize = data.get("diarize", True) is not False
    try:
        mic = int(data.get("mic", 0))
    except (TypeError, ValueError):
        mic = 0

    with _lock:
        if state["status"] == "recording":
            return jsonify({"ok": False, "error": "already recording"}), 400
        if mode == "mac" and not AUDIOTEE.exists():
            return jsonify({"ok": False, "error": "AudioTee not built — run setup.sh"}), 500
        _cancel.clear()  # fresh run — forget any earlier cancel
        _rec_health["files"].clear()
        _rec_health["warning"] = None
        session = new_session(title)
        (session / "meta.json").write_text(
            json.dumps({"mode": mode, "title": title, "context": context,
                        "quality": quality, "diarize": diarize}),
            encoding="utf-8")
        _procs.clear()
        _procs.update(start_recording(session, mic, mode))
        state.update(status="recording", session=str(session), title=title, mode=mode,
                     started_at=time.time(), step="", took=None, phase_times=None, summary_md=None,
                     notion_url=None, notion_error=None, error=None)
    return jsonify({"ok": True})


@app.route("/api/stop", methods=["POST"])
def api_stop():
    with _lock:
        if state["status"] != "recording":
            return jsonify({"ok": False, "error": "not recording"}), 400
        stop_recording(_procs)
        _procs.clear()
        session = Path(state["session"])
        state.update(status="processing", started_at=None, proc_started_at=time.time(),
                     step="Finishing the recording…", progress=0, eta=None)
    threading.Thread(target=_pipeline_worker, args=(process_session, session),
                     daemon=True).start()
    return jsonify({"ok": True})


@app.route("/api/cancel", methods=["POST"])
def api_cancel():
    """Discard the current recording or processing run — nothing is transcribed,
    summarized, or kept, so no Claude credits get used."""
    global _plan
    with _lock:
        st = state["status"]
        session = Path(state["session"]) if state["session"] else None
        proc = _pipeline["proc"]
        if st == "recording":
            stop_recording(_procs)
            _procs.clear()
            _plan = None
            state.update(status="idle", session=None, title=None, started_at=None,
                         proc_started_at=None, step="", progress=0, eta=None,
                         took=None, phase_times=None, summary_md=None, notion_url=None, notion_error=None, error=None)
        elif st == "processing":
            _cancel.set()
            state["step"] = "Cancelling…"
        else:
            return jsonify({"ok": False, "error": "nothing to cancel"}), 400
    if st == "recording":
        if session:
            shutil.rmtree(session, ignore_errors=True)
    else:
        # kill the step that's running now; the worker thread cleans up and goes idle
        try:
            if proc and proc.poll() is None:
                proc.kill()
        except Exception:
            pass
    return jsonify({"ok": True})


@app.route("/api/upload", methods=["POST"])
def api_upload():
    """Transcribe & summarize an audio/video file the user already has."""
    with _lock:
        if state["status"] in ("recording", "processing"):
            return jsonify({"ok": False, "error": "busy — finish the current meeting first"}), 400
    f = request.files.get("file")
    if not f or not f.filename:
        return jsonify({"ok": False, "error": "no file was received"}), 400
    title = (request.form.get("title") or "").strip()
    context = (request.form.get("context") or "").strip()
    quality = "best" if request.form.get("quality") == "best" else "fast"
    diarize = request.form.get("diarize", "true").lower() not in ("false", "0", "no")
    if not title:
        title = Path(f.filename).stem[:60]
    session = new_session(title)
    ext = Path(f.filename).suffix.lower()[:8] or ".bin"
    src = session / ("source" + ext)
    try:
        f.save(str(src))
    except Exception as e:
        return jsonify({"ok": False, "error": f"could not save the file: {e}"}), 500
    (session / "meta.json").write_text(
        json.dumps({"mode": "room", "title": title, "context": context,
                    "source": "upload", "quality": quality, "diarize": diarize}),
        encoding="utf-8")
    with _lock:
        _cancel.clear()  # fresh run — forget any earlier cancel
        state.update(status="processing", session=str(session), title=title, mode="room",
                     started_at=None, proc_started_at=time.time(), step="Preparing the audio…",
                     progress=0, eta=None, took=None, phase_times=None, summary_md=None,
                     notion_url=None, notion_error=None, error=None)
    threading.Thread(target=_pipeline_worker, args=(process_import, session),
                     kwargs={"source_file": src}, daemon=True).start()
    return jsonify({"ok": True})


@app.route("/api/youtube", methods=["POST"])
def api_youtube():
    """Transcribe & summarize the audio from a pasted YouTube (or similar) link."""
    data = request.get_json(silent=True) or {}
    url = (data.get("url") or "").strip()
    title = (data.get("title") or "").strip()
    context = (data.get("context") or "").strip()
    quality = "best" if data.get("quality") == "best" else "fast"
    diarize = data.get("diarize", True) is not False
    if not (url.startswith("http://") or url.startswith("https://")):
        return jsonify({"ok": False, "error": "paste a full link starting with http"}), 400
    if importlib.util.find_spec("yt_dlp") is None:
        return jsonify({"ok": False, "error": "the link downloader isn't installed yet — "
                        "quit the app and run start_ui.sh once to add it"}), 400
    with _lock:
        if state["status"] in ("recording", "processing"):
            return jsonify({"ok": False, "error": "busy — finish the current meeting first"}), 400
        if not title:
            title = "YouTube audio"
        session = new_session(title)
        (session / "meta.json").write_text(
            json.dumps({"mode": "room", "title": title, "context": context,
                        "source": "youtube", "url": url,
                        "quality": quality, "diarize": diarize}),
            encoding="utf-8")
        _cancel.clear()  # fresh run — forget any earlier cancel
        state.update(status="processing", session=str(session), title=title, mode="room",
                     started_at=None, proc_started_at=time.time(),
                     step="Downloading the audio from YouTube…", progress=0, eta=None,
                     took=None, phase_times=None, summary_md=None, notion_url=None, notion_error=None, error=None)
    threading.Thread(target=_pipeline_worker, args=(process_import, session),
                     kwargs={"youtube_url": url}, daemon=True).start()
    return jsonify({"ok": True})


@app.route("/api/status")
def api_status():
    _clear_pending_verify()  # the page is talking to us, so this version works
    now = time.time()
    with _lock:
        s = dict(state)
        plan = dict(_plan) if _plan else None  # shallow snapshot; phases list is read-only
    # Compute progress OUTSIDE the lock — it may read diar_progress.json from disk, and
    # we don't want that I/O to block the worker thread's phase transitions.
    progress, eta = compute_progress(s, plan, now)
    if s["status"] == "processing":
        with _lock:
            progress = max(progress, state.get("progress", 0))  # never go backwards
            state["progress"] = progress
    s["progress"] = progress
    s["eta"] = eta
    s["elapsed"] = int(now - s["started_at"]) if (s["status"] == "recording" and s["started_at"]) else 0
    s["session_name"] = Path(s["session"]).name if s["session"] else None
    s["notion_configured"] = notion_configured()
    s["version"] = APP_VERSION
    s["just_updated"] = take_just_updated()
    s["cost"] = read_cost(Path(s["session"])) if s["session"] else None
    s["billed"] = read_billing(Path(s["session"])) if s["session"] else None
    s["coverage"] = read_coverage(Path(s["session"])) if s["session"] else None
    if s["status"] == "recording" and s["session"]:
        try:
            warn = recording_health(Path(s["session"]), s.get("mode") or "mac",
                                    s.get("started_at"))
        except Exception:
            warn = None
        if warn:
            _rec_health["warning"] = warn  # sticky until the next recording starts
    s["rec_warning"] = _rec_health["warning"] if s["status"] == "recording" else None
    return jsonify(s)


@app.route("/api/new", methods=["POST"])
def api_new():
    with _lock:
        if state["status"] in ("recording", "processing"):
            return jsonify({"ok": False}), 400
        state.update(status="idle", session=None, title=None, step="", progress=0, eta=None,
                     took=None, phase_times=None, summary_md=None, notion_url=None, notion_error=None, error=None)
    return jsonify({"ok": True})


@app.route("/api/meetings")
def api_meetings():
    items = []
    if BASE.exists():
        for p in sorted(BASE.iterdir(), key=lambda x: x.name, reverse=True):
            if p.is_dir() and (p / "summary.md").exists():
                try:  # prefer the meta.json title (covers auto-named meetings)
                    title = (json.loads((p / "meta.json").read_text(encoding="utf-8"))
                             .get("title") or "").strip()
                except Exception:
                    title = ""
                items.append({"name": p.name, "title": title})
    return jsonify({"meetings": items[:50]})


@app.route("/api/unfinished")
def api_unfinished():
    """Meetings that have audio but never got their notes (e.g. laptop shut down
    mid-processing) — offered for resume in the UI."""
    with _lock:
        active = state["session"]
    items = []
    if BASE.exists():
        for p in sorted(BASE.iterdir(), key=lambda x: x.name, reverse=True):
            if (p.is_dir() and str(p) != active
                    and not (p / "summary.md").exists() and has_audio(p)):
                items.append({"name": p.name})
    return jsonify({"unfinished": items[:5]})


@app.route("/api/resume", methods=["POST"])
def api_resume():
    """Pick up an unfinished meeting where it left off (skips finished steps)."""
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    folder = BASE / name
    if not name or not folder.resolve().is_relative_to(BASE.resolve()) or not folder.is_dir():
        return jsonify({"ok": False, "error": "meeting not found"}), 404
    if not has_audio(folder):
        return jsonify({"ok": False, "error": "no audio found for this meeting"}), 404
    with _lock:
        if state["status"] in ("recording", "processing"):
            return jsonify({"ok": False, "error": "busy — finish the current meeting first"}), 400
        _cancel.clear()
        try:
            title = json.loads((folder / "meta.json").read_text()).get("title") or ""
        except Exception:
            title = ""
        state.update(status="processing", session=str(folder), title=title,
                     started_at=None, proc_started_at=time.time(),
                     step="Resuming this meeting…", progress=0, eta=None, took=None, phase_times=None, summary_md=None,
                     notion_url=None, notion_error=None, error=None)
    threading.Thread(target=_pipeline_worker, args=(process_session, folder),
                     daemon=True).start()
    return jsonify({"ok": True})


@app.route("/api/discard", methods=["POST"])
def api_discard():
    """Throw away an unfinished meeting the user doesn't want."""
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    folder = BASE / name
    if not name or not folder.resolve().is_relative_to(BASE.resolve()) or not folder.is_dir():
        return jsonify({"ok": False, "error": "meeting not found"}), 404
    with _lock:
        if state["session"] == str(folder) and state["status"] in ("recording", "processing"):
            return jsonify({"ok": False, "error": "this meeting is in progress — cancel it instead"}), 400
    shutil.rmtree(folder, ignore_errors=True)
    return jsonify({"ok": True})


@app.route("/api/meeting/<name>")
def api_meeting(name):
    folder = BASE / name
    if not folder.resolve().is_relative_to(BASE.resolve()) or not folder.is_dir():
        return jsonify({"ok": False}), 404
    summary = folder / "summary.md"
    try:
        meta = json.loads((folder / "meta.json").read_text(encoding="utf-8"))
    except Exception:
        meta = {}
    return jsonify({
        "ok": True, "name": name,
        "summary_md": summary.read_text(encoding="utf-8") if summary.exists() else "",
        "folder": str(folder),
        "cost": read_cost(folder),
        "billed": read_billing(folder),
        "coverage": read_coverage(folder),
        "took": meta.get("took"),
        "phase_times": meta.get("phase_times"),
    })


@app.route("/api/spend")
def api_spend():
    """Running total of what this app has spent on Claude (sum of every cost.json)."""
    total, n = 0.0, 0
    if BASE.exists():
        for p in BASE.iterdir():
            if p.is_dir():
                c = read_cost(p)
                if c:
                    total += c["cost_usd"]
                    n += 1
    return jsonify({"total_usd": round(total, 2), "meetings": n})


@app.route("/api/transcript/<name>")
def api_transcript(name):
    folder = BASE / name
    if not folder.resolve().is_relative_to(BASE.resolve()) or not folder.is_dir():
        return jsonify({"ok": False}), 404
    refined = folder / "transcript-refined.txt"
    raw = folder / "transcript.txt"
    f = refined if refined.exists() else raw
    return jsonify({
        "ok": True,
        "transcript": f.read_text(encoding="utf-8") if f.exists() else "",
        "refined": refined.exists(),
    })


def export_filename(session_name: str) -> str:
    """A friendly file name for an exported recording, from the session folder name."""
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})_(\d{2})(\d{2})(\d{2})(?:_(.+))?$", session_name)
    if not m:
        return session_name
    title = (m.group(7) or "").replace("_", " ").strip() or "Meeting"
    return f"{title} — {m.group(1)}-{m.group(2)}-{m.group(3)} {m.group(4)}.{m.group(5)}"


@app.route("/api/save_audio", methods=["POST"])
def api_save_audio():
    """Export a meeting's audio to Desktop/'Meeting & Calls audio files' as one .m4a."""
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    folder = BASE / name
    if not name or not folder.resolve().is_relative_to(BASE.resolve()) or not folder.is_dir():
        return jsonify({"ok": False, "error": "meeting not found"}), 404

    # room.wav = single-stream recordings & imports; you/them = call on this Mac
    room = folder / "room.wav"
    if room.exists() and room.stat().st_size > 2000:
        inputs = [room]
    else:
        inputs = [p for p in (folder / "you.wav", folder / "them.wav")
                  if p.exists() and p.stat().st_size > 2000]
    if not inputs:
        return jsonify({"ok": False, "error": "no audio found for this meeting — "
                        "it may have been recorded before this feature was added, "
                        "or the files were deleted"}), 404

    AUDIO_EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    base_name = export_filename(name)
    out = AUDIO_EXPORT_DIR / f"{base_name}.m4a"
    n = 2
    while out.exists():  # never overwrite an earlier export
        out = AUDIO_EXPORT_DIR / f"{base_name} ({n}).m4a"
        n += 1

    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
    for p in inputs:
        cmd += ["-i", str(p)]
    if len(inputs) == 2:  # merge your mic + the call into one recording
        cmd += ["-filter_complex", "amix=inputs=2:duration=longest"]
    cmd += ["-c:a", "aac", "-b:a", "96k", str(out)]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    except subprocess.TimeoutExpired:
        return jsonify({"ok": False, "error": "converting the audio took too long"}), 500
    if r.returncode != 0 or not out.exists():
        return jsonify({"ok": False, "error": "could not convert the audio"}), 500

    subprocess.run(["open", "-R", str(out)], check=False)  # show it in Finder
    return jsonify({"ok": True, "file": out.name})


@app.route("/api/transcript_docx/<name>")
def api_transcript_docx(name):
    """Download the transcript as a Word document, styled like the app's view."""
    folder = BASE / name
    if not folder.resolve().is_relative_to(BASE.resolve()) or not folder.is_dir():
        return jsonify({"ok": False}), 404
    refined = folder / "transcript-refined.txt"
    raw = folder / "transcript.txt"
    f = refined if refined.exists() else raw
    if not f.exists():
        return jsonify({"ok": False, "error": "no transcript for this meeting"}), 404

    try:
        title = (json.loads((folder / "meta.json").read_text(encoding="utf-8"))
                 .get("title") or "").strip()
    except Exception:
        title = ""
    pretty = export_filename(name)          # "Team Sync — 2026-07-10 14.30"
    if not title:
        title = pretty.split(" — ")[0]
    when = pretty.split(" — ")[-1] if " — " in pretty else ""
    subtitle = (f"{when} · Full transcript" if when else "Full transcript") \
        + ("" if refined.exists() else " (raw)")

    data = docx_export.transcript_docx(title, subtitle, f.read_text(encoding="utf-8"))
    return send_file(
        BytesIO(data), as_attachment=True,
        download_name=f"Transcript — {pretty}.docx",
        mimetype="application/vnd.openxmlformats-officedocument.wordprocessingml.document")


@app.route("/api/reveal", methods=["POST"])
def api_reveal():
    data = request.get_json(silent=True) or {}
    folder = BASE / data.get("name", "")
    if folder.resolve().is_relative_to(BASE.resolve()) and folder.is_dir():
        subprocess.run(["open", str(folder)], check=False)
        return jsonify({"ok": True})
    return jsonify({"ok": False}), 404


@app.route("/api/quit", methods=["POST"])
def api_quit():
    """Stop the app (so the user can quit from the page, no Terminal needed)."""
    with _lock:
        if state["status"] == "recording":
            stop_recording(_procs)
            _procs.clear()

    def _bye():
        time.sleep(0.4)
        os._exit(0)
    threading.Thread(target=_bye, daemon=True).start()
    return jsonify({"ok": True})


@app.route("/api/update_check")
def api_update_check():
    """Is there a newer version? Never raises — being offline just means 'don't know'."""
    try:
        latest = fetch_latest_version()
        version = str(latest.get("version") or "")
        notes = [str(n) for n in (latest.get("notes") or [])][:8]
        released = str(latest.get("released") or "")
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return jsonify({"ok": False, "current": APP_VERSION, "not_published": True,
                            "reason": "the update site isn't published yet"})
        return jsonify({"ok": False, "current": APP_VERSION, "reason": f"error {e.code}"})
    except Exception as e:
        # Just the bare reason — the page writes the sentence, so it never reads like
        # two half-messages stuck together.
        return jsonify({"ok": False, "current": APP_VERSION,
                        "reason": str(getattr(e, "reason", e))[:120]})
    available = is_newer(version, APP_VERSION)
    blocked = read_bad_version()
    if available and blocked and version_tuple(version) == version_tuple(blocked):
        # This exact version was rolled back because it wouldn't start. Offering it
        # again would just loop: install, fail, restore, offer, install…
        return jsonify({"ok": True, "current": APP_VERSION, "latest": version,
                        "update_available": False, "released": released, "notes": notes,
                        "blocked": version,
                        "reason": f"version {version} was put back because it didn't start "
                                  f"properly on this Mac"})
    return jsonify({
        "ok": True,
        "current": APP_VERSION,
        "latest": version,
        "update_available": available,
        "released": released,
        "notes": notes,
    })


@app.route("/api/update", methods=["POST"])
def api_update():
    """Install the newest version, then restart the app."""
    # A page on another site can't set a custom header without asking us first, and we
    # never agree — so this stops anything but our own page from triggering an update.
    if request.headers.get("X-Meeting-Notes") != "1":
        return jsonify({"ok": False, "error": "unexpected request"}), 400
    if (UPD / "IN_PROGRESS").exists():
        return jsonify({"ok": False, "error": "A previous update didn't finish. Quit the app "
                                              "and start it again — it will put things right "
                                              "by itself."}), 400
    with _lock:
        if state["status"] in ("recording", "processing"):
            return jsonify({"ok": False, "error": "Please wait until the current meeting has "
                                                  "finished before updating."}), 400
    if not _update_lock.acquire(blocking=False):
        return jsonify({"ok": False, "error": "An update is already running — please wait."}), 400
    try:
        ok, message, new_version, swapped = perform_update()
        if not ok:
            return jsonify({"ok": False, "error": message, "swapped": swapped}), 400
        # Downloading can take a while — make sure a meeting didn't start meanwhile.
        with _lock:
            if state["status"] == "recording":
                stop_recording(_procs)
                _procs.clear()
        relaunching = _schedule_relaunch()

        def _bye():
            time.sleep(0.6)  # let this response reach the page first
            os._exit(0)
        threading.Thread(target=_bye, daemon=True).start()
        return jsonify({"ok": True, "version": new_version, "message": message,
                        "relaunching": relaunching})
    finally:
        _update_lock.release()


# Which browser to open the app in. Override with MEETING_NOTES_BROWSER in .env
# (e.g. MEETING_NOTES_BROWSER="Safari") if you ever want a different one.
BROWSER_APP = os.environ.get("MEETING_NOTES_BROWSER", "Google Chrome")


def open_app_page():
    """Open the app's page in the preferred browser, falling back to the default."""
    url = f"http://127.0.0.1:{PORT}"
    try:
        r = subprocess.run(["open", "-a", BROWSER_APP, url],
                           capture_output=True, timeout=10)
        if r.returncode == 0:
            return
    except Exception:
        pass
    webbrowser.open(url)  # preferred browser not found — use the default one


def _open_browser():
    time.sleep(1.0)
    open_app_page()


if __name__ == "__main__":
    try:
        # Make the Desktop export folder right away, so it's easy to find.
        AUDIO_EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    print("\n  Meeting Recorder is running.")
    print("  Open this in your browser if it didn't open automatically:\n")
    print(f"      http://127.0.0.1:{PORT}\n")
    print("  Use the 'Quit app' button on the page (or Control-C here) to quit.\n")
    note_startup_version()   # did an update just install us? then say so on the page
    # Don't let a closing Terminal window interrupt a file swap mid-update.
    for _sig in (signal.SIGHUP, signal.SIGTERM):
        try:
            signal.signal(_sig, _defer_exit)
        except (ValueError, OSError, AttributeError):
            pass
    # Something already on our port is usually the previous copy shutting down after an
    # update. Wait for it, rather than assuming another copy is running — that assumption
    # would leave the page pointing at a server that no longer exists.
    if port_is_open():
        print("  Waiting for the previous copy to finish closing…")
        if not wait_for_port_free(25):
            print("  Meeting Notes is already running — opening it.")
            # A copy IS serving, so this version plainly works. Clear the first-run
            # check, or repeatedly double-clicking the icon would eventually be mistaken
            # for a version that won't start and trigger a needless rollback.
            _clear_pending_verify()
            open_app_page()
            sys.exit(0)
    # After an update the existing page reloads itself onto the new version, so don't
    # open a second tab on top of it.
    _relaunched = UPD / "RELAUNCHED"
    if _relaunched.exists():
        try:
            _relaunched.unlink()
        except Exception:
            pass
        print("  Updated — your existing Meeting Notes tab will refresh itself.")
    else:
        threading.Thread(target=_open_browser, daemon=True).start()
    _verify_after_uptime()   # don't roll back a version that's actually running fine
    try:
        app.run(host="127.0.0.1", port=PORT, threaded=True)
    except OSError:
        # Lost a race with another copy starting at the same moment — just show it.
        open_app_page()
