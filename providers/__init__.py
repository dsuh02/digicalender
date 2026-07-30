"""Provider registry. Add new adapters here and the API picks them up."""

from __future__ import annotations

from .base import CalendarProvider, SyncResult
from .google import GoogleProvider
from .local import LocalProvider
from .microsoft import MicrosoftProvider

REGISTRY: dict[str, type[CalendarProvider]] = {
    LocalProvider.name: LocalProvider,
    GoogleProvider.name: GoogleProvider,
    MicrosoftProvider.name: MicrosoftProvider,
}


def describe() -> list[dict]:
    """What the UI shows in the Calendars panel."""
    return [
        {"name": cls.name, "label": cls.label, "configured": cls.configured}
        for cls in REGISTRY.values()
    ]


def get(name: str, account: dict | None = None) -> CalendarProvider:
    cls = REGISTRY.get(name)
    if cls is None:
        raise KeyError(f"unknown provider: {name}")
    return cls(account)


__all__ = ["REGISTRY", "describe", "get", "CalendarProvider", "SyncResult"]
