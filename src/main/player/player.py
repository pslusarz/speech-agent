"""The player: one chapter read aloud, a few minutes of audio prepared ahead of the ear.

The browser plays the audio and reports where it is; between reports the player
assumes the clock. Everything it is asked for - the word being heard, the sentence to
resume at, how much audio is ready - comes from that position and the word timings of
the segments it has prepared.

Audio is made a segment at a time, a few sentences from one paragraph each. While less
than REFILL_AT seconds are ready beyond what is being heard, it makes more until AHEAD
seconds are, so playback never waits once it has begun and nothing is made far ahead
of a listener who may stop at any moment.

Each new reading is a new session. A segment made for an old session is dropped when
it lands, and the page ignores anything tagged with a session it has moved on from.
"""

import logging
import os
import threading
import time
from dataclasses import dataclass, field
from itertools import count

from .book import Book
from .tts import Engine, Word

AHEAD = float(os.getenv("PLAYER_AHEAD_SECONDS", 300))
REFILL_AT = float(os.getenv("PLAYER_REFILL_AT_SECONDS", 60))
# a segment is whole sentences from one paragraph, up to about this many characters
SEGMENT_CHARS = int(os.getenv("PLAYER_SEGMENT_CHARS", 400))
# how much of the text just heard tail() gives back
TAIL_CHARS = 600
# the generator looks again this often, since the position moves with the clock
POLL = 0.5

log = logging.getLogger("speech.player")

IDLE, PLAYING, PAUSED, FINISHED = "idle", "playing", "paused", "finished"


@dataclass(frozen=True)
class Segment:
    id: int
    session: int
    chapter: int
    start: int
    end: int
    duration: float
    # offsets into the chapter, times into this segment
    words: tuple[Word, ...]
    wav: bytes = field(repr=False)

    def word_at(self, t: float) -> Word | None:
        heard = [w for w in self.words if w.t0 <= t]
        return heard[-1] if heard else None

    def time_of(self, offset: int) -> float:
        """When the first word at or after `offset` begins."""
        return next((w.t0 for w in self.words if w.start >= offset), 0.0)


