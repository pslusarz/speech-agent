import threading
from collections import deque
from concurrent.futures import Future
from typing import Protocol, runtime_checkable

GRACE = 1.0
IDLE = "no progress reported yet"


class Stop(Exception):
    """Raised inside a task at its next beat once wind-down has been requested."""


@runtime_checkable
class Task(Protocol):
    """What a tool must provide. The harness owns the thread; the task owns the work."""

    def run(self) -> str:
        """Do the work and return the value. Raising is a result too."""

    def tail(self, lines: int) -> str:
        """Say what is going on, at most `lines` of recent output."""

    def stop(self) -> None:
        """Wind down. Called from another thread; must not block."""


class Work:
    """A Task base with the usual tail and stop: a status line, a bounded log, a flag.

    Subclasses implement run() and call beat() wherever they can afford to pause,
    which is both where progress is reported and where a stop request lands.
    """

    def __init__(self, keep: int = 50):
        self.status = ""
        self._log: deque[str] = deque(maxlen=keep)
        self._stopping = threading.Event()

    def log(self, line: str):
        self._log.append(line)

    def tail(self, lines: int) -> str:
        recent = list(self._log)[-lines:] if lines > 0 else []
        return "\n".join(filter(None, [self.status, *recent])) or IDLE

    def stop(self):
        self._stopping.set()

    @property
    def stopping(self) -> bool:
        return self._stopping.is_set()

    def beat(self, seconds: float = 0):
        if self._stopping.wait(seconds):
            raise Stop("asked to stop")


class Runner:
    """Owns the thread one task runs on, and the value it settles with.

    A killed task stays killed: whatever it returns while winding down is
    dropped rather than posted, so a decision to kill is not undone by a
    late result.
    """

    def __init__(self, id: int, task: Task, on_done):
        self.id = id
        self.task = task
        self.future: Future = Future()
        self.killed = False
        self._lock = threading.Lock()
        self._thread = threading.Thread(target=self._work, daemon=True)
        self.future.add_done_callback(lambda _: on_done(self))

    def start(self):
        # a kill can land in the gap between construction and start
        if self.killed:
            return
        self.future.set_running_or_notify_cancel()
        self._thread.start()

    @property
    def done(self) -> bool:
        return self.killed or self.future.done()

    def tail(self, lines: int) -> str:
        return self.task.tail(lines)

    def kill(self, grace: float = GRACE):
        with self._lock:
            if self.killed:
                return
            self.killed = True
        self.task.stop()
        # a thread cannot be interrupted, so this is a courtesy, not a guarantee
        if self._thread.is_alive():
            self._thread.join(grace)

    def _work(self):
        try:
            self._settle(self.task.run())
        except Stop as e:
            self._settle(f"stopped before finishing: {e}")
        except Exception as e:
            self._settle(f"failed: {e!r}")

    def _settle(self, value: str):
        # a kill racing a task that is finishing must not set the future twice
        with self._lock:
            if not self.future.done():
                self.future.set_result(value)
