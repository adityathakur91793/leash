"""Sanitizers and heuristic flags for untrusted catalogue text."""

from __future__ import annotations

from html.parser import HTMLParser
import re

MAX_EXTERNAL_TEXT_LENGTH = 10_000
MAX_PRODUCT_DESCRIPTION_LENGTH = 600

INJECTION_INDICATORS = (
    "ignore previous",
    "ignore all instructions",
    "system:",
    "developer:",
    "[inst]",
    "<|im_start|>",
    "<script",
    "<iframe",
    "javascript:",
    "reveal your prompt",
    "disregard the above",
    "tool call",
)

_SPACE_RE = re.compile(r"\s+")


class _PlainTextParser(HTMLParser):
    """Extract text while excluding script/style and their contents."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._suppressed: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        if tag.lower() in {"script", "style"}:
            self._suppressed.append(tag.lower())

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if self._suppressed and self._suppressed[-1] == tag:
            self._suppressed.pop()

    def handle_data(self, data: str) -> None:
        if not self._suppressed:
            self.parts.append(data)


def inspect_untrusted_text(value: str, *, max_length: int) -> tuple[str, bool, bool]:
    """Return plain text, whether it was truncated, and an injection heuristic flag.

    The heuristic is defense in depth, not a guarantee that unflagged content is safe.
    """
    bounded = value[:MAX_EXTERNAL_TEXT_LENGTH]
    truncated = len(value) > MAX_EXTERNAL_TEXT_LENGTH
    lowered = bounded.lower()
    normalized = re.sub(r"[^a-z0-9]+", " ", lowered)
    normalized_markers = tuple(
        re.sub(r"[^a-z0-9]+", " ", marker.lower()).strip()
        for marker in INJECTION_INDICATORS
    )
    suspicious = any(marker in lowered for marker in INJECTION_INDICATORS) or any(
        marker and marker in normalized for marker in normalized_markers
    )

    parser = _PlainTextParser()
    try:
        parser.feed(bounded)
        parser.close()
        plain = " ".join(parser.parts)
    except Exception:
        plain = re.sub(r"<[^>]*>", " ", bounded)
    plain = _SPACE_RE.sub(" ", plain).strip()
    if len(plain) > max_length:
        plain = plain[:max_length].rstrip()
        truncated = True
    return plain, truncated, suspicious
