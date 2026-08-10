"""
Node declarations, and the graph derived from them.

A node says what it needs and what it makes. That is the only place either fact
is written down — the edge list below is computed from those declarations every
time it is asked for, never stored and never hand-maintained. A hand-written
"A then B" list is a second source of truth for the same graph, and the two
always end up disagreeing; when they do, the code keeps reading whatever the
declaration said while the scheduler keeps firing whatever the list said.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable


@dataclass(frozen=True)
class Node:
    """One unit of work.

    reads / produces are **artifact kinds** — the contract names other nodes
    also use. They are the entire interface; nothing else couples two nodes.
    """

    id: str
    produces: tuple[str, ...] = ()
    reads: tuple[str, ...] = ()
    handler: Callable | None = None
    # What running this costs against the run's budget. Nodes that call an
    # external service cost more than nodes that only touch the database, so a
    # runaway loop hits the ceiling on spend rather than on iteration count.
    cost: int = 1
    # Minimum seconds between runs. A guard against a node whose inputs churn
    # faster than it is useful to recompute — NOT a scheduler.
    min_interval_s: int = 0
    description: str = ""
    # Optional gate: return False and the node is skipped without being an
    # error. Used for "no accounts configured yet", which is not a failure.
    enabled: Callable[[], bool] | None = None


_REGISTRY: dict[str, Node] = {}


def register(node: Node) -> Node:
    if node.id in _REGISTRY:
        raise ValueError(f"duplicate node id: {node.id}")
    _REGISTRY[node.id] = node
    return node


def all_nodes() -> list[Node]:
    return list(_REGISTRY.values())


def get(node_id: str) -> Node | None:
    return _REGISTRY.get(node_id)


def producers_of(kind: str) -> list[Node]:
    return [n for n in _REGISTRY.values() if kind in n.produces]


def consumers_of(kind: str) -> list[Node]:
    return [n for n in _REGISTRY.values() if kind in n.reads]


def edges() -> list[tuple[str, str, str]]:
    """The dependency graph, derived. (producer_id, consumer_id, kind).

    This is the ONLY edge list. It is computed, so it cannot drift from what
    the nodes actually declare.
    """
    out = []
    for kind in known_kinds():
        for p in producers_of(kind):
            for c in consumers_of(kind):
                if p.id != c.id:                 # self-reads are not edges
                    out.append((p.id, c.id, kind))
    return sorted(out)


def known_kinds() -> list[str]:
    kinds: set[str] = set()
    for n in _REGISTRY.values():
        kinds.update(n.produces)
        kinds.update(n.reads)
    return sorted(kinds)


def orphan_reads() -> list[tuple[str, str]]:
    """(node_id, kind) a node reads that nothing produces.

    Not necessarily a bug — an input may legitimately arrive from outside the
    pipeline — but it IS the shape of the mistake where a contract name was
    typo'd, so it is worth being able to see.
    """
    return sorted(
        (n.id, k) for n in _REGISTRY.values()
        for k in n.reads if not producers_of(k)
    )


def cycles() -> list[list[str]]:
    """Any dependency cycle, so a graph that cannot make progress is visible
    before it is scheduled rather than after it hangs."""
    graph: dict[str, set[str]] = {n.id: set() for n in _REGISTRY.values()}
    for producer, consumer, _kind in edges():
        graph[producer].add(consumer)

    found: list[list[str]] = []
    WHITE, GREY, BLACK = 0, 1, 2
    colour = {nid: WHITE for nid in graph}
    stack: list[str] = []

    def walk(nid: str) -> None:
        colour[nid] = GREY
        stack.append(nid)
        for nxt in sorted(graph[nid]):
            if colour[nxt] == GREY:
                found.append(stack[stack.index(nxt):] + [nxt])
            elif colour[nxt] == WHITE:
                walk(nxt)
        stack.pop()
        colour[nid] = BLACK

    for nid in sorted(graph):
        if colour[nid] == WHITE:
            walk(nid)
    return found


def describe() -> dict:
    """Everything the graph knows about itself — for the API and for tests."""
    return {
        "nodes": [{
            "id": n.id, "reads": list(n.reads), "produces": list(n.produces),
            "cost": n.cost, "min_interval_s": n.min_interval_s,
            "description": n.description,
        } for n in sorted(_REGISTRY.values(), key=lambda x: x.id)],
        "edges": [{"from": a, "to": b, "kind": k} for a, b, k in edges()],
        "kinds": known_kinds(),
        "orphan_reads": [{"node": n, "kind": k} for n, k in orphan_reads()],
        "cycles": cycles(),
    }
