"""
The registered nodes.

Each one declares what it reads and what it produces, and that declaration is
the only thing that couples it to anything else. Importing this module is what
puts a node into the graph; nothing else needs editing.
"""

from __future__ import annotations

import mail
import store

from .contracts import Node, register


def _has_mail_accounts() -> bool:
    return any(a.get("enabled") for a in store.list_mail_accounts())


def _sync_mail(ctx) -> dict:
    """Pull envelopes, then publish a summary as this node's output.

    The summary is the artifact — not the raw rows. Downstream work should
    depend on a stable, small shape, so that changing how mail is fetched does
    not change what anything else sees.
    """
    res = mail.sync_all()
    summary = mail.summary()
    ctx.produce("mail.summary", summary)
    ok = [r for r in res["results"] if r.get("ok")]
    return {
        "accounts": len(res["results"]),
        "ok": len(ok),
        "stored": sum(r.get("stored", 0) for r in ok),
        "unread": summary["unread"],
    }


register(Node(
    id="mail_sync",
    produces=("mail.summary",),
    reads=(),
    handler=_sync_mail,
    cost=2,                       # talks to an external service
    min_interval_s=300,           # IMAP politeness, and nothing here is urgent
    enabled=_has_mail_accounts,
    description="Fetch mail envelopes over IMAP and publish a summary.",
))
