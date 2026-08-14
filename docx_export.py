#!/usr/bin/env python3
"""
Meeting Recorder — export a transcript as a Word (.docx) document.

Builds the file with Python's standard library only (a .docx is a zip of XML),
so there is nothing extra to install. The layout mirrors the app's transcript
view: each speaker's name in its own colour with a grey timestamp, and the
spoken text underneath.
"""

import io
import re
import zipfile
from xml.sax.saxutils import escape

# Same speaker palette as the app's transcript view (ui.html), without '#'.
SPK_COLORS = ["4F46E5", "DB2777", "0D9488", "B45309",
              "2563EB", "7C3AED", "0369A1", "BE123C"]
INK = "0F172A"      # body text
MUTED = "94A3B8"    # timestamps
TITLE = "1E293B"    # document title
SUBTLE = "64748B"   # subtitle

TS_RE = re.compile(r"^\[(\d{1,2}:\d{2}(?::\d{2})?)\]\s*(.*)$")
SPK_RE = re.compile(r"^(You|Them|Speaker\s*\d+|[A-Z][A-Za-z0-9 .'\-]{0,28}):\s+(.*)$")


def parse_turns(text: str):
    """Group transcript lines into speaker turns, exactly like the app's view."""
    turns = []
    cur = None
    for raw in (text or "").split("\n"):
        line = raw.rstrip()
        if not line.strip() or line.startswith("#"):
            continue
        ts, rest = "", line.strip()
        m = TS_RE.match(rest)
        if m:
            ts, rest = m.group(1), m.group(2)
        speaker = ""
        sm = SPK_RE.match(rest)
        if sm:
            speaker = re.sub(r"\s+", " ", sm.group(1)).strip()
            rest = sm.group(2)
        if cur is not None and cur["speaker"] == speaker:
            cur["text"] += " " + rest
        else:
            cur = {"ts": ts, "speaker": speaker, "text": rest}
            turns.append(cur)
    return turns


def _run(text, color=INK, size=22, bold=False):
    """One styled text run (size is in half-points: 22 = 11pt)."""
    props = f'<w:color w:val="{color}"/><w:sz w:val="{size}"/><w:szCs w:val="{size}"/>'
    if bold:
        props = "<w:b/>" + props
    return (f'<w:r><w:rPr>{props}</w:rPr>'
            f'<w:t xml:space="preserve">{escape(text)}</w:t></w:r>')


def _para(runs, before=0, after=120):
    return (f'<w:p><w:pPr><w:spacing w:before="{before}" w:after="{after}"/></w:pPr>'
            + "".join(runs) + "</w:p>")


def transcript_docx(title: str, subtitle: str, transcript_text: str) -> bytes:
    """Render the transcript as a .docx and return the file's bytes."""
    colors, paras = {}, []

    def color_for(speaker):
        if speaker not in colors:
            colors[speaker] = SPK_COLORS[len(colors) % len(SPK_COLORS)]
        return colors[speaker]

    paras.append(_para([_run(title or "Meeting transcript", TITLE, 34, bold=True)],
                       after=40))
    if subtitle:
        paras.append(_para([_run(subtitle, SUBTLE, 20)], after=280))

    for t in parse_turns(transcript_text):
        if t["speaker"]:
            head = [_run(t["speaker"], color_for(t["speaker"]), 22, bold=True)]
            if t["ts"]:
                head.append(_run("   " + t["ts"], MUTED, 18))
            paras.append(_para(head, before=200, after=40))
            paras.append(_para([_run(t["text"])], after=80))
        else:
            runs = ([_run(t["ts"] + "  ", MUTED, 18)] if t["ts"] else []) + [_run(t["text"])]
            paras.append(_para(runs, before=120, after=80))

    document = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        "<w:body>" + "".join(paras) +
        '<w:sectPr><w:pgSz w:w="11906" w:h="16838"/>'
        '<w:pgMar w:top="1134" w:right="1134" w:bottom="1134" w:left="1134"/></w:sectPr>'
        "</w:body></w:document>"
    )
    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/word/document.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
        "</Types>"
    )
    rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
        'Target="word/document.xml"/>'
        "</Relationships>"
    )

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", content_types)
        z.writestr("_rels/.rels", rels)
        z.writestr("word/document.xml", document)
    return buf.getvalue()
