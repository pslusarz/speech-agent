import copy
import logging
import queue
import threading
import time
from dataclasses import dataclass, field
from itertools import count

from ..bedrock import MODEL, raw_client
from .run import Runner
from .tools import KILLED, Tool

STOP = object()
DEFAULT_TIMEOUT = 30.0

TIMEOUT_ARG = dict(
    type="number",
    description=(
        "How long you expect this call to take, in seconds: the interval after which "
        "you would consider the result overdue."
    ),
)


@dataclass
class Event:
    kind: str
    mid: int
    payload: str = ""


def with_timeout(schema: dict) -> dict:
    s = copy.deepcopy(schema)
    s["input_schema"]["properties"]["timeout"] = TIMEOUT_ARG
    s["input_schema"]["required"] = [*s["input_schema"].get("required", ()), "timeout"]
    return s


log = logging.getLogger("speech.harness")

NOTICE = "[automatic notice from the harness, not from the person you are talking to]"

SP = (
    "You are talking with a person. Some of your tools run in the background: calling "
    "one starts the work and hands you a task id at once, and the outcome is reported "
    "to you later in an automatic notice. Keep talking with the person meanwhile.\n"
    "Before each of your turns you are shown the background work still running, with "
    "the results of anything started alongside it that has already finished. Use that "
    "to answer questions about progress; tail and kill are there if you need to look "
    "closer or stop something.\n"
    "Automatic notices come from the harness, not from the person. Task ids are "
    "bookkeeping between you and the harness: never quote one to the person. Do tell "
    "them what a task reported, including progress and partial results, in your own "
    "words as work you are doing."
)
# a turn is a ReAct loop; this only stops a runaway, it does not ration anything
MAX_STEPS = 6
QUOTE = 120


def quote(text: str) -> str:
    return repr(text if len(text) <= QUOTE else text[: QUOTE - 3] + "...")


@dataclass
class Call:
    """One background tool call, and the question it was made to answer."""

    id: int
    tool: str
    args: dict
    question: int
    # the assistant entry that made it; calls made in one breath settle as one batch
    batch: int
    result: str | None = None
    killed: bool = False

    @property
    def terminal(self) -> bool:
        return self.result is not None or self.killed

    @property
    def sig(self) -> str:
        return f"{self.tool}({', '.join(f'{k}={v!r}' for k, v in self.args.items())})"

    @property
    def outcome(self) -> str:
        if self.killed:
            return "was killed before it returned"
        if self.result is not None:
            return f"returned: {self.result}"
        return (
            f"still running. tail(task={self.id}) shows its progress; "
            f"kill(task={self.id}) stops it."
        )


@dataclass
class Entry:
    """One message in the transcript, in the shape the API takes it."""

    id: int
    role: str
    content: list[dict]
    # said: the person or the agent talking; tool_results; note: a harness notice
    kind: str = "said"
    question: int | None = None
    # words the harness asked for, kept for context but never shown to the reader
    aside: bool = False
    # for a reply: the last entry the model had been shown when it wrote it
    saw: int = 0
    # for a reply: the model call it came from, so a page can swap its draft for it
    call: int = 0
    at: float = field(default_factory=time.time)

    @property
    def text(self) -> str:
        return "".join(b["text"] for b in self.content if b["type"] == "text")


def started(c: Call) -> str:
    return (
        f"started in the background as task #{c.id}. Its outcome will be reported to "
        "you once it and anything started alongside it have finished. If the person "
        f"asks how it is going before then, tail(task={c.id}) shows its progress."
    )


def duplicate(c: Call) -> str:
    """Said only when it happens, so the edge case costs no context the rest of the time."""
    return (
        f"error: not started. {c.sig} is already running as task #{c.id}, and its "
        "outcome will be reported when it finishes. Tell the person it is already under "
        f"way; if they would rather start it fresh, kill(task={c.id}) and call it again."
    )


def overdue(c: Call, seconds: float) -> str:
    return (
        f"{NOTICE} Task #{c.id}, {c.sig}, has not finished within the {seconds:g}s you "
        f"expected of it. Look at how it is doing with tail(task={c.id}), then decide: if "
        "it is still making progress, leave it running; if it cannot make progress, "
        f"stop it with kill(task={c.id}) and carry on from there."
    )


def gave_up(tool: str, n: int) -> str:
    return (
        f"{KILLED}. {tool} has now been killed {n} time{'s' if n > 1 else ''} over this "
        "question and is not going to work, so do not start it again. Tell the person "
        "what you were trying to find out, that the tool keeps failing, and ask how they "
        "would like to proceed."
    )


