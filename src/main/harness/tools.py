from dataclasses import dataclass
from typing import Callable

KILLED = "killed"

UNCHANGED = (
    "(nothing has moved since you last looked; you will be told when it does, "
    "so there is nothing to gain by looking again now)"
)


@dataclass
class Tool:
    """A tool the agent can call.

    A background tool's `fn` takes the model's arguments and returns a Task for the
    harness to run on its own thread; the call outlives the turn that made it. A
    synchronous tool's `fn` takes the agent and returns a string at once, because it
    acts on tasks rather than becoming one.
    """

    schema: dict
    fn: Callable
    synchronous: bool = False

    @property
    def name(self) -> str:
        return self.schema["name"]


TAIL = dict(
    name="tail",
    description=(
        "Check on a running task. Pass the id shown as [task #N], and how many of its "
        "most recent output lines you want to see."
    ),
    input_schema=dict(
        type="object",
        properties=dict(
            task=dict(type="integer", description="Task id to inspect"),
            lines=dict(
                type="integer",
                description="How many recent lines to show, newest last. Defaults to 5.",
            ),
        ),
        required=["task"],
    ),
)


def _tail(agent, task: int, lines: int = 5) -> str:
    r = agent.tasks.get(task)
    if r is None:
        return f"no such task #{task}"
    if r.killed:
        return "that task was killed before it returned"
    # the outcome, not a brush-off: being told to call again reads as "the result is lost"
    if r.future.done():
        return f"that task has finished, and returned: {r.future.result()}"
    out = r.tail(lines)
    if out == agent.seen.get(task):
        return f"{out}\n{UNCHANGED}"
    agent.seen[task] = out
    return out


tail = Tool(TAIL, _tail, synchronous=True)


KILL = dict(
    name="kill",
    description="Stop a running task that is stuck or cannot make progress. Pass the task id.",
    input_schema=dict(
        type="object",
        properties=dict(task=dict(type="integer", description="Task id to stop")),
        required=["task"],
    ),
)


def _kill(agent, task: int) -> str:
    r = agent.tasks.get(task)
    if r is None:
        return f"no such task #{task}"
    if r.done:
        return "that task is already over, so there was nothing to stop"
    agent.kill(task)
    return agent.note_kill(task)


kill = Tool(KILL, _kill, synchronous=True)
