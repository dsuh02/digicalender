"""
Samsung adapter — Tizen TVs (2016+) and soundbars that speak the same protocol.

Three channels, used for different things:

  http://IP:8001/api/v2/     unauthenticated JSON device info. Best liveness
                             check there is: if this answers, the TV is awake.
  wss://IP:8002/...          the remote control channel. Needs a pairing token.
  Wake-on-LAN                the only way to power ON a fully-off TV — its
                             network stack is down, so no API can reach it.

**Pairing:** the first WebSocket connect makes the TV show an on-screen
"Allow this device?" prompt. Accept it with the physical remote and the TV
replies with a token, which is stored on the device row and reused forever
after. Until that happens every command fails with a pairing message — that's
expected, not a bug.

**Soundbars:** Samsung soundbars are inconsistent. Newer ones answer the same
Tizen endpoints and work here; many older ones only expose UPnP or are designed
to be driven over HDMI-CEC by the TV, in which case the honest answer is to
control volume through the TV device instead. This adapter probes and reports
what it finds rather than pretending.
"""

from __future__ import annotations

import base64
import json
import urllib.error
import urllib.request

from .base import (CAP_DPAD, CAP_INPUT, CAP_POWER, CAP_TRANSPORT, CAP_VOLUME,
                   DeviceAdapter, Result)
from .wsclient import WebSocket, WSError

INFO_PORT = 8001
WS_PORT = 8002
CLIENT_NAME = "DigiCalender"

KEYS = {
    "KEY_POWER", "KEY_POWEROFF", "KEY_POWERON",
    "KEY_VOLUP", "KEY_VOLDOWN", "KEY_MUTE",
    "KEY_UP", "KEY_DOWN", "KEY_LEFT", "KEY_RIGHT", "KEY_ENTER", "KEY_RETURN",
    "KEY_HOME", "KEY_MENU", "KEY_SOURCE", "KEY_GUIDE", "KEY_INFO", "KEY_EXIT",
    "KEY_PLAY", "KEY_PAUSE", "KEY_STOP", "KEY_REWIND", "KEY_FF",
    "KEY_CHUP", "KEY_CHDOWN",
    "KEY_HDMI", "KEY_HDMI1", "KEY_HDMI2", "KEY_HDMI3", "KEY_HDMI4", "KEY_TV",
    "KEY_0", "KEY_1", "KEY_2", "KEY_3", "KEY_4",
    "KEY_5", "KEY_6", "KEY_7", "KEY_8", "KEY_9",
}


class SamsungTvAdapter(DeviceAdapter):
    kind = "samsung_tv"
    label = "Samsung TV / soundbar (Tizen)"
    capabilities = (CAP_POWER, CAP_VOLUME, CAP_DPAD, CAP_TRANSPORT, CAP_INPUT)
    config_fields = (
        {"name": "ip", "label": "IP address", "type": "text",
         "help": "Settings > General > Network > Network Status on the TV"},
        {"name": "mac", "label": "MAC address", "type": "text",
         "help": "Needed for Wake-on-LAN; without it the TV can't be turned on"},
        {"name": "token", "label": "Pairing token", "type": "text",
         "help": "Filled in automatically after you accept the prompt on the TV"},
    )

    # --------------------------------------------------------------- info

    def _info(self) -> dict | None:
        if not self.ip:
            return None
        try:
            with urllib.request.urlopen(
                    f"http://{self.ip}:{INFO_PORT}/api/v2/", timeout=3.0) as r:
                return json.loads(r.read().decode() or "{}")
        except (urllib.error.URLError, OSError, ValueError):
            return None

    def get_state(self) -> Result:
        if not self.ip:
            return Result.fail("no IP configured")
        info = self._info()
        if info is None:
            # A TV that's off stops answering entirely — that IS the state.
            return Result(ok=True, state={"online": False, "on": False},
                          message="asleep or unreachable")
        dev = info.get("device", {}) or {}
        return Result(ok=True, state={
            "online": True,
            "on": True,
            "name": dev.get("name") or info.get("name", ""),
            "model": dev.get("modelName", ""),
            "type": dev.get("type", ""),
            "paired": bool(self.config.get("token")),
            "wifi_mac": dev.get("wifiMac", ""),
        })

    # ------------------------------------------------------------- remote

    def _ws_url(self) -> str:
        name = base64.b64encode(CLIENT_NAME.encode()).decode()
        url = (f"wss://{self.ip}:{WS_PORT}/api/v2/channels/"
               f"samsung.remote.control?name={name}")
        token = (self.config.get("token") or "").strip()
        if token:
            url += f"&token={token}"
        return url

    def _send_keys(self, keys: list[str]) -> Result:
        """Open a channel, send the keys, close. Returns a new token when the
        TV issues one so the caller can persist it."""
        if not self.ip:
            return Result.fail("no IP configured")
        bad = [k for k in keys if k not in KEYS]
        if bad:
            return Result.fail(f"unsupported key(s): {', '.join(bad)}")
        try:
            with WebSocket(self._ws_url(), timeout=8.0) as ws:
                first = ws.recv_json()
                if not first:
                    return Result.fail("TV closed the connection without replying")
                event = first.get("event", "")
                if event == "ms.channel.unauthorized":
                    return Result.fail(
                        "TV refused the connection — accept the prompt on screen, "
                        "or clear the device from the TV's Device Manager and retry")
                new_token = ((first.get("data") or {}).get("token") or "")
                for k in keys:
                    ws.send_json({
                        "method": "ms.remote.control",
                        "params": {"Cmd": "Click", "DataOfCmd": k,
                                   "Option": "false", "TypeOfRemote": "SendRemoteKey"},
                    })
                out = Result(ok=True)
                if new_token and new_token != (self.config.get("token") or ""):
                    out.data = {"token": new_token}
                    out.message = "paired"
                return out
        except WSError as e:
            return Result.fail(f"remote channel failed: {e}")
        except OSError:
            return Result(ok=False, state={"online": False},
                          message=f"{self.name} is not reachable — it may be off")

    # ------------------------------------------------------------ commands

    def command(self, cmd: str, params: dict | None = None) -> Result:
        p = params or {}

        if cmd == "power_on":
            # WoL only; the TV's API is gone when it's off.
            mac = (self.config.get("mac") or "").strip()
            if not mac:
                return Result.fail("a MAC address is required to power the TV on")
            ok = self.wake_on_lan(mac)
            return Result(ok=ok, message="" if ok else "wake-on-LAN packet failed")

        if cmd == "power_off":
            return self._send_keys(["KEY_POWER"])

        if cmd == "toggle":
            st = self.get_state()
            return self.command("power_off" if st.state.get("on") else "power_on")

        if cmd == "key":
            return self._send_keys([str(p.get("key", ""))])

        if cmd in ("volume_up", "volume_down", "mute"):
            key = {"volume_up": "KEY_VOLUP", "volume_down": "KEY_VOLDOWN",
                   "mute": "KEY_MUTE"}[cmd]
            n = max(1, min(int(p.get("steps", 1)), 20))
            return self._send_keys([key] * n)

        if cmd in ("play", "pause", "play_pause"):
            return self._send_keys(["KEY_PLAY" if cmd != "pause" else "KEY_PAUSE"])

        if cmd == "input":
            src = str(p.get("source", "KEY_HDMI"))
            return self._send_keys([src])

        return Result.fail(f"unknown command: {cmd}")
