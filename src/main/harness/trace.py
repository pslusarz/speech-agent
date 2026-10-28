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
        content = [b.model_dump(exclude_none=True) for b in r.content]
        uses = [b["name"] for b in content if b["type"] == "tool_use"]
        title = f"response: {r.stop_reason}" + (f", calls {', '.join(uses)}" if uses else "")
        self.trace.add("response", title, dump(content))
        return r


class Trace:
    def __init__(self):
        self.steps: list[Step] = []
        self.version = 0
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
            self.add("entry", title, dump(e.content), said=said)
            return e

        def _put(ev, *a, **kw):
            if isinstance(ev, Event):
                title = f"event queued: {ev.kind} #{ev.mid}"
                self.add("event", title, ev.payload)
            return put(ev, *a, **kw)

        agent._append, agent.events.put = _append, _put
        return agent

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

    def add(self, kind: str, title: str, body: str = "", said=None):
        with self._cv:
            n = len(self.steps) + 1
            self.steps.append(Step(n, time.time(), kind, title, body, said))
            self.version += 1
            self._cv.notify_all()

    def wait(self, seen: int, timeout: float = 20.0) -> int:
        """Block until something happened after `seen`, or long enough to say so."""
        with self._cv:
            self._cv.wait_for(lambda: self.version != seen, timeout)
            return self.version
