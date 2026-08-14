#!/usr/bin/env python3
"""
Meeting Recorder — Piece 3: summary + AI-refined transcript, via the Claude API.

Reads a session's transcript (made by Piece 2) and produces, in the same folder:
  summary.md             – TL;DR, summary, action items, and headings that adapt
                           to what the meeting was actually about
  transcript-refined.txt – the full transcript, AI-cleaned for accuracy/readability

This is the only step that uses a paid online service (the Claude API). It reads
your API key from a local .env file (set up by summarize.sh). Transcription
(Piece 2) stays free and local.

Usage:
  bash summarize.sh                 # summarize your most recent transcript
  bash summarize.sh "/path/to/session-folder"
  bash summarize.sh --model claude-sonnet-4-6   # cheaper; or claude-haiku-4-5 (cheapest)
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

DEFAULT_BASE = Path.home() / "MeetingNotes"
# Best quality by default; set SUMMARY_MODEL in .env (e.g. claude-sonnet-4-6 for ~half
# the cost, claude-haiku-4-5 for ~a fifth) or pass --model to spend less.
DEFAULT_MODEL = os.environ.get("SUMMARY_MODEL", "claude-opus-4-8").strip() or "claude-opus-4-8"

# Claude API prices in US$ per 1,000,000 tokens (input, output). Used to estimate
# each meeting's cost from the actual token usage the API reports back.
PRICES = {
    "opus":   (5.0, 25.0),
    "sonnet": (3.0, 15.0),
    "haiku":  (1.0, 5.0),
}


def price_for(model: str):
    m = (model or "").lower()
    for key, rates in PRICES.items():
        if key in m:
            return rates
    return PRICES["opus"]  # unknown model: assume the priciest so we never under-report

SUMMARY_PROMPT = """You are an expert meeting-notes writer. Below is a transcript of a meeting.
Lines are labelled "You" (the person who recorded the meeting) and "Them" (the other
participants), with [mm:ss] timestamps.

Write clear, useful meeting notes in Markdown. Structure:
- The VERY FIRST line must be exactly `TITLE: ...` — a specific, descriptive 3–8 word
  name for this meeting (e.g. "Q3 budget review with finance", never something generic
  like "Meeting notes"). Write it in the meeting's own language. This line is used as
  the note's title and is removed from the body.
- Then start the notes with a one-sentence **TL;DR**.
- A `## Summary` section: the key points discussed, in tight bullet points.
- A `## Action items` section: concrete to-dos as a checklist (`- [ ] ...`), each noting
  who owns it (You / Them / a name) and any due date mentioned. If there are none, write
  "None identified."
- Then add ANY other `##` sections that genuinely fit this meeting and would be useful —
  for example Decisions, Open questions, Risks, Next steps, Key numbers. Choose sections
  that match the actual content; do not force ones that don't apply.

Rules: Be faithful to the transcript — never invent facts, names, or commitments. If
background is provided below, use it to get names, roles, and terminology right and to
attribute speakers, but never contradict what was actually said. Keep it concise and
skimmable. Output ONLY the Markdown notes, with no preamble or sign-off.
"""

REFINE_PROMPT = """Below is a raw speech-to-text transcript of a meeting. Because it was
auto-generated, it contains errors. Produce a cleaned-up, accurate, readable version.

Do: fix obvious mis-transcriptions, spelling, capitalisation and punctuation; join words
into natural sentences; remove pure filler ("um", "uh") and clearly spurious artifacts
(e.g. a repeated "Thank you." over music or silence). Keep the [mm:ss] timestamps and the
speaker labels (You / Them, or Speaker 1 / Speaker 2 …).

Do NOT: add, summarise, paraphrase heavily, or invent any content. Only clean up what is
actually there. If a passage is genuinely unintelligible, keep your best guess and mark it
with [unclear].

If background below names the participants, replace the generic speaker labels with the
correct names where you can confidently tell who is who; otherwise keep the labels as they
are. Use the background to fix names, spellings, and jargon — never to invent content.

