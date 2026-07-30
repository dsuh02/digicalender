"""
Device adapter registry.

Add an adapter class here and the API, the "add device" form and the widget
settings pickers all learn about it — nothing else needs editing.
"""

from __future__ import annotations

from .base import DeviceAdapter, Result
from .govee import GoveeCloudAdapter, GoveeLanAdapter
from .roku import RokuAdapter
from .samsung import SamsungTvAdapter

REGISTRY: dict[str, type[DeviceAdapter]] = {
    RokuAdapter.kind: RokuAdapter,
    GoveeLanAdapter.kind: GoveeLanAdapter,
    GoveeCloudAdapter.kind: GoveeCloudAdapter,
    SamsungTvAdapter.kind: SamsungTvAdapter,
}


def describe() -> list[dict]:
    """Adapter catalogue for the UI: what exists, what it can do, what it needs."""
    return [
        {
            "kind": cls.kind,
            "label": cls.label,
            "capabilities": list(cls.capabilities),
            "config_fields": list(cls.config_fields),
        }
        for cls in REGISTRY.values()
    ]


def adapter_for(device: dict) -> DeviceAdapter | None:
    cls = REGISTRY.get(device.get("kind", ""))
    return cls(device) if cls else None


__all__ = ["REGISTRY", "describe", "adapter_for", "DeviceAdapter", "Result"]
