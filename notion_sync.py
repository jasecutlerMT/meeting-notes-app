#!/usr/bin/env python3
"""
Meeting Recorder — optional: save a meeting's summary to Notion as a new page.

Reads a session's summary.md and creates a new page in Notion — either as a child
of a page, or as a new entry in a database (auto-detected). Uses only Python's
standard library (no extra installs).

Configured via two values in .env (set up by connect_notion.sh):
  NOTION_TOKEN            – your Notion internal integration secret
  NOTION_PARENT_PAGE_ID   – the page/database id (or URL) to put notes under

On success it prints the new Notion page's URL.

Usage:
  python3 notion_sync.py                 # sync your most recent meeting
  python3 notion_sync.py "/path/to/session-folder"
"""

import datetime as dt
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

BASE = Path.home() / "MeetingNotes"
NOTION_VERSION = "2022-06-28"


def normalize_id(ref: str) -> str:
    """Accept a Notion URL or raw id; return the FIRST id as a dashed UUID.

    The first 32-hex run in a Notion URL is the page/database id (any '?v=' view
    id comes later in the query string), so we take the first match.
    """
    s = (ref or "").strip()
    dashed = re.findall(
        r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}", s)
    if dashed:
        return dashed[0].lower()
    plain = re.findall(r"[0-9a-fA-F]{32}", s)
    if plain:
        r = plain[0].lower()
        return f"{r[0:8]}-{r[8:12]}-{r[12:16]}-{r[16:20]}-{r[20:32]}"
    raise ValueError(f"Could not find a Notion id in: {ref!r}")