class Agent:
    """A plain linear transcript, plus a list of what is still running.

    There is no tree. Each background call remembers the question it serves and the
    breath it was made in. Before every model call the harness appends a notice of
    the work still running, built fresh from that list and never stored, so the
    agent always sees current state and the transcript never accumulates stale
    snapshots. When every call made in one breath has settled, their outcomes are
    reported together as a stored notice, quoting the question if the person has
    said anything since.
    """

    def __init__(
        self,
        sp: str = "",
        model: str = MODEL,
        tools: list[Tool] = (),
        maxtok: int = 1024,
        max_auto: int = 4,
        max_retries: int = 2,
        timeout_cap: float = 60.0,
        client=None,
        notices=(),
        on_text=(),
        on_end=(),
        heard=None,
        on_idle=(),
        ends_turn=(),
    ):
        self.cli = client if client is not None else raw_client()
        self.model = model
        self.maxtok = maxtok
        self.max_auto = max_auto
        self.max_retries = max_retries
        self.timeout_cap = timeout_cap
        self.sp = f"{SP}\n\n{sp}" if sp else SP
        self.tools = {t.name: t for t in tools}
        # more current state shown before every model call, built fresh and never stored
        self.notices = list(notices)
        # told each piece of text as the model writes it: (model call, text)
        self.on_text = list(on_text)
        # told when a model call has finished writing: (model call, what is kept of it
        # if it was interrupted, else None)
        self.on_end = list(on_end)
        # how much of a call's text the person took in before they cut it off; all of
        # it by default, since they read it as it was written
        self.heard = heard or (lambda call, text: text)
        # told each time the loop has finished with an event and no turn is running
        self.on_idle = list(on_idle)
        # tools that have the last word: once one has run, the turn is over, since a
        # further step would have nothing to say and might say something anyway
        self.ends_turn = set(ends_turn)
        self._calls_made = count(1)
        self._busy = False
        self._interrupting = threading.Event()
        # the model call the last interruption cut short
        self.last_cut = 0
        self.schemas = [
            t.schema if t.synchronous else with_timeout(t.schema) for t in tools
        ]
        self.log: list[Entry] = []
        self.calls: dict[int, Call] = {}
        self.tasks: dict[int, Runner] = {}
        self.timers: dict[int, threading.Timer] = {}
        self.seen: dict[int, str] = {}
        self.errors: list[Exception] = []
        self.events: queue.Queue = queue.Queue()
        # batches whose outcome the agent has not been told yet
        self.owed: set[int] = set()
        self._auto: dict[int, int] = {}
        # a message posted mid-turn, and the reply of that turn which answered it
        self._covered: dict[int, int] = {}
        self._gave_up = False
        self._entries = count(1)
        self._tasks = count(1)
        self._cv = threading.Condition(threading.RLock())
        self._loop = threading.Thread(target=self._run, daemon=True)
        self._loop.start()

    # what a client or a test sees

    def post(self, text: str) -> Entry:
        e = self._append("user", text)
        e.question = e.id
        self.events.put(Event("user", e.id))
        return e

    def interrupt(self, timeout: float = 10.0) -> bool:
        """The person cut in: stop the turn under way, keeping what they took in of it.

        Returns once the turn has stopped and anything kept of it is in the transcript,
        so a message posted next comes after it, or False if no turn was running.
        """
        with self._cv:
            if not self._busy:
                return False
            # the loop thread leaves the stream at its next event. Closing the stream
            # from here instead leaves that thread blocked in its read for good
            self._interrupting.set()
        with self._cv:
            stopped = self._cv.wait_for(lambda: not self._busy, timeout)
        if not stopped:
            log.warning("the turn did not stop within %ss of being interrupted", timeout)
        return stopped

    def truncate(self, call: int, heard: str) -> bool:
        """Cut a stored reply back to what the person heard of it.

        Its tool calls stay, since they ran; if nothing is left at all, the entry is
        emptied and the model is no longer shown it.
        """
        with self._cv:
            e = next((e for e in self.log if e.role == "assistant" and e.call == call), None)
            if e is None or e.text == heard.rstrip():
                return False
            kept = heard.rstrip()
            uses = [b for b in e.content if b["type"] != "text"]
            e.content = ([dict(type="text", text=kept)] if kept else []) + uses
            log.info("cut stored reply #%d (call %d) to %d chars", e.id, call, len(kept))
            self._cv.notify_all()
            return True

    @property
    def busy(self) -> bool:
        return self._busy

    def notify(self, text: str) -> Entry:
        """Tell the agent something happened, without asking it to take a turn."""
        return self._append("user", f"{NOTICE} {text}", kind="note")

    def answered(self, q: int) -> bool:
        with self._cv:
            if any(self.calls[c].question == q for c in self._owing()):
                return False
            return self.answer_of(q) is not None

    def answer_of(self, q: int) -> Entry | None:
        with self._cv:
            said = [
                e
                for e in self.log
                if self._spoken(e) and (e.question == q or e.id == self._covered.get(q))
            ]
            return said[-1] if said else None

    def wait_for(self, target: str | Entry, timeout: float = 60) -> Entry:
        if isinstance(target, str):
            target = self.post(target)
        if not self.wait_until(lambda: self.answered(target.id), timeout):
            raise TimeoutError(f"no reply to #{target.id}: {self.errors}")
        return self.answer_of(target.id)

    def wait_until(self, pred, timeout: float = 60) -> bool:
        with self._cv:
            return self._cv.wait_for(pred, timeout)

    def transcript(self) -> list[tuple[str, str]]:
        """What the reader sees: the person, and the agent when it spoke to them."""
        with self._cv:
            return [
                ("user" if e.role == "user" else "agent", e.text)
                for e in self.log
                if (e.role == "user" and e.kind == "said") or self._spoken(e)
            ]

    def stop(self):
        for cid in list(self.timers):
            self._disarm(cid)
        self.events.put(STOP)
        self._loop.join()

    # what the meta tools call

    def kill(self, cid: int):
        r = self.tasks.get(cid)
        if r is None or r.killed:
            return
        self._disarm(cid)
        r.kill()
        with self._cv:
            self.calls[cid].killed = True
            self._cv.notify_all()
        self._settled(self.calls[cid])

    def note_kill(self, cid: int) -> str:
        """Say whether attempts at this call, for this question, have run out."""
        c = self.calls[cid]
        n = self._attempts(cid)
        if n <= self.max_retries:
            return KILLED
        self._gave_up = True
        return gave_up(c.tool, n)

    def _attempts(self, cid: int) -> int:
        """How many times this exact call has been killed while serving its question.

        The arguments are part of the key, so three cities asked side by side are
        three lines of attempt and do not spend each other's retries.
        """
        c = self.calls[cid]
        return sum(
            o.killed and (o.question, o.tool, o.args) == (c.question, c.tool, c.args)
            for o in self.calls.values()
        )

    # the transcript

    def _append(self, role: str, content, **kw) -> Entry:
        if isinstance(content, str):
            content = [dict(type="text", text=content)]
        with self._cv:
            e = Entry(next(self._entries), role, content, **kw)
            self.log.append(e)
            self._cv.notify_all()
            return e

    def _spoken(self, e: Entry) -> bool:
        return e.role == "assistant" and not e.aside and bool(e.text.strip())

    def _owing(self) -> list[int]:
        return [c.id for c in self.calls.values() if c.batch in self.owed]

    def running(self) -> str | None:
        """The notice of work still in flight, grouped by the question it serves."""
        with self._cv:
            live = {c.batch for c in self.calls.values() if not c.terminal}
            if not live:
                return None
            lines = [f"{NOTICE} Background work you started is still running."]
            by_q: dict[int, list[Call]] = {}
            for c in self.calls.values():
                if c.batch in live:
                    by_q.setdefault(c.question, []).append(c)
            for q, calls in by_q.items():
                lines.append(f"Started for the person's message {quote(self._said(q))}:")
                lines += [f"- task #{c.id} {c.sig}: {c.outcome}" for c in calls]
            lines.append(
                "Outcomes are reported together once everything started alongside "
                "them has finished, so a result shown here has not been reported yet."
            )
            return "\n".join(lines)

    def render(self) -> list[dict]:
        with self._cv:
            msgs: list[dict] = []
            for e in self.log:
                # a reply the person cut off before any of it was heard
                if not e.content:
                    continue
                blocks = copy.deepcopy(e.content)
                if msgs and msgs[-1]["role"] == e.role:
                    msgs[-1]["content"] += blocks
                else:
                    msgs.append(dict(role=e.role, content=blocks))
            # current state, never stored: the next turn gets a fresh one
            for note in [self.running(), *(n() for n in self.notices)]:
                if not note:
                    continue
                if msgs[-1]["role"] != "user":
                    msgs.append(dict(role="user", content=[]))
                msgs[-1]["content"].append(dict(type="text", text=note))
            return msgs

    def _said(self, q: int) -> str:
        return next(e.text for e in self.log if e.id == q)

    def _spoken_since(self, q: int) -> bool:
        return any(
            e.role == "user" and e.kind == "said" and e.id > q for e in self.log
        )

    # the event loop

    def _run(self):
        while (ev := self.events.get()) is not STOP:
            with self._cv:
                self._busy = True
                # a cut-in meant for an earlier turn is not this one's to honour
                self._interrupting.clear()
            try:
                if ev.kind == "user":
                    self._take_turn(ev.mid, trigger="user")
                elif ev.kind == "overdue":
                    self._overdue(ev)
                elif ev.kind == "batch":
                    self._report(ev.mid)
                else:
                    self._result(ev)
            except Exception as e:
                log.exception("event loop failed on %s", ev)
                self.errors.append(e)
            with self._cv:
                self._busy = False
                self._cv.notify_all()
            for f in self.on_idle:
                try:
                    f()
                except Exception as e:
                    log.exception("an idle hook failed")
                    self.errors.append(e)

    def _call(self, trigger: str = "user") -> tuple:
        """Ask the model, streaming, and say how far into the transcript it could see.

        Text is passed to on_text as it arrives, except in a turn the timer asked for,
        whose words are the agent's own business until it decides otherwise. If the
        person cuts in, the stream is left at its next event, whatever kind, and the
        message is None; the text written until then comes back instead.
        """
        with self._cv:
            saw = self.log[-1].id
            messages = self.render()
        n = next(self._calls_made)
        kw = dict(tools=self.schemas) if self.schemas else {}
        written: list[str] = []
        r = None
        try:
            with self.cli.messages.stream(
                model=self.model,
                max_tokens=self.maxtok,
                system=self.sp,
                messages=messages,
                **kw,
            ) as stream:
                for event in stream:
                    if self._interrupting.is_set():
                        break
                    if event.type != "content_block_delta" or event.delta.type != "text_delta":
                        continue
                    written.append(event.delta.text)
                    if trigger != "overdue":
                        for f in self.on_text:
                            f(n, event.delta.text)
                else:
                    r = stream.get_final_message()
        except Exception:
            # the connection dropping as it is left is no failure of the turn
            if not self._interrupting.is_set():
                raise
        return r, saw, n, "".join(written)

    def _heard(self) -> int:
        """How far the agent had read when it last spoke to the person."""
        spoken = [e for e in self.log if self._spoken(e)]
        return spoken[-1].saw if spoken else 0

    def _unheard(self, saw: int) -> bool:
        """Whether the person said something, up to `saw`, not yet spoken back to."""
        heard = self._heard()
        return any(
            e.role == "user" and e.kind == "said" and heard < e.id <= saw
            for e in self.log
        )

    def _take_turn(self, question: int, trigger: str):
        with self._cv:
            # posted while a turn was running, and that turn has seen and answered it;
            # another turn would ask the model to continue its own reply
            if trigger == "user" and self._heard() >= question:
                self._covered[question] = [e for e in self.log if self._spoken(e)][-1].id
                return
        self._gave_up = False
        for _ in range(MAX_STEPS):
            if self._interrupting.is_set():
                return
            r, saw, n, written = self._call(trigger)
            if r is None:
                self._cut(question, saw, n, written)
                return
            for f in self.on_end:
                f(n, None)
            content = [
                b.model_dump(include={"type", "text", "id", "name", "input"})
                for b in r.content
                if b.type in ("text", "tool_use")
            ]
            uses = [b for b in r.content if b.type == "tool_use"]
            # the API wants a tool_use answered by the very next message, so a person
            # posting meanwhile waits on the lock until the results are kept
            with self._cv:
                if content:
                    node = self._append(
                        "assistant",
                        content,
                        question=question,
                        # a turn the timer asked for is the agent's own business, unless
                        # it has given up, or the person spoke and this is the reply
                        aside=trigger == "overdue"
                        and not self._gave_up
                        and not self._unheard(saw),
                        saw=saw,
                        call=n,
                    )
                if not uses:
                    return
                before = len(self.calls)
                results = [
                    dict(
                        type="tool_result",
                        tool_use_id=u.id,
                        content=self._use(u, question, node.id),
                    )
                    for u in uses
                ]
                self._append("user", results, kind="tool_results", question=question)
                began = len(self.calls) - before == len(uses)
            if any(u.name in self.ends_turn for u in uses):
                return
            # it said something and only started background work, so there is nothing
            # to add until that work reports; another step would only say "still waiting"
            if began and node.text.strip():
                return
            # otherwise every result is read on the next step: a look, a kill, a
            # refusal, or a start the person has not been told about yet

    def _cut(self, question: int, saw: int, n: int, written: str):
        """Keep what the person took in of a reply they cut off, and nothing else.

        A tool call the model had begun is dropped with the rest: its input was never
        finished, and nothing was run for it.
        """
        self.last_cut = n
        kept = self.heard(n, written).rstrip()
        log.info("interrupted call %d: wrote %d chars, kept %d", n, len(written), len(kept))
        for f in self.on_end:
            f(n, kept)
        with self._cv:
            if kept:
                self._append("assistant", kept, question=question, saw=saw, call=n)
            lost = written[len(kept):].strip() if written.startswith(kept) else written.strip()
            if lost:
                self.notify(
                    "The person cut you off there. They did not take in the rest of "
                    f"what you were writing: {quote(lost)}"
                )

    def _use(self, u, question: int, batch: int) -> str:
        tool = self.tools.get(u.name)
        if tool is None:
            return f"error: there is no tool called {u.name}"
        if tool.synchronous:
            return tool.fn(self, **u.input)
        args = dict(u.input)
        due = args.pop("timeout", None)
        try:
            task = tool.fn(**args)
        except Exception as e:
            return f"failed: {e!r}"
        with self._cv:
            same = next(
                (
                    c
                    for c in self.calls.values()
                    if not c.terminal and (c.tool, c.args) == (u.name, args)
                ),
                None,
            )
            if same is not None:
                return duplicate(same)
            c = Call(next(self._tasks), u.name, args, question, batch)
            self.calls[c.id] = c
            self.owed.add(batch)
        self.tasks[c.id] = r = Runner(c.id, task, self._on_done)
        r.start()
        self._arm(c.id, due)
        return started(c)

    def _on_done(self, r: Runner):
        self.events.put(Event("result", r.id, payload=r.future.result()))

    def _result(self, ev: Event):
        self._disarm(ev.mid)
        r = self.tasks.get(ev.mid)
        if r is None or r.killed:
            return
        with self._cv:
            self.calls[ev.mid].result = ev.payload
        self._settled(self.calls[ev.mid])

    def _settled(self, c: Call):
        with self._cv:
            batch = [o for o in self.calls.values() if o.batch == c.batch]
            if all(o.terminal for o in batch):
                self.events.put(Event("batch", c.batch))

    def _report(self, batch: int):
        with self._cv:
            if batch not in self.owed:
                return
            calls = [c for c in self.calls.values() if c.batch == batch]
            q = calls[0].question
        try:
            # nothing came back, so the turn that killed them has already said so
            if all(c.killed for c in calls):
                return
            spent = self._auto.get(q, 0)
            if spent >= self.max_auto:
                return
            self._auto[q] = spent + 1
            lines = [f"{NOTICE} Background work you started has finished:"]
            lines += [f"- {c.sig} {c.outcome}" for c in calls]
            if self._spoken_since(q):
                lines.append(
                    f"It was started for the person's earlier message {quote(self._said(q))}, "
                    "and the conversation has moved on since, so say which question "
                    "you are answering."
                )
            lines.append(
                "Answer the person now in words. Start another task only if you "
                "cannot answer without it."
            )
            self._append("user", "\n".join(lines), kind="note", question=q)
            self._take_turn(q, trigger="result")
        finally:
            with self._cv:
                self.owed.discard(batch)

    # timers

    def _arm(self, cid: int, due: float | None):
        seconds = min(float(due or DEFAULT_TIMEOUT), self.timeout_cap)
        t = threading.Timer(seconds, self._fire, args=(cid, seconds))
        t.daemon = True
        self.timers[cid] = t
        t.start()

    def _fire(self, cid: int, seconds: float):
        self.events.put(Event("overdue", cid, payload=repr(seconds)))

    def _disarm(self, cid: int):
        t = self.timers.pop(cid, None)
        if t is not None:
            t.cancel()

    def _overdue(self, ev: Event):
        self._disarm(ev.mid)
        r = self.tasks.get(ev.mid)
        if r is None or r.done:
            return
        c = self.calls[ev.mid]
        seconds = float(ev.payload)
        self._append("user", overdue(c, seconds), kind="note", question=c.question)
        self._take_turn(c.question, trigger="overdue")
        # left running rather than killed, so ask again after the same interval
        if not r.done:
            self._arm(ev.mid, seconds)
