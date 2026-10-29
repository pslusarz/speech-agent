"""Cut a Bedrock stream off from another thread, and show where each thread is.

    LLM_CACHE=off uv run python scripts/cut_stream.py flag    # the reader leaves at its next event
    LLM_CACHE=off uv run python scripts/cut_stream.py close   # close() from here: the reader hangs

Found on 2026-10-28: close() from another thread returns at once but leaves the reading
thread blocked for good, so the harness sets a flag and the reader leaves the stream
itself, at the next event (about a quarter of a second, often less).

Prints a timeline of what each thread did, and dumps every thread's stack if anything
takes longer than it should, so a hang shows where it is stuck.
"""

import faulthandler
import sys
import threading
import time

from main.bedrock import MODEL, raw_client

STALL = 15
t0 = time.monotonic()


def say(*a):
    print(f"{time.monotonic() - t0:6.2f}s [{threading.current_thread().name}]", *a, flush=True)


faulthandler.dump_traceback_later(STALL, repeat=True, file=sys.stderr)
cli = raw_client()
ready, cut, chars = threading.Event(), threading.Event(), 0
holder = {}
MODE = sys.argv[1] if len(sys.argv) > 1 else "flag"  # flag: break in the reader; close: close from here


def read():
    global chars
    try:
        say("opening stream")
        with cli.messages.stream(
            model=MODEL,
            max_tokens=2000,
            messages=[dict(role="user", content="Tell me, in about 400 words, the story of Zarathustra's prologue.")],
        ) as stream:
            holder["stream"] = stream
            say("stream open")
            events = 0
            for event in stream:
                events += 1
                if cut.is_set():
                    say(f"saw the cut at event {events} ({event.type}); leaving the stream")
                    break
                if event.type == "content_block_delta" and event.delta.type == "text_delta":
                    chars += len(event.delta.text)
                    if chars > 80:
                        ready.set()
            else:
                say("stream ended normally")
        say("left the with block (stream closed in this thread)")
    except Exception as e:
        say("reader got", type(e).__name__, str(e)[:200])
    say("reader done")


r = threading.Thread(target=read, name="reader", daemon=True)
r.start()
ready.wait(60)
say(f"{chars} chars read; cutting ({MODE})")
cut.set()
if MODE == "close":
    holder["stream"].close()
    say("close() returned")
r.join(10)
say("reader alive after join:", r.is_alive())
faulthandler.cancel_dump_traceback_later()
