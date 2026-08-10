"""
Readiness, admission, and execution.

Three ideas, each of which exists because the obvious alternative fails
somewhere that is expensive to debug:

**Readiness is a predicate, not a trigger.** A node is runnable when every
input it declared exists and is newer than the version it last consumed. Nobody
tells it to run. The alternative — "when node A finishes, dispatch B, C and D" —
only produces correct ordering if that dispatch list happens to match what B, C
and D actually read, and there is nothing keeping the two in step.

**Admission is one function, and uniqueness lives in the database.** Checking
"is this already queued?" and then inserting has a gap between the two
statements. Under one worker you never notice; under two you get duplicates
that look like the pipeline running twice for no reason. The unique index does
not have that gap.

**Every task records why it exists and what it read.** A task with a blank
reason is a task nobody can explain later, and an unrecorded read makes "did
this run on stale input?" unanswerable without reading source.
"""

from __future__ import annotations

import time
import traceback
from dataclasses import dataclass

import store

from . import contracts

DEFAULT_BUDGET = 50


class BudgetExhausted(Exception):
    pass


# --------------------------------------------------------------- readiness

@dataclass
class Readiness:
    node_id: str
    ready: bool
    reason: str
    missing: tuple[str, ...] = ()
    stale_inputs: tuple[str, ...] = ()   # kinds newer than what we consumed


def evaluate(node_id: str, *, run_id: str | None = None,
             now: float | None = None) -> Readiness:
    """Can this node run right now, and in one sentence, why or why not?"""
    node = contracts.get(node_id)
    if node is None:
        return Readiness(node_id, False, "no such node")

    if node.enabled is not None:
        try:
            if not node.enabled():
                return Readiness(node_id, False, "node is not configured")
        except Exception as e:
            return Readiness(node_id, False, f"enabled check failed: {e}")

    if store.pipeline_live_task(node_id):
        return Readiness(node_id, False, "already queued or running")

    # One attempt per run. Without this a node with nothing to wait for is
    # runnable the instant it finishes, and the tick loop spins on it until it
    # hits max_tasks — which looks like the pipeline working very hard.
    if run_id and store.pipeline_ran_in_run(run_id, node_id):
        return Readiness(node_id, False, "already attempted in this run")

    last = store.pipeline_last_success(node_id)

    if node.min_interval_s and last and last.get("ended_at"):
        age = store.seconds_since(last["ended_at"], now=now)
        if age is not None and age < node.min_interval_s:
            return Readiness(node_id, False,
                             f"ran {int(age)}s ago; min interval {node.min_interval_s}s")

    # A source node — nothing declared as input — is runnable whenever its
    # interval allows. That is the base case that lets the graph start.
    if not node.reads:
        return Readiness(node_id, True, "source node" if not last else "source node, interval elapsed")

    consumed = store.pipeline_input_watermark(node_id, list(node.reads)) if last else {}
    missing, stale = [], []
    for kind in node.reads:
        latest = store.pipeline_latest_artifact(kind)
        if latest is None:
            missing.append(kind)
            continue
        seen = consumed.get(kind)
        if seen is None or latest["version"] > seen:
            stale.append(kind)

    if missing:
        return Readiness(node_id, False,
                         f"waiting on {', '.join(missing)}", tuple(missing), tuple(stale))
    if not stale:
        return Readiness(node_id, False, "inputs unchanged since last run",
                         (), ())
    return Readiness(node_id, True,
                     f"new {', '.join(stale)}", (), tuple(stale))


def runnable(*, run_id: str | None = None, now: float | None = None) -> list[Readiness]:
    return [r for r in (evaluate(n.id, run_id=run_id, now=now)
                        for n in contracts.all_nodes()) if r.ready]


# --------------------------------------------------------------- admission

def admit(run_id: str, node_id: str, reason: str) -> dict | None:
    """THE only way work is created. Returns the task, or None if refused.

    Refusal is normal and is not an error: another worker got there first, or
    the budget is spent. Both are answers, not failures.
    """
    if not reason:
        raise ValueError("a task must record why it was admitted")

    run = store.get_pipeline_run(run_id)
    if run is None:
        return None
    if run["spent"] + _cost(node_id) > run["budget"]:
        store.finish_pipeline_run(run_id, "exhausted")
        return None

    # Uniqueness is the index's job. Racing here returns None instead of a
    # second identical task.
    return store.create_pipeline_task(run_id, node_id, reason)


