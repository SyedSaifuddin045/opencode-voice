"""Deterministic speech adapter: streaming sentence buffer + normalization.

Pipeline per emitted sentence:
    raw text -> markdown.to_speech -> normalize (symbols, pronunciations,
    whitespace) -> speech-ready sentence.

The adapter is stateful for streaming: feed() accepts incremental text
deltas and returns only sentences that are complete so far; flush()
returns the remainder (final partial sentence) and clears the buffer.
"""

from __future__ import annotations

import re

from .markdown import to_speech

# longest-first symbol replacement
_SYMBOL_RULES: list[tuple[str, str]] = [
    ("!==", "not equal to"),
    ("===", "equals"),
    (">=", "greater than or equal to"),
    ("<=", "less than or equal to"),
    ("!=", "not equal to"),
    ("≠", "not equal to"),
    ("≥", "greater than or equal to"),
    ("≤", "less than or equal to"),
    ("->", " to "),
    ("=>", " to "),
    ("→", " to "),
    ("←", " to "),
    ("&&", " and "),
    ("||", " or "),
    ("==", "equals"),
    ("+=", " plus "),
    ("-=", " minus "),
    ("...", " dot dot dot "),
]

# words after which a period must not split a sentence
_ABBREVIATIONS = {
    "e.g", "i.e", "etc", "vs", "cf", "al", "dr", "mr", "mrs", "ms", "prof",
    "st", "jr", "sr", "no", "approx", "dept", "est", "min", "max", "vol",
    "u.s", "u.k", "e.u", "a.m", "p.m", "fig", "inc", "ltd", "corp",
}


def _space_collapse(text: str) -> str:
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


class SpeechAdapter:
    """Streaming sentence extractor + deterministic speech normalizer."""

    def __init__(
        self,
        *,
        speak_code: bool = False,
        speak_urls: bool = False,
        pronunciations: dict[str, str] | None = None,
        max_sentence_chars: int = 400,
    ):
        self.speak_code = speak_code
        self.speak_urls = speak_urls
        self.pronunciations = dict(pronunciations or {})
        self.max_sentence_chars = max_sentence_chars
        self._buffer = ""

    # ------------------------------------------------------------- public

    def feed(self, delta: str) -> list[str]:
        """Append a text delta; return sentences that are now complete."""
        if delta:
            self._buffer += delta
        return self._drain(final=False)

    def flush(self) -> list[str]:
        """Return whatever remains in the buffer as final sentence(s)."""
        out = self._drain(final=True)
        self._buffer = ""
        return out

    def reset(self) -> None:
        self._buffer = ""

    @property
    def pending(self) -> str:
        return self._buffer

    def normalize(self, text: str) -> str:
        """Normalization only (no markdown, no buffering)."""
        return self._normalize(text)

    # ------------------------------------------------------------ internal

    def _drain(self, *, final: bool) -> list[str]:
        out: list[str] = []
        # force-cut pathological unbroken text (only while streaming)
        if not final and len(self._buffer) > self.max_sentence_chars * 2:
            cleaned = self._finalize(self._force_cut())
            if cleaned:
                out.append(cleaned)
        while True:
            idx = self._find_boundary(self._buffer)
            if idx is None:
                break
            sentence, self._buffer = self._buffer[: idx + 1], self._buffer[idx + 1:]
            cleaned = self._finalize(sentence)
            if cleaned:
                out.append(cleaned)
        if final:
            rest = self._finalize(self._buffer)
            if rest:
                out.append(rest)
        return out

    def _force_cut(self) -> str:
        limit = self.max_sentence_chars
        cut = self._buffer[:limit]
        sp = cut.rfind(" ")
        if sp > limit // 2:
            cut, self._buffer = cut[: sp + 1], self._buffer[sp + 1:]
        else:
            self._buffer = self._buffer[limit:]
        return cut

    def _finalize(self, sentence: str) -> str:
        text = sentence.strip()
        if not text:
            return ""
        text = to_speech(text)
        text = self._normalize(text)
        return text.strip()

    def _normalize(self, text: str) -> str:
        if not text:
            return ""
        s = text

        if not self.speak_urls:
            s = re.sub(r"\b(?:https?|ftp)://\S+", " ", s)
            s = re.sub(r"\bwww\.\S+", " ", s)
        if not self.speak_code:
            s = re.sub(r"(?m)^\s*\$\s+", "", s)

        # custom pronunciations (word-boundary, case-sensitive)
        for src, dst in self.pronunciations.items():
            s = re.sub(rf"\b{re.escape(src)}\b", dst, s)

        s = s.replace("and/or", " and or ")
        s = s.replace("n/a", "not applicable")

        for sym, words in _SYMBOL_RULES:
            if sym in s:
                s = s.replace(sym, words)

        # path separators -> "slash", but never inside decimals/fractions
        s = re.sub(r"(?<![\d.])/(?!\d)", " slash ", s)
        s = s.replace(" / ", " slash ")

        s = _space_collapse(s)
        return s

    # ------------------------------------------------- sentence boundary

    def _find_boundary(self, buf: str) -> int | None:
        """Index of last char of first speakable sentence boundary, or None.

        Skips boundaries inside code fences, after abbreviations, and
        after digits/single letters (3.12, U.S., 1.).
        """
        if not buf:
            return None
        in_fence = False
        i = 0
        n = len(buf)
        while i < n:
            if buf.startswith("```", i) or buf.startswith("~~~", i):
                in_fence = not in_fence
                i += 3
                continue
            ch = buf[i]
            if ch in ".!?。！？" and not in_fence:
                j = i
                while j + 1 < n and buf[j + 1] in ".!?。！？":
                    j += 1
                nxt = buf[j + 1] if j + 1 < n else None
                at_end = nxt is None
                if at_end or nxt in " \t\n\"'”’)」":
                    if self._split_allowed(buf, i):
                        return j
                    i = j + 1
                    continue
                # punctuation glued to next token (3.12, U.S.A) -> no split
                i = j + 1
                continue
            i += 1
        return None

    def _split_allowed(self, buf: str, dot_idx: int) -> bool:
        start = dot_idx
        while start > 0 and buf[start - 1] in ".!?":
            start -= 1
        token = re.search(r"([A-Za-z0-9._/-]+)$", buf[:start])
        if not token:
            return True
        word = token.group(1)
        if not word:
            return True
        if word.isdigit():
            return False  # list markers / ordinals
        if "." in word:
            lowered = word.lower().rstrip(".")
            if lowered in _ABBREVIATIONS:
                return False
            if re.fullmatch(r"\d+(\.\d+)*", word):
                return False  # version numbers / decimals
            if re.fullmatch(r"[A-Za-z]\.[A-Za-z]", word):
                return False  # initials like U.S
            return True
        if word.lower() in _ABBREVIATIONS:
            return False
        if len(word) == 1 and word.isalpha():
            return False  # single letter
        return True
