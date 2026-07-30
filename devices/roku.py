"""
Roku adapter — External Control Protocol (ECP).

ECP is plain HTTP on port 8060 with no authentication, which makes Roku by far
the most cooperative device on the network. Works identically on the streaming
stick, the Ultra, and a Roku TV; the TV additionally honours volume, power and
input keys, because on a stick those belong to whatever it's plugged into.

  GET  /query/device-info      device name, model, power state
  GET  /query/apps             installed channels
  GET  /query/active-app       what's on screen now
  GET  /query/media-player     playback state
  POST /keypress/<key>         remote button
  POST /launch/<appId>         open a channel
  GET  /query/icon/<appId>     channel artwork (proxied by the server)

Reference: developer.roku.com/docs/developer-program/dev-tools/external-control-api.md
"""

from __future__ import annotations

import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

from .base import (CAP_APPS, CAP_DPAD, CAP_INPUT, CAP_POWER, CAP_TRANSPORT,
                   CAP_VOLUME, DeviceAdapter, Result)

PORT = 8060
TIMEOUT = 3.0

# Buttons ECP accepts. Anything not on this list is rejected before we send it,
# so a typo in a scene can't put the device into a weird state.
KEYS = {
    "Home", "Rev", "Fwd", "Play", "Select", "Left", "Right", "Down", "Up",
    "Back", "InstantReplay", "Info", "Backspace", "Search", "Enter",
    "VolumeDown", "VolumeMute", "VolumeUp", "PowerOff", "PowerOn", "Power",
    "ChannelUp", "ChannelDown", "InputTuner", "InputAV1",
    "InputHDMI1", "InputHDMI2", "InputHDMI3", "InputHDMI4",
    "FindRemote",
}