Output ONLY the cleaned transcript, with no preamble or commentary.
"""


def find_latest_session(base: Path):
    """Most recently modified folder that has a transcript."""
    if not base.exists():
        return None
    candidates = [
        p for p in base.iterdir()
        if p.is_dir() and ((p / "transcript.json").exists() or (p / "transcript.txt").exists())
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


def fmt_ts(seconds: float) -> str:
    seconds = max(0, int(seconds))
    return f"{seconds // 60:02d}:{seconds % 60:02d}"


def read_transcript(session: Path) -> str:
    """Build a clean labelled transcript string from transcript.json (or .txt fallback)."""
    j = session / "transcript.json"
    if j.exists():
        rows = json.loads(j.read_text(encoding="utf-8"))
        return "\n".join(
            f"[{fmt_ts(r['start'])}] {r['speaker']}: {r['text']}" for r in rows
        )
    t = session / "transcript.txt"
    if t.exists():
        return t.read_text(encoding="utf-8")
    return ""


def read_context(session: Path) -> str:
    meta = session / "meta.json"
    if meta.exists():
        try:
            return (json.loads(meta.read_text(encoding="utf-8")).get("context") or "").strip()
        except Exception:
            return ""
    return ""


def text_of(message) -> str:
    return "".join(b.text for b in message.content if b.type == "text").strip()


def run_claude_code(prompt: str) -> str:
    """Run one prompt through the Claude Code app (billed to the user's Claude
    subscription, not API credits) and return the response text."""
    r = subprocess.run(["claude", "-p", "--output-format", "text"],
                       input=prompt, capture_output=True, text=True, timeout=3600)
    out = (r.stdout or "").strip()
    if r.returncode != 0 or not out:
        raise RuntimeError((r.stderr or out or "no output from Claude Code").strip()[:500])
    return out


def summarize_via_subscription(session: Path, transcript: str, ctx_block: str, args) -> int:
    """Make the notes with Claude Code (the user's Claude subscription)."""
    if shutil.which("claude") is None:
        print("ERROR: USE_CLAUDE_SUBSCRIPTION is on, but the Claude Code app isn't "
              "installed. Run:  bash use_subscription.sh  to set it up — or delete the "
              "USE_CLAUDE_SUBSCRIPTION line from .env to go back to API credits.",
              file=sys.stderr)
        return 1
    print("[*] Using your Claude subscription (via Claude Code) — no API credits used.")
    try:
        print("[*] Writing the summary and action items...")
        raw = run_claude_code(SUMMARY_PROMPT + ctx_block + "\nTRANSCRIPT:\n" + transcript)
        summary_md, auto_title = split_title(raw)
        apply_auto_title(session, auto_title)
        (session / "summary.md").write_text(summary_md + "\n", encoding="utf-8")
        print(f"    saved: {session / 'summary.md'}")

        if not args.no_refine:
            print("[*] AI-refining the full transcript (this is the longer step)...")
            refined = run_claude_code(REFINE_PROMPT + ctx_block + "\nTRANSCRIPT:\n" + transcript)
            (session / "transcript-refined.txt").write_text(refined + "\n", encoding="utf-8")
            print(f"    saved: {session / 'transcript-refined.txt'}")
    except subprocess.TimeoutExpired:
        print("\nERROR: Claude Code took too long — try again.", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"\nERROR from Claude Code: {e}\n\nIf you're not logged in (or your "
              "subscription's usage window is used up), run: bash use_subscription.sh",
              file=sys.stderr)
        return 1
    # No cost.json on purpose: subscription use isn't billed per meeting.
    # Leave a plain marker so the app can say, on this meeting's results,
    # "made with your subscription — no API credits used" (removes all doubt).
    try:
        (session / "billing.txt").write_text("subscription", encoding="utf-8")
    except Exception:
        pass
    print("\n[OK] Done — made with your Claude subscription (no API credits used).")
    print("\n----- summary preview -----")
    print("\n".join(summary_md.splitlines()[:14]))
    print("---------------------------")
    return 0


def split_title(md: str):
    """Pull the 'TITLE: ...' first line (that we asked Claude for) out of the notes."""
    lines = md.splitlines()
    title = ""
    for i, line in enumerate(lines[:3]):  # tolerate a stray blank line before it
        m = re.match(r"^\s*[#>*\s]*TITLE\s*[:：]\s*(.+?)\s*$", line, re.IGNORECASE)
        if m:
            title = m.group(1).strip().strip('*"“”\'.').strip()[:80]
            del lines[i]
            break
    return "\n".join(lines).lstrip("\n"), title


def apply_auto_title(session: Path, title: str):
    """If the user didn't name the meeting, use Claude's title for it."""
    if not title:
        return
    meta_p = session / "meta.json"
    try:
        meta = json.loads(meta_p.read_text(encoding="utf-8")) if meta_p.exists() else {}
    except Exception:
        meta = {}
    if (meta.get("title") or "").strip():
        return  # the user named it themselves — their name wins
    meta["title"] = title
    meta["auto_titled"] = True
    meta_p.write_text(json.dumps(meta), encoding="utf-8")
    print(f"[*] Untitled meeting — named it: {title}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Summarise a meeting transcript and produce an AI-refined transcript.")
    parser.add_argument("session", nargs="?", default=None,
                        help="path to a session folder (default: your most recent transcript)")
    parser.add_argument("--model", default=DEFAULT_MODEL,
                        help=f"Claude model (default {DEFAULT_MODEL}; "
                             f"cheaper: claude-sonnet-4-6, cheapest: claude-haiku-4-5)")
    parser.add_argument("--base", default=str(DEFAULT_BASE),
                        help="where recordings are stored (default: ~/MeetingNotes)")
    parser.add_argument("--no-refine", action="store_true",
                        help="only make the summary, skip the AI-refined transcript (cheaper)")
    args = parser.parse_args()

    if args.session:
        session = Path(args.session).expanduser()
        if not session.is_dir():
            print(f"ERROR: that folder doesn't exist: {session}", file=sys.stderr)
            return 1
    else:
        session = find_latest_session(Path(args.base).expanduser())
        if session is None:
            print(f"ERROR: no transcripts found in {args.base}. Run transcribe.sh first.",
                  file=sys.stderr)
            return 1

    transcript = read_transcript(session).strip()
    if not transcript:
        print(f"ERROR: no transcript text found in {session}.", file=sys.stderr)
        return 1

    context = read_context(session)
    ctx_block = (f"\n\nBACKGROUND (provided by the user before the meeting):\n{context}\n"
                 if context else "")

    print(f"\n[*] Session: {session.name}")

    # Subscription mode: make the notes with Claude Code instead of the paid API.
    if os.environ.get("USE_CLAUDE_SUBSCRIPTION", "").strip().lower() in ("1", "true", "yes"):
        return summarize_via_subscription(session, transcript, ctx_block, args)

    import anthropic
    try:
        client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY from the environment
    except Exception as e:
        print(f"ERROR: could not start the Claude client: {e}", file=sys.stderr)
        return 1

    print(f"[*] Using model: {args.model}")

    in_tokens = out_tokens = 0  # accumulate real usage to estimate this meeting's cost
    try:
        print("[*] Writing the summary and action items...")
        summary_msg = client.messages.create(
            model=args.model,
            max_tokens=4000,
            messages=[{"role": "user", "content": SUMMARY_PROMPT + ctx_block + "\nTRANSCRIPT:\n" + transcript}],
        )
        in_tokens += summary_msg.usage.input_tokens
        out_tokens += summary_msg.usage.output_tokens
        summary_md, auto_title = split_title(text_of(summary_msg))
        apply_auto_title(session, auto_title)
        (session / "summary.md").write_text(summary_md + "\n", encoding="utf-8")
        print(f"    saved: {session / 'summary.md'}")

        if not args.no_refine:
            print("[*] AI-refining the full transcript (this is the longer step)...")
            parts = []
            with client.messages.stream(
                model=args.model,
                max_tokens=32000,
                messages=[{"role": "user", "content": REFINE_PROMPT + ctx_block + "\nTRANSCRIPT:\n" + transcript}],
            ) as stream:
                for chunk in stream.text_stream:
                    parts.append(chunk)
                final = stream.get_final_message()  # carries the streamed call's usage
            in_tokens += final.usage.input_tokens
            out_tokens += final.usage.output_tokens
            refined = "".join(parts).strip()
            (session / "transcript-refined.txt").write_text(refined + "\n", encoding="utf-8")
            print(f"    saved: {session / 'transcript-refined.txt'}")

        in_rate, out_rate = price_for(args.model)
        cost = (in_tokens / 1_000_000) * in_rate + (out_tokens / 1_000_000) * out_rate
        (session / "cost.json").write_text(json.dumps({
            "model": args.model, "input_tokens": in_tokens, "output_tokens": out_tokens,
            "cost_usd": round(cost, 4),
        }), encoding="utf-8")
        print(f"[*] Estimated cost for this meeting: ${cost:.2f} "
              f"({in_tokens:,} in / {out_tokens:,} out tokens)")

    except anthropic.AuthenticationError:
        print("\nERROR: your Anthropic API key was rejected. Open the .env file in the "
              "meeting-recorder folder and check the key, or delete .env and run again to "
              "re-enter it.", file=sys.stderr)
        return 1
    except anthropic.RateLimitError:
        print("\nERROR: rate limited / out of credit. Check your balance in the Anthropic "
              "Console, then try again.", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"\nERROR talking to Claude: {e}", file=sys.stderr)
        return 1

    print("\n[OK] Done. Open your notes with:")
    print(f'   open "{session / "summary.md"}"')
    print("\n----- summary preview -----")
    preview = "\n".join(summary_md.splitlines()[:14])
    print(preview)
    print("---------------------------")
    return 0


if __name__ == "__main__":
    sys.exit(main())