def clock_time(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    return f"{m}:{s:02d}"


class Player:
    def __init__(
        self,
        book: Book,
        engine: Engine,
        clock=time.monotonic,
        ahead: float = AHEAD,
        refill_at: float = REFILL_AT,
        segment_chars: int = SEGMENT_CHARS,
        background: bool = True,
    ):
        self.book = book
        self.engine = engine
        self.clock = clock
        self.ahead_target = ahead
        self.refill_at = refill_at
        self.segment_chars = segment_chars
        self.state = IDLE
        self.chapter: int | None = None
        self.session = 0
        self.segments: list[Segment] = []
        # where the next segment starts; None once the chapter has all been made
        self.next_offset: int | None = None
        self._pos = (0, 0.0)
        self._pos_at = clock()
        # where the person was in the text when they last spoke over the reading
        self.mark: int | None = None
        self.last_by: str | None = None
        # while this says so, a play or resume the agent asks for waits, and no audio is
        # made for the book: the agent's own voice is speaking
        self.hold = lambda: False
        self.pending: tuple | None = None
        self.events: list[dict] = []
        self.errors: list[Exception] = []
        self._ids = count(1)
        self._cv = threading.Condition(threading.RLock())
        self._stopping = False
        self._thread = None
        if background:
            self._thread = threading.Thread(target=self._generate, daemon=True)
            self._thread.start()

    # where the ear is

    def position(self) -> tuple[int, float]:
        """Segment index and seconds into it, carried forward by the clock while playing."""
        with self._cv:
            i, t = self._pos
            if self.state != PLAYING or not self.segments:
                return i, t
            t += self.clock() - self._pos_at
            while i < len(self.segments) - 1 and t >= self.segments[i].duration:
                t -= self.segments[i].duration
                i += 1
            # past the last segment made: the page is waiting on the next one
            return i, min(t, self.segments[i].duration)

    def offset(self) -> int:
        """The chapter offset of the word being heard."""
        with self._cv:
            if not self.segments:
                return self.next_offset or 0
            i, t = self.position()
            seg = self.segments[i]
            w = seg.word_at(t)
            return w.start if w else seg.start

    def ready_ahead(self) -> float:
        """Seconds of audio made beyond what is being heard."""
        with self._cv:
            if not self.segments:
                return 0.0
            i, t = self.position()
            return sum(s.duration for s in self.segments[i:]) - t

    def elapsed(self) -> float:
        with self._cv:
            i, t = self.position()
            return sum(s.duration for s in self.segments[:i]) + t

    # what the person or the agent can do

    def play(self, chapter: int, offset: int = 0, by: str = "agent") -> int:
        """Start reading `chapter` from the sentence containing `offset`."""
        c = self.book[chapter]
        start = c.sentence_at(offset)[0]
        with self._cv:
            if by == "agent" and self.hold():
                self.pending = ("play", chapter, start)
                log.info("play of chapter %d at %d held until the voice is done", chapter, start)
                self._emit("held", action="play", start=start)
                return start
            self.pending = None
            self.session += 1
            self.chapter = chapter
            self.segments = []
            self.next_offset = start
            self.state = PLAYING
            self.mark = None
            self.last_by = by
            self._set_pos(0, 0.0)
            self._emit("session", chapter=chapter, start=start, by=by)
            self._cv.notify_all()
        return start

    def pause(self, by: str = "agent") -> bool:
        with self._cv:
            if self.state != PLAYING:
                return False
            self._set_pos(*self.position())
            self.state = PAUSED
            self.last_by = by
            self._emit("pause", by=by)
            return True

    def resume(self, offset: int | None = None, by: str = "agent") -> int:
        """Carry on from the start of a sentence.

        With no offset, that is the sentence the person was hearing when they last
        spoke over the reading, or else the one where it was paused.
        """
        with self._cv:
            if self.chapter is None:
                raise ValueError("nothing has been played yet")
            target = offset if offset is not None else (self.mark if self.mark is not None else self.offset())
            start = self.book[self.chapter].sentence_at(target)[0]
            if by == "agent" and self.hold():
                self.pending = ("resume", start)
                log.info("resume at %d held until the voice is done", start)
                self._emit("held", action="resume", start=start)
                return start
            self.pending = None
            i = next(
                (k for k, s in enumerate(self.segments) if s.start <= start < s.end), None
            )
            if i is None:
                # not among the segments made, so read afresh from there
                return self.play(self.chapter, start, by=by)
            self._set_pos(i, self.segments[i].time_of(start))
            self.state = PLAYING
            self.mark = None
            self.last_by = by
            log.info("resume %s target=%d sentence=%d in segment %d", by, target, start, i)
            self._emit("play", index=i, t=self._pos[1], by=by)
            self._cv.notify_all()
            return start

    def release(self, cancel: bool = False):
        """Carry out the play or resume held, unless cancelled, once nothing holds it."""
        with self._cv:
            if self.pending is None or (not cancel and self.hold()):
                return
            held, self.pending = self.pending, None
            if cancel:
                log.info("dropped the held %s: the person cut in", held[0])
                self._emit("released", action=held[0], cancelled=True)
                return
            log.info("carrying out the held %s", held[0])
            if held[0] == "play":
                self.play(held[1], held[2])
            else:
                self.resume(held[1])

    def heard_message(self):
        """The person spoke while it read: remember where, so resume() can go back there."""
        with self._cv:
            if self.state == PLAYING:
                self.mark = self.offset()
                log.info("person spoke while playing: mark=%d", self.mark)

    def report(self, session: int, index: int, t: float):
        """The page says where playback is. A report from an old session is ignored."""
        with self._cv:
            if session != self.session or not 0 <= index < len(self.segments):
                log.debug("report ignored: session=%d index=%d (now session=%d, %d segments)",
                          session, index, self.session, len(self.segments))
                return
            log.debug("report session=%d index=%d t=%.2f (clock said %s)",
                      session, index, t, self.position())
            self._set_pos(index, t)
            if (
                self.state == PLAYING
                and self.next_offset is None
                and index == len(self.segments) - 1
                and t >= self.segments[index].duration
            ):
                self.state = FINISHED
                self._emit("finished")
            self._cv.notify_all()

    def stop(self):
        with self._cv:
            self._stopping = True
            self._cv.notify_all()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def snapshot(self) -> dict:
        """Everything a page that has just opened needs, as one event."""
        with self._cv:
            i, t = self.position()
            return dict(
                kind="sync",
                session=self.session,
                n=len(self.events),
                state=self.state,
                chapter=self.chapter,
                index=i,
                t=t,
                last=self.next_offset is None,
                segments=[
                    dict(
                        index=k,
                        id=s.id,
                        start=s.start,
                        end=s.end,
                        duration=s.duration,
                        words=[[w.start, w.end, w.t0, w.t1] for w in s.words],
                    )
                    for k, s in enumerate(self.segments)
                ],
            )

    def segment(self, id: int) -> Segment | None:
        with self._cv:
            return next((s for s in self.segments if s.id == id), None)

    # what the agent is shown

    def status(self) -> dict:
        with self._cv:
            out = dict(state=self.state, by=self.last_by)
            if self.pending is not None:
                out.update(held=self.pending[0])
                if self.pending[0] == "play":
                    out.update(held_chapter=self.pending[1])
            if self.chapter is None:
                return out
            c = self.book[self.chapter]
            at = self.offset()
            s0, s1 = c.sentence_at(at)
            out.update(
                chapter=c.id,
                title=c.title,
                offset=at,
                length=len(c.text),
                paragraph=c.paragraph_index(at),
                sentence=c.text[s0:s1],
                elapsed=clock_time(self.elapsed()),
                ready=clock_time(self.ready_ahead()),
            )
            if self.mark is not None:
                m0, m1 = c.sentence_at(self.mark)
                out.update(mark=self.mark, mark_sentence=c.text[m0:m1])
            return out

    def tail(self, chars: int = TAIL_CHARS) -> str:
        """The text heard most recently, up to the end of the sentence being heard."""
        with self._cv:
            if self.chapter is None:
                return ""
            c = self.book[self.chapter]
            end = c.sentence_at(self.offset())[1]
            start = c.sentence_at(max(0, end - chars))[0]
            return c.text[start:end]

    # making audio

    def _set_pos(self, i: int, t: float):
        self._pos = (i, t)
        self._pos_at = self.clock()

    def _emit(self, kind: str, **data):
        self.events.append(dict(kind=kind, session=self.session, n=len(self.events) + 1, **data))
        shown = {k: v for k, v in data.items() if k != "words"}
        log.info("event %s session=%d %s", kind, self.session, shown)
        self._cv.notify_all()

    def wait(self, seen: int, timeout: float = 20.0) -> int:
        """Block until there are more than `seen` events, or long enough to say so."""
        with self._cv:
            self._cv.wait_for(lambda: len(self.events) > seen, timeout)
            return len(self.events)

    def wait_until_ready(self, timeout: float = 20.0) -> bool:
        """Block until the first segment of the current reading has been made."""
        with self._cv:
            return self._cv.wait_for(lambda: bool(self.segments), timeout)

    def needs_audio(self) -> bool:
        with self._cv:
            return (
                self.state in (PLAYING, PAUSED)
                and self.next_offset is not None
                and not self.hold()
                and self.ready_ahead() < self.refill_at
            )

    def fill(self):
        """Make segments until AHEAD seconds are ready, if fewer than REFILL_AT are."""
        if not self.needs_audio():
            return
        while True:
            with self._cv:
                if self.next_offset is None or self.ready_ahead() >= self.ahead_target:
                    return
                if self.state not in (PLAYING, PAUSED):
                    return
                session, chapter = self.session, self.chapter
                start, end = self._span(self.book[chapter], self.next_offset)
                text = self.book[chapter].text[start:end]
            began = time.monotonic()
            speech = self.engine.speak(text)
            log.info("made %d chars in %.2fs -> %.2fs of audio, %d words timed",
                     len(text), time.monotonic() - began, speech.duration, len(speech.words))
            with self._cv:
                # a new reading began while this was being made
                if session != self.session:
                    log.info("dropped a segment made for old session %d", session)
                    continue
                seg = Segment(
                    next(self._ids),
                    session,
                    chapter,
                    start,
                    end,
                    speech.duration,
                    tuple(Word(w.start + start, w.end + start, w.t0, w.t1) for w in speech.words),
                    speech.wav,
                )
                self.segments.append(seg)
                self.next_offset = self._after(self.book[chapter], end)
                # the clock only runs once there is something to hear
                if len(self.segments) == 1:
                    self._set_pos(0, 0.0)
                self._emit(
                    "segment",
                    index=len(self.segments) - 1,
                    id=seg.id,
                    start=seg.start,
                    end=seg.end,
                    duration=seg.duration,
                    words=[[w.start, w.end, w.t0, w.t1] for w in seg.words],
                    last=self.next_offset is None,
                )

    def _span(self, c, offset: int) -> tuple[int, int]:
        """Whole sentences from `offset`, within its paragraph, up to segment_chars."""
        p_end = c.paragraph_at(offset)[1]
        end = c.sentence_at(offset)[1]
        for s0, s1 in c.sentences:
            if s0 < end:
                continue
            if s1 > p_end or s1 - offset > self.segment_chars:
                break
            end = s1
        return offset, end

    def _after(self, c, end: int) -> int | None:
        nxt = next((s0 for s0, _ in c.sentences if s0 >= end), None)
        return nxt

    def _generate(self):
        while True:
            with self._cv:
                self._cv.wait_for(lambda: self._stopping or self.needs_audio(), POLL)
                if self._stopping:
                    return
            try:
                self.fill()
            except Exception as e:
                with self._cv:
                    self.errors.append(e)
                    self._emit("error", message=repr(e))
                    # one failure should not become a tight loop of them
                    self._cv.wait(POLL)
