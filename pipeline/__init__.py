"""
Background work that other background work depends on.

The problem this solves: once one job's output is another job's input, "run
these together when that finishes" stops being good enough. Grouping is not
dependency. A group says *these happen at the same time*; a dependency says
*this one cannot start until that one has finished and written something*. The
moment those two diverge, work runs against data that is missing or stale, and
the symptom shows up somewhere else entirely — usually as a retry loop that
looks like slowness.

So nothing here lists what runs after what. Instead:

  * A **node** declares what it `reads` and what it `produces`.
  * The **graph is derived** from those declarations (`contracts.edges()`), so
    it cannot disagree with the code. There is no second list to drift.
  * A node is **runnable** when every input it declared exists and is newer than
    the version it last consumed — not when some other node happens to finish.
  * **One function admits work** (`engine.admit`), and uniqueness is enforced by
    a database index rather than a check-then-insert, so it holds no matter how
    many workers exist.
  * Every read is **recorded**, so "did this run on stale input?" is a query.
  * A run carries a **budget**. Loops that each cap themselves independently
    still compound; one shared ceiling cannot.

Adding a node is: write the handler, declare its contract, register it. Nothing
else changes, and nothing else needs to know it exists.
"""

from . import contracts, engine  # noqa: F401
