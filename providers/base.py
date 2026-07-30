"""
Provider interface.

DigiCalender stores every event locally; providers are sync adapters that pull
remote events into that store and push local changes back out. The local
calendar is itself a provider so the rest of the app never special-cases it.

To add Google / Microsoft later, implement pull() and push() below and register
the class in providers/__init__.py:REGISTRY. Nothing else in the app changes.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class SyncResult:
    provider: str
    account_id: str | None
    pulled: int = 0
    pushed: int = 0
    ok: bool = True
    message: str = ""

    def as_dict(self) -> dict:
        return {
            "provider": self.provider,
            "account_id": self.account_id,
            "pulled": self.pulled,
            "pushed": self.pushed,
            "ok": self.ok,
            "message": self.message,
        }


class CalendarProvider:
    """Base sync adapter. Subclasses implement pull/push."""

    #: short id, e.g. "google" — matches events.provider
    name: str = "base"
    #: human label for the UI
    label: str = "Base"
    #: False until credentials are wired up; the UI greys these out
    configured: bool = False

    def __init__(self, account: dict | None = None):
        self.account = account or {}

    @property
    def account_id(self) -> str | None:
        return self.account.get("id")

    def pull(self, since: str | None = None) -> SyncResult:
        """Fetch remote events into the local store."""
        raise NotImplementedError

    def push(self) -> SyncResult:
        """Send locally-changed (dirty) events to the remote."""
        raise NotImplementedError

    def sync(self, since: str | None = None) -> SyncResult:
        pulled = self.pull(since)
        if not pulled.ok:
            return pulled
        pushed = self.push()
        return SyncResult(
            provider=self.name,
            account_id=self.account_id,
            pulled=pulled.pulled,
            pushed=pushed.pushed,
            ok=pushed.ok,
            message=pushed.message or pulled.message,
        )


class NotConfiguredProvider(CalendarProvider):
    """Stub used until OAuth credentials exist. Reports cleanly instead of
    throwing, so /api/sync stays useful while only the local calendar works."""

    configured = False

    def pull(self, since: str | None = None) -> SyncResult:
        return SyncResult(
            provider=self.name,
            account_id=self.account_id,
            ok=False,
            message=f"{self.label} is not connected yet — no credentials configured.",
        )

    def push(self) -> SyncResult:
        return self.pull()
