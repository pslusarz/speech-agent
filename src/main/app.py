"""Chat with an agent that reads the book aloud, beside the player and the text.

    uv run uvicorn main.app:app --port 8000

The left side is the conversation. The right side is the player - its buttons drive
the same player the agent does - and the chapter being read, the word being heard
lit up. Under the chat, what the model would be sent if it were asked now.
"""

import asyncio
import json
import logging
import os
import re
from html import escape
from pathlib import Path

from fasthtml.common import (
    Audio,
    Button,
    Details,
    Div,
    EventStream,
    Form,
    Input,
    Main,
    NotStr,
    Option,
    P,
    Pre,
    Response,
    Script,
    Select,
    Span,
    Style,
    Summary,
    Titled,
    fast_app,
    serve,
    sse_message,
)

from .bedrock import raw_client
from .harness.agent import Agent
from .harness.trace import Trace, dump
from .player.book import Book
from .player.player import Player
from .player.tools import SP, person_did, status_line, tools
from .player.tts import Kokoro

STATIC = Path(__file__).parent / "static"
LOG_FILE = Path(os.getenv("LOG_FILE", Path(__file__).resolve().parents[2] / "logs" / "speech-agent.log"))
# how much of a model request or tool result goes into one log line
LOG_BODY = 2000
log = logging.getLogger("speech.app")
client_log = logging.getLogger("speech.page")


def setup_logging():
    """One file for the server, the agent and the page, so a session can be read back."""
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s.%(msecs)03d %(name)-13s %(levelname)-5s %(message)s", "%H:%M:%S")
    root = logging.getLogger("speech")
    if root.handlers:
        return
    root.setLevel(os.getenv("LOG_LEVEL", "DEBUG"))
    for h in (logging.FileHandler(LOG_FILE), logging.StreamHandler()):
        h.setFormatter(fmt)
        root.addHandler(h)


def log_steps(trace: Trace):
    """Every step the debugger keeps goes to the log too: entries, events, model calls."""
    add = trace.add

    def _add(kind, title, body="", said=None):
        text = body if len(body) <= LOG_BODY else body[:LOG_BODY] + f"... ({len(body)} chars)"
        logging.getLogger("speech.agent").info("%s: %s\n%s", kind, title, text)
        return add(kind, title, body, said=said)

    trace.add = _add
    return trace
WORD = re.compile(r"\S+")

CSS = """
main.container { max-width:none; }
#view { display:grid; grid-template-columns:minmax(20rem,1fr) minmax(28rem,1.2fr); gap:1.5rem; }
#transcript { display:flex; flex-direction:column; gap:.6rem; margin-bottom:1rem; }
.row { display:flex; }
.row.user { justify-content:flex-end; }
.bubble { max-width:40rem; padding:.55rem .85rem; border-radius:1rem; white-space:pre-wrap; }
.user .bubble { background:#1f6feb; color:#fff; border-bottom-right-radius:.25rem; }
.agent .bubble { background:#f1f3f5; color:#111; border-bottom-left-radius:.25rem; }
form { display:flex; gap:.5rem; }
input[name=msg] { flex:1; padding:.55rem .75rem; }
#controls { display:flex; gap:.5rem; align-items:center; flex-wrap:wrap; }
#controls select { width:auto; margin:0; }
#controls button { margin:0; padding:.35rem .8rem; }
#player-note { color:#57606a; font-size:.85rem; min-height:1.2rem; }
#text { max-height:70vh; overflow:auto; font-family:Georgia,serif; line-height:1.6; padding-right:.5rem; }
#text span[data-s] { cursor:pointer; border-radius:.2rem; }
#text span.lit { background:#ffe58f; }
.debug pre { font-size:.72rem; white-space:pre-wrap; max-height:40vh; overflow:auto; }
"""

# the agent speaks without being asked, so the transcript is pushed to the page
STREAM = """
const chat = new EventSource('/events?after=' + AFTER);
chat.addEventListener('said', m => document.getElementById('transcript')
  .append(document.createRange().createContextualFragment(m.data)));
"""


def bubble(said):
    who, text = said
    return Div(Span(text, cls="bubble"), cls=f"row {who}")


def chapter_html(c) -> str:
    """The chapter as paragraphs of words, each word tagged with its offset."""
    out = []
    for s, e in c.paragraphs:
        words = " ".join(
            f'<span data-s="{s + m.start()}">{escape(m.group())}</span>'
            for m in WORD.finditer(c.text[s:e])
        )
        out.append(f"<p>{words}</p>")
    return "".join(out)