def _api(method: str, path: str, token: str, body=None):
    req = urllib.request.Request(
        f"https://api.notion.com/v1{path}",
        data=json.dumps(body).encode("utf-8") if body is not None else None,
        method=method,
        headers={"Authorization": f"Bearer {token}",
                 "Notion-Version": NOTION_VERSION,
                 "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def rich_text(s: str):
    out = []
    for part in re.split(r"(\*\*.+?\*\*)", s):
        if not part:
            continue
        if part.startswith("**") and part.endswith("**") and len(part) > 4:
            out.append({"type": "text", "text": {"content": part[2:-2][:2000]},
                        "annotations": {"bold": True}})
        else:
            out.append({"type": "text", "text": {"content": part[:2000]}})
    return out or [{"type": "text", "text": {"content": ""}}]


def _block(kind: str, text: str):
    return {"object": "block", "type": kind, kind: {"rich_text": rich_text(text)}}


def md_to_blocks(md: str):
    blocks = []
    for raw in md.split("\n"):
        line = raw.rstrip()
        if not line.strip():
            continue
        if line.startswith("## "):
            blocks.append(_block("heading_2", line[3:]))
        elif line.startswith("# "):
            blocks.append(_block("heading_1", line[2:]))
        elif re.match(r"^[-*]\s+\[([ xX])\]\s+", line):
            m = re.match(r"^[-*]\s+\[([ xX])\]\s+(.*)", line)
            blocks.append({"object": "block", "type": "to_do",
                           "to_do": {"rich_text": rich_text(m.group(2)),
                                     "checked": m.group(1).lower() == "x"}})
        elif re.match(r"^[-*]\s+", line):
            blocks.append({"object": "block", "type": "bulleted_list_item",
                           "bulleted_list_item": {"rich_text": rich_text(re.sub(r"^[-*]\s+", "", line))}})
        else:
            blocks.append({"object": "block", "type": "paragraph",
                           "paragraph": {"rich_text": rich_text(line)}})
    return blocks[:100]


def pretty_title(session: Path) -> str:
    meta = session / "meta.json"
    if meta.exists():
        try:
            t = (json.loads(meta.read_text()).get("title") or "").strip()
            if t:
                return t
        except Exception:
            pass
    m = re.match(r"^\d{4}-\d{2}-\d{2}_\d{6}_?(.*)$", session.name)
    return (m.group(1).replace("_", " ").strip() if m and m.group(1) else "Meeting notes")


def meeting_datetime(session: Path):
    """When the meeting happened, from the session folder's timestamped name
    (falling back to the folder's modification time). Local timezone attached."""
    try:
        m = re.match(r"^(\d{4})-(\d{2})-(\d{2})_(\d{2})(\d{2})(\d{2})", session.name)
        if m:
            naive = dt.datetime(*(int(g) for g in m.groups()))
        else:
            naive = dt.datetime.fromtimestamp(session.stat().st_mtime)
        return naive.astimezone()
    except Exception:
        return None


def date_block(when):
    """A '📅 Thursday 10 July 2026, 2:30 PM' paragraph for the top of the note."""
    try:
        text = when.strftime("📅 %A %-d %B %Y, %-I:%M %p")
    except ValueError:  # platforms without %-d
        text = when.strftime("📅 %A %d %B %Y, %I:%M %p")
    return _block("paragraph", text)


def database_props(token: str, parent_id: str):
    """If parent_id is a database the integration can see, return
    (title property name, date property name or None); otherwise None (it's a page)."""
    try:
        db = _api("GET", f"/databases/{parent_id}", token)
    except Exception:
        return None
    props = db.get("properties") or {}
    tprop = next((n for n, p in props.items() if p.get("type") == "title"), "Name")
    date_props = [n for n, p in props.items() if p.get("type") == "date"]
    preferred = [n for n in date_props
                 if n.strip().lower() in ("date", "meeting date", "event time", "when", "day")]
    dprop = (preferred or date_props or [None])[0]
    return tprop, dprop


def create_page(token: str, parent_id: str, title: str, blocks, when=None):
    title_rt = [{"type": "text", "text": {"content": title[:2000]}}]
    props = database_props(token, parent_id)
    if props is not None:
        tprop, dprop = props
        properties = {tprop: {"title": title_rt}}
        if when is not None and dprop:
            # fill the database's Date column, so Notion's date sections/sorting work
            properties[dprop] = {"date": {"start": when.isoformat(timespec="seconds")}}
        elif when is not None:
            blocks = [date_block(when)] + blocks  # no Date column — write it in the note
        body = {"parent": {"database_id": parent_id},
                "properties": properties, "children": blocks[:100]}
    else:
        if when is not None:
            blocks = [date_block(when)] + blocks
        body = {"parent": {"page_id": parent_id},
                "properties": {"title": {"title": title_rt}}, "children": blocks[:100]}
    try:
        return _api("POST", "/pages", token, body).get("url", "")
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")
        raise RuntimeError(f"Notion API error {e.code}: {detail}")


def find_latest(base: Path):
    if not base.exists():
        return None
    cands = [p for p in base.iterdir() if p.is_dir() and (p / "summary.md").exists()]
    return max(cands, key=lambda p: p.stat().st_mtime) if cands else None


def main() -> int:
    token = os.environ.get("NOTION_TOKEN", "").strip()
    parent_ref = os.environ.get("NOTION_PARENT_PAGE_ID", "").strip()
    if not token or not parent_ref:
        print("ERROR: Notion is not connected. Run connect_notion.sh first.", file=sys.stderr)
        return 1

    session = Path(sys.argv[1]).expanduser() if len(sys.argv) > 1 else find_latest(BASE)
    if not session or not session.is_dir():
        print("ERROR: no meeting with a summary found.", file=sys.stderr)
        return 1
    summary = session / "summary.md"
    if not summary.exists():
        print(f"ERROR: no summary.md in {session}.", file=sys.stderr)
        return 1

    try:
        parent_id = normalize_id(parent_ref)
        url = create_page(token, parent_id, pretty_title(session),
                          md_to_blocks(summary.read_text(encoding="utf-8")),
                          when=meeting_datetime(session))
    except Exception as e:
        print(str(e), file=sys.stderr)
        return 1

    print(url)
    return 0


if __name__ == "__main__":
    sys.exit(main())
