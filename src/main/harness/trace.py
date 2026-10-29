"""What the exp10 agent did, step by step, for the debugger in app.py to show.

Three hooks, none of them inside agent.py: the model client is wrapped, so every
request and response is kept exactly as sent; `_append` is wrapped, so every entry
stored in the transcript is kept; and `events.put` is wrapped, so every result,
batch and timer the loop is told about is kept in the order it was queued.
"""

import json
import threading
import time
from dataclasses import dataclass

from .agent import Event


def dump(x) -> str:
    return json.dumps(x, indent=2, ensure_ascii=False)


@dataclass
class Step:
    n: int
    at: float
    # entry: stored in the transcript; event: queued for the loop; request,
    # response, error: one model call
    kind: str
    title: str
    body: str = ""
    # who said what, when the step is a line of the conversation the reader sees
    said: tuple[str, str] | None = None
    # the model call a reply came from, so the page can replace that call's draft
    call: int = 0


class Traced:
    """A model client that tells the trace what it was asked and what it said."""

    def __init__(self, cli, trace: "Trace"):
        self.cli = cli
        self.trace = trace

    @property
    def messages(self):
        return self

    def create(self, **kw):
        self.trace.request(kw["messages"])
        try:
            r = self.cli.messages.create(**kw)
        except Exception as e:
            self.trace.add("error", f"model call failed: {type(e).__name__}", str(e))
            raise
        self.trace.response(r)
        return r

    def stream(self, **kw):
        self.trace.request(kw["messages"])
        return TracedStream(self.cli.messages.stream(**kw), self.trace)


class TracedStream:
    """A model stream that tells the trace what it said once the message is complete."""

    def __init__(self, inner, trace: "Trace"):
        self.inner = inner
        self.trace = trace

    def __enter__(self):
        try:
            self.stream = self.inner.__enter__()
        except Exception as e:
            self.trace.add("error", f"model call failed: {type(e).__name__}", str(e))
            raise
        return self

    def __exit__(self, *exc):
        if exc[0] is not None:
            self.trace.add("error", f"model stream failed: {exc[0].__name__}", str(exc[1]))
        return self.inner.__exit__(*exc)

    def __iter__(self):
        return iter(self.stream)

    def get_final_message(self):
        r = self.stream.get_final_message()
        self.trace.response(r)
        return r


class Trace:
    def __init__(self):
        self.steps: list[Step] = []
        self.version = 0
        # text as the model writes it, for the page's drafts: dict(call, text), or
        # dict(call, cut) when a call was cut off, with what was kept of it
        self.live: list[dict] = []
        self._stored: set[str] = set()
        self._last: list[dict] = []
        self._requests = 0
        self._cv = threading.Condition()

    def client(self, cli) -> Traced:
        return Traced(cli, self)

    def attach(self, agent):
        append, put = agent._append, agent.events.put

        def _append(role, content, **kw):
            e = append(role, content, **kw)
            self._stored.update(b["text"] for b in e.content if b["type"] == "text")
            flags = " (aside)" if e.aside else ""
            said = None
            if e.role == "user" and e.kind == "said":
                said = ("user", e.text)
            elif agent._spoken(e):
                said = ("agent", e.text)
            title = f"stored #{e.id} {e.role} {e.kind}{flags}"
            self.add("entry", title, dump(e.content), said=said, call=e.call)
            return e

        def _put(ev, *a, **kw):
            if isinstance(ev, Event):
                title = f"event queued: {ev.kind} #{ev.mid}"
                self.add("event", title, ev.payload)
            return put(ev, *a, **kw)

        agent._append, agent.events.put = _append, _put
        agent.on_text.append(self.text)
        agent.on_end.append(self.ended)
        return agent

    def text(self, call: int, piece: str):
        """A piece of a reply being written: shown at once, never kept as a step."""
        with self._cv:
            self.live.append(dict(call=call, text=piece))
            self._cv.notify_all()

    def ended(self, call: int, kept: str | None):
        if kept is not None:
            with self._cv:
                self.live.append(dict(call=call, cut=kept))
                self._cv.notify_all()

    def response(self, r):
        content = [b.model_dump(exclude_none=True) for b in r.content]
        uses = [b["name"] for b in content if b["type"] == "tool_use"]
        title = f"response: {r.stop_reason}" + (f", calls {', '.join(uses)}" if uses else "")
        self.add("response", title, dump(content))

    def request(self, messages: list[dict]):
        """Keep only what changed since the last request; the rest is already shown."""
        self._requests += 1
        same = 0
        while same < min(len(messages), len(self._last)) and messages[same] == self._last[same]:
            same += 1
        self._last = messages
        last = messages[-1]["content"][-1]
        transient = last["type"] == "text" and last["text"] not in self._stored
        title = f"request #{self._requests}: {len(messages)} messages, {len(messages) - same} new"
        if transient:
            title += ", running notice appended (not stored)"
        self.add("request", title, dump(messages[same:]))

    def add(self, kind: str, title: str, body: str = "", said=None, call: int = 0):
        with self._cv:
            n = len(self.steps) + 1
            self.steps.append(Step(n, time.time(), kind, title, body, said, call))
            self.version += 1
            self._cv.notify_all()

    def wait(self, seen: int, timeout: float = 20.0) -> int:
        """Block until something happened after `seen`, or long enough to say so."""
        with self._cv:
            self._cv.wait_for(lambda: self.version != seen, timeout)
            return self.version

    def wait_live(self, seen: int, seen_live: int, timeout: float = 20.0) -> tuple[int, int]:
        """Block until a step or a piece of text arrived after what was seen."""
        with self._cv:
            self._cv.wait_for(
                lambda: self.version != seen or len(self.live) != seen_live, timeout
            )
            return self.version, len(self.live)