def make_app(agent: Agent, player: Player, trace: Trace):
    # fasthtml serves any path ending in a static extension from static_path itself,
    # ahead of every route, so /static/player.js is src/main/static/player.js
    app, rt = fast_app(pico=True, hdrs=(Style(CSS),), static_path=str(STATIC.parent))
    book = player.book

    def by_person(action: str):
        agent.notify(person_did(player, action))

    @rt("/")
    def index():
        said = [s.said for s in trace.steps[: trace.version] if s.said]
        controls = Div(
            Select(
                *[Option(f"{c.id}. {c.title}", value=c.id) for c in book.chapters],
                name="chapter",
                id="chapter",
            ),
            Button("Play chapter", hx_post="/player/play", hx_include="#chapter", hx_swap="none"),
            Button("Pause", hx_post="/player/pause", hx_swap="none", cls="secondary"),
            Button("Resume", hx_post="/player/resume", hx_swap="none", cls="secondary"),
            id="controls",
        )
        return Titled(
            book.title,
            Main(
                Div(
                    Div(
                        Div(*[bubble(s) for s in said], id="transcript"),
                        Form(
                            Input(name="msg", placeholder="Ask something, or ask it to read...",
                                  autofocus=True, autocomplete="off"),
                            Button("Send"),
                            hx_post="/say",
                            hx_swap="none",
                            hx_on__after_request="this.reset()",
                        ),
                        Details(
                            Summary("what the model would be sent now"),
                            Pre("", id="model-view"),
                            cls="debug",
                            hx_get="/model-view",
                            hx_target="#model-view",
                            hx_trigger="toggle",
                        ),
                    ),
                    Div(
                        controls,
                        P("", id="player-note"),
                        Audio(id="audio", preload="auto"),
                        Div(P("Pick a chapter and press play, or ask the agent to read."), id="text"),
                    ),
                    id="view",
                )
            ),
            Script(f"const AFTER = {trace.version};\n{STREAM}"),
            Script(src="/static/player.js"),
        )

    @rt("/events")
    async def events(request, after: int = 0):
        seen = int(request.headers.get("last-event-id") or after)

        async def stream():
            nonlocal seen
            while True:
                now = await asyncio.to_thread(trace.wait, seen)
                for s in trace.steps[seen:now]:
                    if s.said:
                        yield f"id: {s.n}\n" + sse_message(bubble(s.said), "said")
                seen = now

        return EventStream(stream())

    @rt("/model-view")
    def model_view():
        with agent._cv:
            view = agent.render() if agent.log else []
        return dump(view)

    @rt("/say")
    def say(msg: str = ""):
        if msg.strip():
            log.info("person said: %r (player %s)", msg.strip(), player.state)
            player.heard_message()
            agent.post(msg.strip())
        return ""

    @rt("/player/events")
    async def player_events():
        async def stream():
            snap = player.snapshot()
            yield f"event: player\ndata: {json.dumps(snap)}\n\n"
            seen = snap["n"]
            while True:
                now = await asyncio.to_thread(player.wait, seen)
                for e in player.events[seen:now]:
                    yield f"event: player\ndata: {json.dumps(e)}\n\n"
                seen = now

        return EventStream(stream())

    @rt("/player/play", methods=["post"])
    def play(chapter: int, offset: int = 0):
        if not 0 <= chapter < len(book.chapters):
            return Response("no such chapter", status_code=400)
        log.info("person pressed play: chapter=%d offset=%d", chapter, offset)
        player.play(chapter, max(0, offset), by="person")
        by_person("started")
        return ""

    @rt("/player/pause", methods=["post"])
    def pause():
        log.info("person pressed pause (player was %s)", player.state)
        if player.pause(by="person"):
            by_person("paused")
        return ""

    @rt("/player/resume", methods=["post"])
    def resume():
        if player.chapter is None:
            return Response("nothing has been played yet", status_code=400)
        log.info("person pressed resume (player was %s, mark=%s)", player.state, player.mark)
        player.resume(by="person")
        by_person("resumed")
        return ""

    @rt("/log", methods=["post"])
    async def page_log(request):
        """What the page saw: audio element events, server events received, errors."""
        try:
            entries = json.loads(await request.body())
        except ValueError:
            return Response("not json", status_code=400)
        for e in entries if isinstance(entries, list) else []:
            if isinstance(e, dict):
                client_log.info("%s %s", str(e.get("kind", "?"))[:40], json.dumps(e.get("data"))[:LOG_BODY])
        return ""

    @rt("/player/report", methods=["post"])
    def report(session: int, index: int, t: float):
        player.report(session, index, max(0.0, t))
        return ""

    @rt("/audio/{id}")
    def audio(id: int):
        seg = player.segment(id)
        if seg is None:
            return Response("gone", status_code=404)
        return Response(seg.wav, media_type="audio/wav")

    @rt("/chapter/{id}")
    def chapter(id: int):
        if not 0 <= id < len(book.chapters):
            return Response("no such chapter", status_code=404)
        return NotStr(chapter_html(book[id]))

    return app


setup_logging()
log.info("starting: loading the book and Kokoro; log at %s", LOG_FILE)
player = Player(Book.load(), Kokoro())
trace = log_steps(Trace())
agent = trace.attach(
    Agent(
        sp=SP,
        tools=tools(player),
        maxtok=1024,
        client=trace.client(raw_client()),
        notices=[lambda: status_line(player)],
    )
)
app = make_app(agent, player, trace)
log.info("ready")

if __name__ == "__main__":
    serve()
