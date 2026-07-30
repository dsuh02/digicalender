"""The on-device calendar. Always available; nothing to sync."""

from __future__ import annotations

from .base import CalendarProvider, SyncResult


class LocalProvider(CalendarProvider):
    name = "local"
    label = "This device"
    configured = True

    def pull(self, since: str | None = None) -> SyncResult:
        return SyncResult(self.name, self.account_id, message="Local calendar is always current.")

    def push(self) -> SyncResult:
        return SyncResult(self.name, self.account_id)
