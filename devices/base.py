"""
Device adapter contract.

A device row in the database is {id, name, kind, room, config}. `kind` selects
the adapter class; `config` carries whatever that adapter needs (ip, mac, model,
token, api key). Adapters are stateless — constructed per request from the row —
so a config edit takes effect immediately with no reload.

Adapters must never raise for an unreachable device. A wall panel that throws a
500 because a TV is unplugged is worse than one that greys the tile out, so
every method returns a result object with ok=False instead.
"""

from __future__ import annotations

import socket
from dataclasses import dataclass, field

# Capability strings the UI keys off to decide which controls to draw.
CAP_POWER = "power"          # on/off
CAP_TOGGLE = "toggle"
CAP_BRIGHTNESS = "brightness"
CAP_COLOR = "color"
CAP_VOLUME = "volume"
CAP_TRANSPORT = "transport"  # play/pause/skip
CAP_DPAD = "dpad"            # directional remote
CAP_APPS = "apps"            # launchable channels/apps
CAP_INPUT = "input"          # HDMI/source switching


@dataclass
class Result:
    ok: bool = True
    state: dict = field(default_factory=dict)
    message: str = ""
    data: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {"ok": self.ok, "state": self.state,
                "message": self.message, "data": self.data}

    @classmethod
    def fail(cls, message: str) -> "Result":
        return cls(ok=False, message=message)


class DeviceAdapter:
    kind = "base"
    label = "Device"
    #: capabilities this adapter can expose; a given device may report fewer
    capabilities: tuple[str, ...] = ()
    #: fields the "add device" form should collect, as {name, label, type, help}
    config_fields: tuple[dict, ...] = ()

    def __init__(self, device: dict):
        self.device = device or {}
        self.config = self.device.get("config") or {}

    @property
    def name(self) -> str:
        return self.device.get("name", "device")

    @property
    def ip(self) -> str:
        return (self.config.get("ip") or "").strip()

    def get_state(self) -> Result:
        """Read current state. Cheap and safe to poll."""
        return Result.fail("not implemented")

    def command(self, cmd: str, params: dict | None = None) -> Result:
        """Run a command. Unknown commands should fail, not raise."""
        return Result.fail(f"unknown command: {cmd}")

    # ------------------------------------------------------------ utilities

    @staticmethod
    def wake_on_lan(mac: str, broadcast: str = "255.255.255.255") -> bool:
        """Magic packet. The only way to power on a TV that's fully off — its
        network stack is down, so no API call can reach it."""
        mac = mac.replace(":", "").replace("-", "").replace(".", "").strip()
        if len(mac) != 12:
            return False
        try:
            payload = bytes.fromhex("FF" * 6 + mac * 16)
        except ValueError:
            return False
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
                s.sendto(payload, (broadcast, 9))
            return True
        except OSError:
            return False

    @staticmethod
    def reachable(host: str, port: int, timeout: float = 1.0) -> bool:
        try:
            with socket.create_connection((host, port), timeout=timeout):
                return True
        except OSError:
            return False
