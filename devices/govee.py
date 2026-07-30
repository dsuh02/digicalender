"""
Govee adapters — two of them, because Govee has two disjoint control paths and
which one you need depends on the product.

**govee_lan** — UDP on the local network, no cloud, no API key, no rate limit.
Discovery is a multicast to 239.255.255.250:4001 with replies on 4002; commands
go to <device>:4003. Requires "LAN Control" switched on per-device in the Govee
Home app (Device settings > LAN Control).

    ⚠️ The LAN API only covers a subset of Govee's RGB lighting. **Smart plugs
    (H5080/H5081 and friends) are not on it** — they have no LAN listener at
    all, so no amount of configuration will make this adapter reach one. Plugs
    must go through govee_cloud below. This is a Govee product decision, not a
    limitation here.

**govee_cloud** — the Developer API. Works for plugs and for everything else on
your account, but every command is a round trip to Govee's servers and the key
is rate limited (10k/day, and per-device throttles). Get a key in the Govee Home
app: Profile > About Us > Apply for API Key; it arrives by email.

    GET https://developer-api.govee.com/v1/devices
    PUT https://developer-api.govee.com/v1/devices/control
    GET https://developer-api.govee.com/v1/devices/state?device=&model=
"""

from __future__ import annotations

import json
import socket
import struct
import urllib.error
import urllib.parse
import urllib.request

from .base import (CAP_BRIGHTNESS, CAP_COLOR, CAP_POWER, CAP_TOGGLE,
                   DeviceAdapter, Result)

MULTICAST_GROUP = "239.255.255.250"
SCAN_PORT = 4001
LISTEN_PORT = 4002
CONTROL_PORT = 4003

CLOUD_BASE = "https://developer-api.govee.com/v1"
CLOUD_TIMEOUT = 8.0


# --------------------------------------------------------------- LAN adapter

def _send_lan(ip: str, payload: dict, timeout: float = 1.5) -> bool:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.settimeout(timeout)
            s.sendto(json.dumps(payload).encode(), (ip, CONTROL_PORT))
        return True
    except OSError:
        return False


class GoveeLanAdapter(DeviceAdapter):
    kind = "govee_lan"
    label = "Govee light (LAN)"
    capabilities = (CAP_POWER, CAP_TOGGLE, CAP_BRIGHTNESS, CAP_COLOR)
    config_fields = (
        {"name": "ip", "label": "IP address", "type": "text",
         "help": "Enable LAN Control in the Govee Home app first"},
        {"name": "sku", "label": "Model / SKU", "type": "text",
         "help": "Optional, e.g. H6159"},
    )

    def get_state(self) -> Result:
        """Ask the device for status and wait for the reply on :4002.

        We bind the listen port for the duration of the call rather than
        running a background listener, so nothing holds the port between polls.
        """
        if not self.ip:
            return Result.fail("no IP configured")
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                s.settimeout(2.0)
                s.bind(("", LISTEN_PORT))
                s.sendto(json.dumps({"msg": {"cmd": "devStatus", "data": {}}}).encode(),
                         (self.ip, CONTROL_PORT))
                while True:
                    raw, addr = s.recvfrom(2048)
                    if addr[0] != self.ip:
                        continue          # another Govee device answering
                    data = json.loads(raw.decode()).get("msg", {}).get("data", {})
                    return Result(ok=True, state={
                        "online": True,
                        "on": bool(data.get("onOff")),
                        "brightness": data.get("brightness"),
                        "color": data.get("color"),
                        "color_temp": data.get("colorTemInKelvin"),
                    })
        except socket.timeout:
            return Result(ok=False, state={"online": False},
                          message=f"{self.name} did not answer on the LAN — is LAN Control on?")
        except (OSError, ValueError, json.JSONDecodeError) as e:
            return Result.fail(f"LAN read failed: {e}")

    def command(self, cmd: str, params: dict | None = None) -> Result:
        p = params or {}
        if not self.ip:
            return Result.fail("no IP configured")

        if cmd in ("power_on", "power_off", "turn"):
            val = 1 if cmd == "power_on" else 0 if cmd == "power_off" else int(bool(p.get("on")))
            ok = _send_lan(self.ip, {"msg": {"cmd": "turn", "data": {"value": val}}})
            return Result(ok=ok, state={"on": bool(val)} if ok else {})

        if cmd == "toggle":
            st = self.get_state()
            if not st.ok:
                return st
            return self.command("power_off" if st.state.get("on") else "power_on")

        if cmd == "brightness":
            val = max(1, min(int(p.get("value", 100)), 100))
            ok = _send_lan(self.ip, {"msg": {"cmd": "brightness", "data": {"value": val}}})
            return Result(ok=ok, state={"brightness": val} if ok else {})

        if cmd == "color":
            r = max(0, min(int(p.get("r", 255)), 255))
            g = max(0, min(int(p.get("g", 255)), 255))
            b = max(0, min(int(p.get("b", 255)), 255))
            ok = _send_lan(self.ip, {"msg": {"cmd": "colorwc", "data": {
                "color": {"r": r, "g": g, "b": b}, "colorTemInKelvin": 0}}})
            return Result(ok=ok, state={"color": {"r": r, "g": g, "b": b}} if ok else {})

        if cmd == "color_temp":
            k = max(2000, min(int(p.get("kelvin", 4000)), 9000))
            ok = _send_lan(self.ip, {"msg": {"cmd": "colorwc", "data": {
                "color": {"r": 0, "g": 0, "b": 0}, "colorTemInKelvin": k}}})
            return Result(ok=ok, state={"color_temp": k} if ok else {})

        return Result.fail(f"unknown command: {cmd}")


