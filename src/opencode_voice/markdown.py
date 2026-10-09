"""Deterministic Markdown -> speech text conversion.

Single public entry point: to_speech(text) -> str.
Order matters: fences first (they may contain markdown-looking text),
then images/links, then block structures, then inline decorations.
"""

from __future__ import annotations

import re

_FENCE_RE = re.compile(r"```[^\n]*\n(.*?)```|~~~[^\n]*\n(.*?)~~~", re.DOTALL)
_HEADING_RE = re.compile(r"^[ \t]{0,3}#{1,6}[ \t]+(.*)$", re.MULTILINE)
_ATX_TRAIL_RE = re.compile(r"[ \t]+#+[ \t]*$")
_IMAGE_RE = re.compile(r"!\[([^\]]*)\]\([^)]*\)")
_LINK_RE = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_AUTOLINK_RE = re.compile(r"<(https?://[^>\s]+)>")
_BARE_URL_RE = re.compile(r"\b(?:https?|ftp)://[^\s)>\]]+")
_INLINE_CODE_RE = re.compile(r"`([^`\n]*)`")
_BOLD_RE = re.compile(r"(\*\*|__)(?=\S)(.+?)(?<=\S)\1", re.DOTALL)
_ITALIC_RE = re.compile(r"(?<![\w*])\*(?=[^\s*])([^*\n]+?)(?<![\s*])\*(?![\w*])")
_UNDERSCORE_RE = re.compile(r"(?<![\w_])_(?=[^\s_])([^_\n]+?)(?<![\s_])_(?![\w_])")
_STRIKE_RE = re.compile(r"~~(?=\S)(.+?)(?<=\S)~~", re.DOTALL)
_HR_RE = re.compile(r"^[ \t]{0,3}([-*_])[ \t]*(?:\1[ \t]*){2,}$", re.MULTILINE)
_HTML_TAG_RE = re.compile(r"</?[a-zA-Z][^>\n]*>")
_BLOCKQUOTE_RE = re.compile(r"^[ \t]{0,3}>[ \t]?", re.MULTILINE)
_TABLE_SEP_RE = re.compile(r"^\s*\|?[\s:|-]+\|?\s*$")
_TASK_ITEM_RE = re.compile(r"^\s*[-*+][\t ]+\[[ xX]\][\t ]+")
_BULLET_RE = re.compile(r"^([\t ]*)[-*+][\t ]+")
_NUMBERED_RE = re.compile(r"^([\t ]*)\d{1,3}[.)][\t ]+")


def _strip_fences(text: str) -> str:
    prev = None
    while prev != text:
        prev = text
        text = _FENCE_RE.sub("\n", text)
    # unbalanced fence (open but never closed): drop the opener + rest-of-line junk
    text = re.sub(r"```[^\n]*\n?", "\n", text)
    text = re.sub(r"~~~[^\n]*\n?", "\n", text)
    return text


def _convert_tables(text: str) -> str:
    lines = text.split("\n")
    out: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if "|" not in line:
            out.append(line)
            i += 1
            continue
        # gather a run of table-looking lines
        j = i
        run: list[str] = []
        while j < len(lines) and "|" in lines[j]:
            run.append(lines[j])
            j += 1
        rows = [r for r in run if not _TABLE_SEP_RE.match(r)]
        statements: list[str] = []
        for row in rows:
            cells = [c.strip() for c in row.strip().strip("|").split("|")]
            cells = [c for c in cells if c != ""]
            if not cells:
                continue
            if len(cells) == 1:
                statements.append(f"{cells[0]}.")
            else:
                statements.append(f"{cells[0]}: {' '.join(cells[1:])}.")
        if len(rows) > 10:
            # large table: summarize instead of reading every cell
            first = statements[0] if statements else ""
            out.append(f"Table with {len(rows)} rows. {first}")
        elif statements:
            out.append(" ".join(statements))
        i = j
    return "\n".join(out)


def _convert_lists(text: str) -> str:
    lines = text.split("\n")
    out: list[str] = []

    def flush_group(group: list[tuple[str, str]]) -> None:
        # group: (kind, item_text)
        items = [t for _, t in group if t]
        if not items:
            return
        if len(items) > 6:
            head = ", ".join(items[:3])
            out.append(f"{head}, and {len(items) - 3} more items.")
            return
        if len(items) == 1:
            out.append(f"{items[0]}.")
        elif len(items) == 2:
            out.append(f"{items[0]} and {items[1]}.")
        else:
            out.append(", ".join(items[:-1]) + f", and {items[-1]}.")

    group: list[tuple[str, str]] = []

    def close() -> None:
        nonlocal group
        if group:
            flush_group(group)
            group = []

    for line in lines:
        m_b = _BULLET_RE.match(line)
        m_n = _NUMBERED_RE.match(line)
        m_t = _TASK_ITEM_RE.match(line)
        if m_t:
            item = _TASK_ITEM_RE.sub("", line, count=1).strip()
            if not group or group[-1][0] != "bullet":
                close()
            group.append(("bullet", item))
            continue
        if m_b:
            item = _BULLET_RE.sub("", line, count=1).strip()
            if not group or group[-1][0] != "bullet":
                close()
            group.append(("bullet", item))
            continue
        if m_n:
            item = _NUMBERED_RE.sub("", line, count=1).strip()
            if not group or group[-1][0] != "num":
                close()
            group.append(("num", item))
            continue
        close()
        out.append(line)
    close()
    return "\n".join(out)


def to_speech(text: str) -> str:
    if not text or not text.strip():
        return ""

    s = text

    # 1. code fences (unless caller wants code read: handled upstream)
    s = _strip_fences(s)

    # 2. images (drop entirely), links (anchor only), autolinks
    s = _IMAGE_RE.sub(" ", s)
    s = _LINK_RE.sub(r"\1", s)
    s = _AUTOLINK_RE.sub(" ", s)

    # 3. tables -> statements
    s = _convert_tables(s)

    # 4. headings
    s = _HEADING_RE.sub(lambda m: _ATX_TRAIL_RE.sub("", m.group(1)).strip() + ".", s)

    # 5. blockquote markers
    s = _BLOCKQUOTE_RE.sub("", s)

    # 6. lists -> natural sentences
    s = _convert_lists(s)

    # 7. horizontal rules / empty pseudo-headings
    s = _HR_RE.sub("", s)

    # 8. html tags
    s = _HTML_TAG_RE.sub(" ", s)

    # 9. bare URLs (only after links so anchors survive)
    s = _BARE_URL_RE.sub(" ", s)

    # 10. inline decorations
    s = _INLINE_CODE_RE.sub(r"\1", s)
    s = _STRIKE_RE.sub(r"\1", s)
    s = _BOLD_RE.sub(r"\2", s)
    s = _ITALIC_RE.sub(r"\1", s)
    s = _UNDERSCORE_RE.sub(r"\1", s)

    # 11. escapes
    s = re.sub(r"\\([\\`*_{}\[\]()#+\-.!|>])", r"\1", s)

    # 12. whitespace cleanup
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r" *\n *", "\n", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    s = re.sub(r"([.!?:]){2,}", r"\1", s)
    return s.strip()
