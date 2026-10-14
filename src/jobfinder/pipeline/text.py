"""HTML to plain text, with entity decoding robust to double escaping (Greenhouse)."""

from __future__ import annotations

import html
import re
from html.parser import HTMLParser

_BLOCK_TAGS = frozenset(
    {"p", "div", "br", "li", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "section"}
)
_SKIP_TAGS = frozenset({"script", "style", "noscript", "template"})
_SPACES = re.compile(r"[ \t\u00a0]+")
_BLANKS = re.compile(r"\n\s*\n+")


class _Stripper(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _SKIP_TAGS:
            self._skip += 1
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n- " if tag == "li" else "\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_TAGS:
            self._skip = max(0, self._skip - 1)
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self.parts.append(data)


def unescape_fully(text: str, max_rounds: int = 3) -> str:
    for _ in range(max_rounds):
        decoded = html.unescape(text)
        if decoded == text:
            break
        text = decoded
    return text


def html_to_text(markup: str) -> str:
    stripper = _Stripper()
    stripper.feed(unescape_fully(markup))
    stripper.close()
    lines = (_SPACES.sub(" ", line).strip() for line in "".join(stripper.parts).splitlines())
    text = "\n".join(lines)
    return _BLANKS.sub("\n\n", text).strip()