def _cost(node_id: str) -> int:
    node = contracts.get(node_id)
    return max(1, node.cost if node else 1)


# --------------------------------------------------------------- execution

class Context:
    """Handed to a node. Reading through it is what makes the read recorded.

    Nodes must not query artifact tables directly — a read the engine cannot
    see is exactly the gap that makes staleness undebuggable later.
    """

    def __init__(self, task_id: str, run_id: str, node: contracts.Node):
        self.task_id = task_id
        self.run_id = run_id
        self.node = node
        self._consumed: dict[str, int] = {}

    def read(self, kind: str):
        """Latest payload for `kind`, recorded as consumed. None if absent."""
        if kind not in self.node.reads:
            raise ValueError(
                f"{self.node.id} read '{kind}' without declaring it — "
                "the declaration is what the graph is built from")
        art = store.pipeline_latest_artifact(kind)
        version = art["version"] if art else None
        store.record_pipeline_read(self.task_id, kind, version)
        if version is not None:
            self._consumed[kind] = version
        return art["payload"] if art else None

    def produce(self, kind: str, payload: dict) -> dict:
        if kind not in self.node.produces:
            raise ValueError(
                f"{self.node.id} produced '{kind}' without declaring it")
        return store.create_pipeline_artifact(kind, self.node.id, self.run_id, payload)


def execute(task: dict) -> dict:
    """Run one admitted task. Never raises — a failure is a recorded outcome."""
    node = contracts.get(task["node_id"])
    if node is None or node.handler is None:
        store.finish_pipeline_task(task["id"], "failed", "node has no handler")
        return {"ok": False, "error": "node has no handler"}

    store.start_pipeline_task(task["id"])
    ctx = Context(task["id"], task["run_id"], node)
    t0 = time.time()
    try:
        result = node.handler(ctx) or {}
        # Declared inputs the handler never actually read are still recorded,
        # as version NULL. Otherwise a node that quietly ignores an input looks
        # identical to one that had nothing to read.
        for kind in node.reads:
            if kind not in ctx._consumed:
                store.record_pipeline_read(task["id"], kind, None)
        store.finish_pipeline_task(task["id"], "done", "")
        store.spend_pipeline_budget(task["run_id"], _cost(node.id))
        return {"ok": True, "ms": int((time.time() - t0) * 1000), **result}
    except Exception as e:
        store.finish_pipeline_task(task["id"], "failed", f"{e}\n{traceback.format_exc()[-800:]}")
        store.spend_pipeline_budget(task["run_id"], _cost(node.id))
        return {"ok": False, "error": str(e), "ms": int((time.time() - t0) * 1000)}


def tick(run_id: str, *, max_tasks: int = 25) -> dict:
    """Admit and run whatever is ready, repeatedly, until nothing is.

    Loops because finishing one node can make another ready — that is the graph
    making progress, and it is driven entirely by readiness rather than by
    anyone declaring a sequence.
    """
    ran, refused = [], []
    for _ in range(max_tasks):
        ready = runnable(run_id=run_id)
        if not ready:
            break
        progressed = False
        for r in ready:
            task = admit(run_id, r.node_id, r.reason)
            if task is None:
                refused.append(r.node_id)
                continue
            out = execute(task)
            ran.append({"node": r.node_id, "reason": r.reason, **out})
            progressed = True
        if not progressed:
            break
    run = store.get_pipeline_run(run_id)
    if run and run["status"] == "running":
        store.finish_pipeline_run(run_id, "done")
    return {"run_id": run_id, "ran": ran, "refused": refused,
            "spent": (run or {}).get("spent", 0), "budget": (run or {}).get("budget", 0)}


def start_run(reason: str, budget: int = DEFAULT_BUDGET) -> dict:
    if not reason:
        raise ValueError("a run must record why it started")
    return store.create_pipeline_run(reason, budget)
