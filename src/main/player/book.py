"""The book the player reads: chapters as plain text, with paragraph and sentence spans.

Every position is a character offset into one chapter's text, which is its paragraphs
joined by a blank line. The player, the agent's tools and the page all speak in these
offsets, so a word heard, a sentence resumed and a passage looked up agree.
"""

import json
import os
import re
from bisect import bisect_right
from dataclasses import dataclass
from pathlib import Path

BOOK = Path(
    os.getenv(
        "BOOK_PATH",
        Path(__file__).resolve().parents[4] / "books-zarathustra" / "data" / "book.json",
    )
)
# section numbers ("1", "2") are layout, not something to read aloud
READ = ("p", "verse")
GAP = "\n\n"
# a sentence ends at . ! ? (or an ellipsis), with any closing quotes or brackets
SENTENCE_END = re.compile(r"[.!?…]+[\"'’”)\]]*(?=\s|$)")


@dataclass(frozen=True)
class Chapter:
    id: int
    title: str
    text: str
    paragraphs: tuple[tuple[int, int], ...]
    sentences: tuple[tuple[int, int], ...]

    def _span(self, spans, offset: int) -> tuple[int, int]:
        starts = [s for s, _ in spans]
        i = max(0, bisect_right(starts, max(0, offset)) - 1)
        return spans[i]

    def sentence_at(self, offset: int) -> tuple[int, int]:
        return self._span(self.sentences, offset)

    def paragraph_at(self, offset: int) -> tuple[int, int]:
        return self._span(self.paragraphs, offset)

    def paragraph_index(self, offset: int) -> int:
        return self.paragraphs.index(self.paragraph_at(offset))


def sentences_of(text: str, start: int, end: int) -> list[tuple[int, int]]:
    out, s = [], start
    for m in SENTENCE_END.finditer(text, start, end):
        out.append((s, m.end()))
        s = m.end()
        while s < end and text[s].isspace():
            s += 1
    if s < end:
        out.append((s, end))
    return out


def chapter(id: int, title: str, paragraphs: list[str]) -> Chapter:
    text = GAP.join(p.strip() for p in paragraphs)
    spans, sents, at = [], [], 0
    for p in paragraphs:
        end = at + len(p.strip())
        spans.append((at, end))
        sents += sentences_of(text, at, end)
        at = end + len(GAP)
    return Chapter(id, title, text, tuple(spans), tuple(sents))


class Book:
    def __init__(self, title: str, chapters: list[Chapter]):
        self.title = title
        self.chapters = chapters

    @classmethod
    def load(cls, path: Path = BOOK) -> "Book":
        b = json.loads(Path(path).read_text())
        chapters = [
            chapter(c["id"], c["title"], [bl["text"] for bl in c["blocks"] if bl["type"] in READ])
            for c in b["chapters"]
        ]
        return cls(b["title"], chapters)

    def __getitem__(self, id: int) -> Chapter:
        return self.chapters[id]

    def find(self, query: str, limit: int) -> list[tuple[int, int]]:
        """Where `query` occurs, as (chapter, offset), case-insensitively, in reading order."""
        q, hits = query.lower(), []
        for c in self.chapters:
            text, at = c.text.lower(), 0
            while (i := text.find(q, at)) >= 0:
                hits.append((c.id, i))
                if len(hits) >= limit:
                    return hits
                at = i + 1
        return hits
