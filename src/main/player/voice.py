"""The agent's own voice: its replies spoken as the model writes them.

Text arrives in pieces. The chunker hands Kokoro the first clause as soon as there is
one, so speech starts after a few words, then whole sentences, which sound better.
Each chunk becomes a part the page plays in turn and reports on, so at any moment the
voice knows how much of each reply the person has heard - which is what the agent is
told it said when the person cuts it off.

While the voice has anything to say, the book waits: speaking pauses it, and a play
or resume the agent asks for meanwhile is held until the voice has finished.
"""

import logging
import os
import re
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from itertools import count

from .player import PLAYING, Player
from .tts import VOICE as BOOK_VOICE
from .tts import Engine, Word

log = logging.getLogger("speech.voice")

# the same voice as the book's reader, unless told otherwise
VOICE = os.getenv("AGENT_VOICE", BOOK_VOICE)
# about ten tokens: enough for Kokoro to find the tone, few enough to start soon
FIRST_CHARS = int(os.getenv("VOICE_FIRST_CHARS", 40))
# after the first, a chunk ends at a sentence, or at a clause once it is this long
CLAUSE_CHARS = int(os.getenv("VOICE_CLAUSE_CHARS", 120))
# and is cut at a space if it reaches this without either
MAX_CHARS = int(os.getenv("VOICE_MAX_CHARS", 300))
SENTENCE = re.compile(r"[.!?…]+[\"'’”)\]]*\s")
CLAUSE = re.compile(r"[,;:—–]\s|\s[—–]\s")
# the generator looks again this often for chunks to make
POLL = 0.25


class Chunker:
    """Splits a reply, arriving in pieces, into chunks worth speaking."""

    def __init__(self, first=FIRST_CHARS, clause=CLAUSE_CHARS, most=MAX_CHARS):
        self.first, self.clause, self.most = first, clause, most
        self.buf = ""
        self.at = 0  # offset of buf in the reply
        self.started = False

    def feed(self, piece: str) -> list[tuple[int, str]]:
        self.buf += piece
        out = []
        while (cut := self._cut()) is not None:
            out.append(self._take(cut))
        return out

    def flush(self) -> list[tuple[int, str]]:
        return [self._take(len(self.buf))] if self.buf.strip() else []

    def _cut(self) -> int | None:
        if not self.started:
            # the first chunk: the first clause or sentence past FIRST_CHARS
            ends = sorted(m.end() for m in [*SENTENCE.finditer(self.buf), *CLAUSE.finditer(self.buf)])
            return next((e for e in ends if e >= self.first), self._forced())
        if m := SENTENCE.search(self.buf):
            return m.end()
        clauses = [m.end() for m in CLAUSE.finditer(self.buf) if m.end() >= self.clause]
        return clauses[0] if clauses else self._forced()

    def _forced(self) -> int | None:
        if len(self.buf) < self.most:
            return None
        space = self.buf.rfind(" ", 0, self.most)
        return space + 1 if space > 0 else self.most

    def _take(self, cut: int) -> tuple[int, str]:
        at, text = self.at, self.buf[:cut]
        self.buf, self.at, self.started = self.buf[cut:], self.at + cut, True
        return at, text


@dataclass
class Part:
    id: int
    call: int
    # where this part's text starts in its reply
    start: int
    text: str
    duration: float = 0.0
    # offsets into this part's text, times into this part
    words: tuple[Word, ...] = ()
    wav: bytes = field(default=b"", repr=False)
    made: bool = False
    # what the page last said: seconds into the part, and when it said it
    t: float = 0.0
    at: float = 0.0
    playing: bool = False
    ended: bool = False
    # stopped before its end: what was heard of it is frozen where it was
    cut: bool = False

    @property
    def over(self) -> bool:
        return self.ended or self.cut

    def freeze(self, now: float):
        if self.playing:
            self.t, self.playing = self.t + (now - self.at), False
        self.cut = not self.ended

    def heard(self, now: float) -> str:
        """The words of this part the person has heard by `now`."""
        if self.ended:
            return self.text
        if not (self.playing or self.t):
            return ""
        t = self.t + (now - self.at if self.playing else 0.0)
        said = [w for w in self.words if w.t0 <= t]
        return self.text[: said[-1].end] if said else ""