def lan_discover(timeout: float = 3.0) -> list[dict]:
    """Multicast scan for LAN-capable Govee devices. Lights only — plugs never
    answer, by design."""
    found: dict[str, dict] = {}
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL,
                         struct.pack("b", 2))
            s.settimeout(timeout)
            s.bind(("", LISTEN_PORT))
            s.sendto(
                json.dumps({"msg": {"cmd": "scan",
                                    "data": {"account_topic": "reserve"}}}).encode(),
                (MULTICAST_GROUP, SCAN_PORT))
            while True:
                try:
                    raw, addr = s.recvfrom(2048)
                except socket.timeout:
                    break
                try:
                    d = json.loads(raw.decode()).get("msg", {}).get("data", {})
                except (ValueError, UnicodeDecodeError):
                    continue
                ip = d.get("ip") or addr[0]
                if ip:
                    found[ip] = {
                        "kind": "govee_lan",
                        "ip": ip,
                        "name": d.get("sku") or "Govee light",
                        "sku": d.get("sku", ""),
                        "mac": d.get("device", ""),
                    }
    except OSError:
        pass
    return list(found.values())


# ------------------------------------------------------------- Cloud adapter

class GoveeCloudAdapter(DeviceAdapter):
    kind = "govee_cloud"
    label = "Govee via cloud API (plugs, any device)"
    capabilities = (CAP_POWER, CAP_TOGGLE, CAP_BRIGHTNESS, CAP_COLOR)
    config_fields = (
        {"name": "api_key", "label": "Govee API key", "type": "password",
         "help": "Govee Home app > Profile > About Us > Apply for API Key"},
        {"name": "device", "label": "Device MAC", "type": "text",
         "help": "The colon-separated id from /v1/devices, e.g. AB:CD:...:12"},
        {"name": "model", "label": "Model", "type": "text",
         "help": "e.g. H5080 for a smart plug"},
    )

    def _req(self, path: str, method: str = "GET", body: dict | None = None):
        key = (self.config.get("api_key") or "").strip()
        if not key:
            return None, "no API key configured"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            CLOUD_BASE + path, data=data, method=method,
            headers={"Govee-API-Key": key, "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=CLOUD_TIMEOUT) as r:
                return json.loads(r.read().decode() or "{}"), ""
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = json.loads(e.read().decode() or "{}").get("message", "")
            except Exception:
                pass
            if e.code == 401:
                return None, "Govee rejected the API key"
            if e.code == 429:
                return None, "Govee rate limit reached — try again shortly"
            return None, f"Govee API {e.code}: {detail or e.reason}"
        except (urllib.error.URLError, OSError, ValueError) as e:
            return None, f"Govee API unreachable: {e}"

    def get_state(self) -> Result:
        dev, model = self.config.get("device"), self.config.get("model")
        if not dev or not model:
            return Result.fail("device MAC and model are required")
        body, err = self._req(
            f"/devices/state?device={urllib.parse.quote(dev)}&model={urllib.parse.quote(model)}")
        if body is None:
            return Result(ok=False, state={"online": False}, message=err)
        props = {}
        for entry in (body.get("data", {}) or {}).get("properties", []) or []:
            props.update(entry)
        return Result(ok=True, state={
            "online": str(props.get("online", "true")).lower() != "false",
            "on": props.get("powerState") == "on",
            "brightness": props.get("brightness"),
            "color": props.get("color"),
        })

    def command(self, cmd: str, params: dict | None = None) -> Result:
        p = params or {}
        dev, model = self.config.get("device"), self.config.get("model")
        if not dev or not model:
            return Result.fail("device MAC and model are required")

        def control(name, value):
            body, err = self._req("/devices/control", "PUT",
                                  {"device": dev, "model": model,
                                   "cmd": {"name": name, "value": value}})
            return Result(ok=body is not None, message=err)

        if cmd in ("power_on", "power_off"):
            r = control("turn", "on" if cmd == "power_on" else "off")
            if r.ok:
                r.state = {"on": cmd == "power_on"}
            return r
        if cmd == "toggle":
            st = self.get_state()
            if not st.ok:
                return st
            return self.command("power_off" if st.state.get("on") else "power_on")
        if cmd == "brightness":
            return control("brightness", max(1, min(int(p.get("value", 100)), 100)))
        if cmd == "color":
            return control("color", {"r": int(p.get("r", 255)),
                                     "g": int(p.get("g", 255)),
                                     "b": int(p.get("b", 255))})
        return Result.fail(f"unknown command: {cmd}")

    def cloud_devices(self) -> tuple[list[dict], str]:
        """Everything on the account — used by discovery to offer real choices
        instead of asking you to type a MAC."""
        body, err = self._req("/devices")
        if body is None:
            return [], err
        out = []
        for d in (body.get("data", {}) or {}).get("devices", []) or []:
            out.append({
                "kind": "govee_cloud",
                "name": d.get("deviceName", "Govee device"),
                "device": d.get("device", ""),
                "model": d.get("model", ""),
                "controllable": d.get("controllable", False),
                "supported": d.get("supportCmds", []),
            })
        return out, ""