class RokuAdapter(DeviceAdapter):
    kind = "roku"
    label = "Roku (stick / box / TV)"
    capabilities = (CAP_POWER, CAP_DPAD, CAP_TRANSPORT, CAP_APPS,
                    CAP_VOLUME, CAP_INPUT)
    config_fields = (
        {"name": "ip", "label": "IP address", "type": "text",
         "help": "Settings > Network > About on the Roku"},
        {"name": "is_tv", "label": "This is a Roku TV", "type": "toggle",
         "help": "Enables volume, power and HDMI input keys"},
    )

    def _url(self, path: str) -> str:
        return f"http://{self.ip}:{PORT}{path}"

    def _request(self, path: str, method: str = "GET") -> tuple[bytes | None, str]:
        """Returns (body, error). Never raises.

        The error string matters more than usual here: a Roku with "Control by
        mobile apps" set to Limited answers /query/device-info happily but
        returns 403 for everything else, so the device looks online while every
        button silently does nothing. Translate that into the actual fix rather
        than letting it read as a network failure.
        """
        req = urllib.request.Request(
            self._url(path), data=b"" if method == "POST" else None, method=method)
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                return r.read(), ""
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode(errors="replace").strip()
            except Exception:
                pass
            # ECP returns 403 only for access control, and keypress rejections
            # come back with an empty body — so don't rely on matching the text.
            if e.code == 403:
                return None, (
                    f"{self.name} is in Limited mode. On the Roku: Settings > System > "
                    "Advanced system settings > Control by mobile apps > Network access "
                    "> set to Default or Permissive.")
            return None, f"Roku returned {e.code}{': ' + body if body else ''}"
        except (urllib.error.URLError, OSError):
            return None, f"{self.name} is not responding on {self.ip}:{PORT}"

    def _get(self, path: str) -> bytes | None:
        return self._request(path)[0]

    def _post(self, path: str) -> tuple[bool, str]:
        body, err = self._request(path, "POST")
        return err == "", err

    # ------------------------------------------------------------- reading

    def get_state(self) -> Result:
        if not self.ip:
            return Result.fail("no IP configured")
        raw, err = self._request("/query/device-info")
        if raw is None:
            return Result(ok=False, state={"online": False}, message=err)
        try:
            root = ET.fromstring(raw)
        except ET.ParseError:
            return Result.fail("device-info was not valid XML")

        def t(tag: str, default: str = "") -> str:
            el = root.find(tag)
            return (el.text or default) if el is not None else default

        # "PowerOn" means awake; sticks report "Ready"/"Headless" when asleep.
        power = t("power-mode", "Unknown")
        state = {
            "online": True,
            "power": power,
            "on": power == "PowerOn",
            "model": t("model-name") or t("model-number"),
            "device_name": t("user-device-name") or t("friendly-device-name"),
            "serial": t("serial-number"),
            "is_tv": t("is-tv") == "true" or bool(self.config.get("is_tv")),
            "supports_volume": t("supports-ecs-microphone") == "true" or t("is-tv") == "true",
        }

        # Probe one restricted endpoint so the UI can badge Limited mode instead
        # of leaving you to wonder why every button is inert.
        _, apps_err = self._request("/query/apps")
        if apps_err and "Limited mode" in apps_err:
            state["limited"] = True
            state["limited_hint"] = apps_err

        app = self._get("/query/active-app")
        if app:
            try:
                a = ET.fromstring(app)
                cur = a.find("app")
                if cur is not None:
                    state["active_app"] = (cur.text or "").strip()
                    state["active_app_id"] = cur.get("id", "")
                scr = a.find("screensaver")
                if scr is not None:
                    state["screensaver"] = True
            except ET.ParseError:
                pass

        media = self._get("/query/media-player")
        if media:
            try:
                m = ET.fromstring(media)
                st = m.get("state", "")
                state["playback"] = st
                state["playing"] = st == "play"
                pos, dur = m.find("position"), m.find("duration")
                if pos is not None and pos.text:
                    state["position"] = pos.text
                if dur is not None and dur.text:
                    state["duration"] = dur.text
            except ET.ParseError:
                pass

        return Result(ok=True, state=state)

    def list_apps(self) -> tuple[list[dict], str]:
        """Returns (apps, error). An empty list with no error means the device
        genuinely has no channels; an empty list *with* one usually means
        Limited mode."""
        raw, err = self._request("/query/apps")
        if err:
            return [], err
        try:
            root = ET.fromstring(raw)
        except ET.ParseError:
            return [], "app list was not valid XML"
        return [{"id": a.get("id", ""), "name": (a.text or "").strip(),
                 "type": a.get("type", "")}
                for a in root.findall("app")], ""

    def app_icon(self, app_id: str) -> tuple[bytes, str] | None:
        """Returned through the server so the kiosk page stays same-origin."""
        try:
            with urllib.request.urlopen(self._url(f"/query/icon/{app_id}"),
                                        timeout=TIMEOUT) as r:
                return r.read(), r.headers.get("Content-Type", "image/jpeg")
        except (urllib.error.URLError, OSError):
            return None

    # ------------------------------------------------------------ commands

    def command(self, cmd: str, params: dict | None = None) -> Result:
        p = params or {}
        if not self.ip:
            return Result.fail("no IP configured")

        def press(path: str) -> Result:
            ok, err = self._post(path)
            return Result(ok=ok, message=err)

        if cmd == "key":
            key = str(p.get("key", ""))
            if key not in KEYS:
                return Result.fail(f"unsupported key: {key}")
            return press(f"/keypress/{key}")

        if cmd == "launch":
            app_id = str(p.get("app_id", "")).strip()
            if not app_id.isalnum():
                return Result.fail("app_id must be alphanumeric")
            return press(f"/launch/{app_id}")

        if cmd == "type":
            # ECP types one character per keypress as Lit_<url-encoded char>.
            for ch in str(p.get("text", ""))[:120]:
                ok, err = self._post("/keypress/Lit_" + urllib.parse.quote(ch))
                if not ok:
                    return Result.fail(err or "typing failed midway")
            return Result(ok=True)

        if cmd == "search":
            kw = urllib.parse.quote(str(p.get("keyword", ""))[:100])
            return press(f"/search/browse?keyword={kw}")

        if cmd in ("power_on", "power_off", "power_toggle"):
            return press("/keypress/" + {"power_on": "PowerOn", "power_off": "PowerOff",
                                         "power_toggle": "Power"}[cmd])

        if cmd in ("volume_up", "volume_down", "mute"):
            key = {"volume_up": "VolumeUp", "volume_down": "VolumeDown",
                   "mute": "VolumeMute"}[cmd]
            for _ in range(max(1, min(int(p.get("steps", 1)), 20))):
                ok, err = self._post(f"/keypress/{key}")
                if not ok:
                    return Result.fail(err)
            return Result(ok=True)

        if cmd in ("play", "pause", "play_pause"):
            return press("/keypress/Play")

        if cmd == "input":
            src = str(p.get("source", "")).strip()
            if src not in KEYS:
                return Result.fail(f"unsupported input: {src}")
            return press(f"/keypress/{src}")

        if cmd == "toggle":
            st = self.get_state()
            if not st.ok:
                return st
            return self.command("power_off" if st.state.get("on") else "power_on")

        return Result.fail(f"unknown command: {cmd}")