class Voice:
    def __init__(
        self,
        player: Player,
        engine: Engine,
        voice: str = VOICE,
        clock=time.monotonic,
        background: bool = True,
    ):
        self.player = player
        self.engine = engine
        self.voice = voice
        self.clock = clock
        self._cv = player._cv
        self.parts: list[Part] = []
        self.queue: deque[Part] = deque()
        self.chunkers: dict[int, Chunker] = {}
        self._ids = count(1)
        self._making: Part | None = None
        self._stopping = False
        # the book waits while there is anything to say
        player.hold = lambda: self.busy
        self._thread = None
        if background:
            self._thread = threading.Thread(target=self._generate, daemon=True)
            self._thread.start()

    @property
    def busy(self) -> bool:
        with self._cv:
            return bool(self.queue) or self._making is not None or any(
                not p.over for p in self.parts
            )

    # what the agent writes

    def text(self, call: int, piece: str):
        with self._cv:
            chunker = self.chunkers.setdefault(call, Chunker())
            self._queue(call, chunker.feed(piece))

    def end(self, call: int, kept: str | None):
        """A model call has finished. Cut off, nothing more of it is said."""
        with self._cv:
            chunker = self.chunkers.pop(call, None)
            if kept is None and chunker is not None:
                self._queue(call, chunker.flush())
            elif kept is not None and self.busy:
                self.stop()

    def _queue(self, call: int, chunks):
        for start, text in chunks:
            p = Part(next(self._ids), call, start, text)
            self.queue.append(p)
            log.info("queued part %d of call %d: %r", p.id, call, text)
        if chunks:
            self._cv.notify_all()

    # what the page says

    def report(self, id: int, t: float, playing: bool, ended: bool = False):
        with self._cv:
            p = next((p for p in self.parts if p.id == id), None)
            if p is None:
                return
            p.t, p.at, p.playing = t, self.clock(), playing and not ended
            if ended and not p.ended:
                p.ended = True
                log.info("part %d heard to the end", id)
                self._release_if_done()

    def heard(self, call: int, written: str) -> str:
        """How much of what `call` wrote the person has heard."""
        with self._cv:
            now = self.clock()
            parts = [p for p in self.parts if p.call == call]
            heard = ""
            for p in parts:
                h = p.heard(now)
                heard = written[: p.start] + h if h else heard
                if h != p.text:
                    break
            return heard

    def stop(self) -> dict[int, tuple[str, str]]:
        """Stop speaking at once: drop what is waiting, and what the page is playing.

        Returns, for each reply not heard to its end, what was heard of it and what
        was not, worked out at the moment of stopping.
        """
        with self._cv:
            if not self.busy:
                return {}
            now = self.clock()
            waiting = list(self.queue) + ([self._making] if self._making else [])
            # only replies still being spoken: one stopped before was reported then
            live = {p.call for p in self.parts if not p.over} | {p.call for p in waiting}
            for p in self.parts:
                if not p.over:
                    p.freeze(now)
            out = {}
            for call in sorted(live):
                said = [p for p in self.parts if p.call == call]
                text = "".join(p.text for p in sorted([*said, *(w for w in waiting if w.call == call)], key=lambda p: p.start))
                heard = self.heard(call, text)
                out[call] = (heard, text[len(heard):])
            self.queue.clear()
            self._making = None
            self.player.release(cancel=True)
            self.player._emit("voice-stop")
            log.info("stopped speaking: %s", {c: (len(h), len(u)) for c, (h, u) in out.items()})
            return out

    def part(self, id: int) -> Part | None:
        with self._cv:
            return next((p for p in self.parts if p.id == id and p.made), None)

    def close(self):
        with self._cv:
            self._stopping = True
            self._cv.notify_all()
        if self._thread is not None:
            self._thread.join(timeout=5)

    # making the audio

    def pump(self) -> bool:
        """Make the next part waiting, if there is one."""
        with self._cv:
            if not self.queue:
                return False
            p = self.queue.popleft()
            self._making = p
            # one voice at a time: the book stops when the agent starts to speak
            if self.player.state == PLAYING:
                self.player.pause(by="agent")
        began = time.monotonic()
        speech = self.engine.speak(p.text, voice=self.voice)
        with self._cv:
            if self._making is not p:
                log.info("dropped part %d: stopped while it was being made", p.id)
                return True
            self._making = None
            p.duration, p.words, p.wav, p.made = speech.duration, speech.words, speech.wav, True
            self.parts.append(p)
            log.info("made part %d in %.2fs -> %.2fs of audio", p.id, time.monotonic() - began, p.duration)
            self.player._emit(
                "voice-part", id=p.id, call=p.call, text=p.text, duration=p.duration,
                words=[[w.start, w.end, w.t0, w.t1] for w in p.words],
            )
        return True

    def _release_if_done(self):
        if not self.busy:
            self.player._emit("voice-done")
            self.player.release()

    def _generate(self):
        while True:
            with self._cv:
                self._cv.wait_for(lambda: self._stopping or bool(self.queue), POLL)
                if self._stopping:
                    return
            try:
                self.pump()
            except Exception as e:
                log.exception("making a voice part failed")
                with self._cv:
                    self._making = None
                    self.player._emit("error", message=repr(e))
