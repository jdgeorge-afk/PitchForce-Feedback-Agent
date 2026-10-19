"""Tiny markup shared by report renderers: paragraphs, "- " bullets, "### " headings, **bold**,
_italic paragraph_, and [label](url). Everything else is plain text (HTML-escaped where rendered)."""
from __future__ import annotations

import html
import re
from dataclasses import dataclass

INLINE = re.compile(r"\*\*(.+?)\*\*|\[([^\]]+)\]\((https?://[^)\s]+)\)")


@dataclass
class Span:
    kind: str  # text | bold | link
    text: str
    url: str = ""


@dataclass
class Block:
    kind: str  # para | bullets | heading | note
    spans: list[Span] | None = None
    items: list[list[Span]] | None = None


def inline(text: str) -> list[Span]:
    out, pos = [], 0
    for m in INLINE.finditer(text):
        if m.start() > pos:
            out.append(Span("text", text[pos:m.start()]))
        if m.group(1) is not None:
            out.append(Span("bold", m.group(1)))
        else:
            out.append(Span("link", m.group(2), m.group(3)))
        pos = m.end()
    if pos < len(text):
        out.append(Span("text", text[pos:]))
    return out


def parse(text: str) -> list[Block]:
    blocks: list[Block] = []
    for chunk in re.split(r"\n\s*\n", (text or "").strip()):
        lines = chunk.split("\n")
        if not chunk.strip():
            continue
        if lines[0].startswith("### "):
            blocks.append(Block("heading", inline(lines[0][4:].strip())))
            rest = "\n".join(lines[1:]).strip()
            if rest:
                blocks.extend(parse(rest))
            continue
        if all(ln.startswith("- ") or ln.startswith("  ") for ln in lines) and lines[0].startswith("- "):
            items: list[str] = []
            for ln in lines:
                if ln.startswith("- "):
                    items.append(ln[2:].strip())
                else:
                    items[-1] += "\n" + ln.strip()
            blocks.append(Block("bullets", items=[inline(i) for i in items]))
            continue
        para = " ".join(ln.strip() for ln in lines)
        if para.startswith("_") and para.endswith("_") and len(para) > 2:
            blocks.append(Block("note", inline(para[1:-1])))
        else:
            blocks.append(Block("para", inline(para)))
    return blocks


def spans_html(spans: list[Span]) -> str:
    out = []
    for s in spans:
        t = html.escape(s.text).replace("\n", "<br>")
        if s.kind == "bold":
            out.append(f"<strong>{t}</strong>")
        elif s.kind == "link":
            out.append(f'<a href="{html.escape(s.url)}" rel="noopener noreferrer" target="_blank">{t}</a>')
        else:
            out.append(t)
    return "".join(out)


def to_html(text: str) -> str:
    out = []
    for b in parse(text):
        if b.kind == "heading":
            out.append(f"<h4>{spans_html(b.spans)}</h4>")
        elif b.kind == "bullets":
            out.append("<ul>" + "".join(f"<li>{spans_html(i)}</li>" for i in b.items) + "</ul>")
        elif b.kind == "note":
            out.append(f'<p class="note"><em>{spans_html(b.spans)}</em></p>')
        else:
            out.append(f"<p>{spans_html(b.spans)}</p>")
    return "\n".join(out)


def to_plain(text: str) -> str:
    def sp(spans):
        return "".join(s.text if s.kind != "link" else (s.url if s.text == s.url else f"{s.text} ({s.url})") for s in spans)

    out = []
    for b in parse(text):
        if b.kind == "bullets":
            out.append("\n".join("- " + sp(i) for i in b.items))
        else:
            out.append(sp(b.spans))
    return "\n\n".join(out)
