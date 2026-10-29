"""The player and the book as tools for the agent, and what it is told about them.

Every tool is synchronous: it acts on the one player, which outlives every turn,
rather than becoming a task of its own. The person drives the same player from the
page, so the agent learns of their clicks from a note, and before every turn it is
shown where the player is now.
"""

from ..harness.agent import NOTICE
from ..harness.tools import Tool
from .player import IDLE, Player

SP = (
    "You are reading a book aloud to a person, through a player they hear and can also "
    "control themselves from the page: they may start, pause or resume it without "
    "asking you, and you will be told when they do. Before each of your turns you are "
    "shown what the player is doing and the sentence being heard.\n"
    "You are not given the text that has been read. If you need it, player_status "
    "returns the passage just heard, and book_read and book_find look up any part of "
    "the book.\n"
    "When the person asks you something while the player is playing, call player_pause "
    "first, then answer. Do not resume the reading on your own: call player_resume only "
    "when the person asks you to carry on. It goes back to the sentence they were "
    "hearing when they spoke. Keep answers short and plain: the person is in the "
    "middle of listening.\n"
    "Positions in the book are character offsets within a chapter. They are bookkeeping "
    "between you and the tools: never quote one to the person.\n"
    "What you write is spoken aloud as you write it, in a voice of your own, so write "
    "for the ear: plain sentences, no lists, headings or markdown. The person may cut "
    "in while you speak; if they do, you will be told what they did not hear. While you "
    "are speaking the book waits, and a play or resume you ask for happens once you "
    "have finished. So once you have started or resumed the reading, say nothing more "
    "unless asked: anything you say keeps the book waiting."
)

# enough for a passage or two, not for a chapter
READ_LIMIT = 3000
FIND_LIMIT = 8
SNIPPET = 80


def _schema(name: str, description: str, **props) -> dict:
    required = [k for k, v in props.items() if v.pop("required", False)]
    return dict(
        name=name,
        description=description,
        input_schema=dict(type="object", properties=props, required=required),
    )


def _int(description: str, required: bool = False) -> dict:
    return dict(type="integer", description=description, required=required)


def status_line(player: Player) -> str | None:
    """One notice of where the player is, for the agent before each turn."""
    st = player.status()
    if st["state"] == IDLE:
        return None
    who = "the person" if st["by"] == "person" else "you"
    lines = [
        f"{NOTICE} The player is {st['state']} (last changed by {who}), in chapter "
        f"{st['chapter']} '{st['title']}', paragraph {st['paragraph']}, at offset "
        f"{st['offset']} of {st['length']}, {st['elapsed']} into this reading, with "
        f"{st['ready']} of audio prepared ahead.",
        f"Sentence being heard: {st['sentence']!r}",
    ]
    if "held" in st:
        lines.append(
            f"Your {st['held']} is waiting until you have finished speaking, and will "
            "happen then unless the person cuts in."
        )
    if "mark" in st:
        lines.append(
            f"When the person last spoke, they were hearing: {st['mark_sentence']!r} "
            f"(offset {st['mark']}); player_resume goes back there."
        )
    return "\n".join(lines)


def tools(player: Player) -> list[Tool]:
    book = player.book

    def play(agent, chapter: int, offset: int = 0) -> str:
        if not 0 <= chapter < len(book.chapters):
            return f"error: there is no chapter {chapter}; book_contents lists them"
        start = player.play(chapter, offset)
        return f"playing chapter {chapter} '{book[chapter].title}' from offset {start}"

    def pause(agent) -> str:
        if player.pause():
            st = player.status()
            return f"paused at offset {st['offset']}, in the sentence {st['sentence']!r}"
        return f"nothing to pause: the player is {player.state}"

    def resume(agent, offset: int | None = None) -> str:
        if player.chapter is None:
            return "error: nothing has been played yet; use player_play"
        start = player.resume(offset)
        c = book[player.chapter]
        s0, s1 = c.sentence_at(start)
        return f"playing again from offset {start}, the sentence {c.text[s0:s1]!r}"

    def status(agent) -> str:
        line = status_line(player)
        if line is None:
            return "the player is idle: nothing has been played yet"
        return f"{line}\nThe passage just heard:\n{player.tail()}"

    def contents(agent) -> str:
        return "\n".join(
            f"chapter {c.id}: {c.title} ({len(c.text)} characters)" for c in book.chapters
        )

    def read(agent, chapter: int, start: int = 0, end: int | None = None) -> str:
        if not 0 <= chapter < len(book.chapters):
            return f"error: there is no chapter {chapter}"
        c = book[chapter]
        start = max(0, start)
        end = min(len(c.text), end if end is not None else start + READ_LIMIT, start + READ_LIMIT)
        more = f"\n(cut at offset {end}; ask again from there for more)" if end < len(c.text) else ""
        return f"chapter {chapter} '{c.title}', offsets {start} to {end}:\n{c.text[start:end]}{more}"

    def find(agent, query: str) -> str:
        q = query.strip()
        if not q:
            return "error: give some words to look for"
        hits = book.find(q, FIND_LIMIT)
        if not hits:
            return f"{q!r} does not occur in the book"
        lines = []
        for ch, at in hits:
            c = book[ch]
            lo, hi = max(0, at - SNIPPET // 2), min(len(c.text), at + len(q) + SNIPPET // 2)
            lines.append(f"chapter {ch} '{c.title}', offset {at}: ...{c.text[lo:hi]}...")
        if len(hits) == FIND_LIMIT:
            lines.append(f"(showing the first {FIND_LIMIT}; there may be more)")
        return "\n".join(lines)

    return [
        Tool(
            _schema(
                "player_play",
                "Start reading a chapter aloud, from the start of the sentence at an offset.",
                chapter=_int("Chapter number, from book_contents", required=True),
                offset=_int("Character offset in the chapter. Defaults to its start."),
            ),
            play,
            synchronous=True,
        ),
        Tool(_schema("player_pause", "Pause the reading."), pause, synchronous=True),
        Tool(
            _schema(
                "player_resume",
                "Carry on reading from the start of a sentence. With no offset, that is the "
                "sentence the person was hearing when they last spoke, or else where it paused.",
                offset=_int("Character offset in the current chapter to carry on from."),
            ),
            resume,
            synchronous=True,
        ),
        Tool(
            _schema(
                "player_status",
                "What the player is doing, and the passage the person has just heard.",
            ),
            status,
            synchronous=True,
        ),
        Tool(_schema("book_contents", "List the chapters of the book."), contents, synchronous=True),
        Tool(
            _schema(
                "book_read",
                f"Read part of a chapter, at most {READ_LIMIT} characters at a time.",
                chapter=_int("Chapter number", required=True),
                start=_int("Offset to start at. Defaults to 0."),
                end=_int("Offset to stop at."),
            ),
            read,
            synchronous=True,
        ),
        Tool(
            _schema(
                "book_find",
                "Find where words occur in the book, with the chapter and offset of each.",
                query=dict(type="string", description="Words to look for", required=True),
            ),
            find,
            synchronous=True,
        ),
    ]


def person_did(player: Player, action: str) -> str:
    """The note the agent is given when the person uses the player themselves."""
    st = player.status()
    where = f" in chapter {st['chapter']} '{st['title']}'" if "chapter" in st else ""
    sentence = f", at the sentence {st['sentence']!r}" if "sentence" in st else ""
    return f"The person {action} the player{where}{sentence}."
