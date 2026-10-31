"""The files an answer claims, checked against the writes that happened.

The shipped instructions ask a Pirate to end its answer with a FILES line naming
the files it wrote. A small local model claims files it never wrote (seen
2026-10-24: a DOT file pasted into the answer, "FILES: flow.dot", no write).
The assignment's write_actions are the truth; this says where the answer and
the writes disagree, so the assigner need not compare them by hand.

It must not mislead: prose on the FILES line ("none, the write was refused") is
not a claim. A piece counts as a claimed path when it has no spaces, or when it
names a file that was written (a path with spaces). Code blocks are skipped, so
a quoted template does not count as the answer's own FILES line.
"""
from __future__ import annotations

import re
from typing import Any

MAX_CLAIMS = 32
FENCE = re.compile(r"^[ \t]*```.*?^[ \t]*```[^\n]*$", re.MULTILINE | re.DOTALL)
LABEL = re.compile(r"^[ \t>*_#-]*\**FILES\**[ \t]*:[ \t]*\**[ \t]*(.*)$", re.MULTILINE)  # FILES:, **FILES:**
ITEM = re.compile(r"^[ \t]*(?:[-*•]|\d{1,3}[.)])[ \t]+(.+)$")  # "- a.md", "* a.md", "1. a.md", "2) a.md"
SPLIT = re.compile(r"\s*(?:,|;|\s+and\s+)\s*")
REMARK = re.compile(r"\s+(?:\(|—|–|-\s|\[)")  # "a.md (patched)", "a.md — new", "a.md - new"
ROOT = re.compile(r"([a-z][a-z0-9_-]{0,63}):(?![\\/])(.+)\Z")  # "work:a.md"; not "C:\..." or "http://"
LINE_REF = re.compile(r":\d+(?::\d+)?\Z")  # "a.md:12"
NOTHING = {"none", "nothing", "n/a", "-", "keine", "no files", "no file"}


def _piece(text: str) -> tuple[str | None, str] | None:
    """(root or None, path) of one piece of the FILES line, or None for nothing."""
    text = REMARK.split(text.strip(), maxsplit=1)[0]
    text = text.strip().strip("`*_'\"").rstrip(".;:,").strip().strip("`*_'\"").strip()
    lowered = text.lower()
    if not text or lowered in NOTHING or lowered.startswith(("none ", "no file", "nothing ")):
        return None
    root = None
    match = ROOT.match(text)
    if match:
        root, text = match.group(1), match.group(2)
    text = LINE_REF.sub("", text)
    while text.startswith("./"):
        text = text[2:]
    return (root, text) if text else None


def claimed(answer: str | None) -> list[tuple[str | None, str]] | None:
    """The pieces after the last FILES label outside code blocks, or None without one."""
    if not answer:
        return None
    text = FENCE.sub("", answer)
    labels = list(LABEL.finditer(text))
    if not labels:
        return None
    last = labels[-1]
    pieces = SPLIT.split(last.group(1).strip()) if last.group(1).strip() else []
    if not pieces:  # a list below the label
        for line in text[last.end():].splitlines()[1:]:
            item = ITEM.match(line)
            if item is None:
                if line.strip():
                    break
                continue
            pieces.append(item.group(1))
    found: list[tuple[str | None, str]] = []
    for raw in pieces:
        piece = _piece(raw)
        if piece is not None and piece not in found:
            found.append(piece)
    return found[:MAX_CLAIMS]


def _same(claim: tuple[str | None, str], write: tuple[str, str]) -> bool:
    (root, path), (written_root, written) = claim, write
    if root is not None and root != written_root:
        return False
    return path == written or written.endswith("/" + path) or path.endswith("/" + written)


def _name(piece: tuple[str | None, str]) -> str:
    return piece[1] if piece[0] is None else f"{piece[0]}:{piece[1]}"


def check(answer: str | None, writes: list[dict[str, Any]]) -> dict[str, list[str]] | None:
    """{claimed, claimed_not_written, written_not_claimed} or None without a FILES label."""
    pieces = claimed(answer)
    if pieces is None:
        return None
    written: list[tuple[str, str]] = []
    for action in writes:
        path, root = action.get("path"), action.get("root", "work")
        if isinstance(path, str) and isinstance(root, str) and (root, path) not in written:
            written.append((root, path))
    # A piece with spaces is a claim only when it names a written file; otherwise it is prose.
    claims = [p for p in pieces if " " not in p[1] or any(_same(p, w) for w in written)]
    return {"claimed": [_name(c) for c in claims],
            "claimed_not_written": [_name(c) for c in claims if not any(_same(c, w) for w in written)],
            "written_not_claimed": [w[1] for w in written if not any(_same(c, w) for c in claims)][:MAX_CLAIMS]}
