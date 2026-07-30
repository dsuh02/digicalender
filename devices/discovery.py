"""
Network discovery — find devices instead of making you type IP addresses.

Three probes, run concurrently:
  SSDP        M-SEARCH for `roku:ecp`, then GET each responder's device-info.
  Govee LAN   multicast scan (lights only — plugs have no LAN listener).
  Samsung     probe :8001/api/v2/ across a subnet sweep, since Samsung's SSDP
              advertisement is unreliable and often absent when the TV is idle.

Everything is best-effort and time-boxed. Discovery on a wall panel must never
hang the UI, so each probe has its own deadline and failures return empty.
"""

from __future__ import annotations

import ipaddress
import json
import socket
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor

from .govee import lan_discover

SSDP_ADDR = ("239.255.255.250", 1900)


def _local_subnet() -> ipaddress.IPv4Network | None:
    """The /24 this host sits on, used to bound the Samsung sweep."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
        return ipaddress.ip_network(f"{ip}/24", strict=False)
    except OSError:
        return None


def ssdp_search(st: str = "roku:ecp", timeout: float = 3.0) -> list[str]:
    """Returns LOCATION urls of responders."""
    msg = (
        "M-SEARCH * HTTP/1.1\r\n"
        f"HOST: {SSDP_ADDR[0]}:{SSDP_ADDR[1]}\r\n"
        'MAN: "ssdp:discover"\r\n'
        "MX: 2\r\n"
        f"ST: {st}\r\n\r\n"
    ).encode()
    locations: set[str] = set()
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.settimeout(timeout)
            s.sendto(msg, SSDP_ADDR)
            while True:
                try:
                    raw, _ = s.recvfrom(2048)
                except socket.timeout:
                    break
                for line in raw.decode(errors="replace").split("\r\n"):
                    if line.lower().startswith("location:"):
                        locations.add(line.split(":", 1)[1].strip())
    except OSError:
        pass
    return sorted(locations)


def discover_roku(timeout: float = 3.0) -> list[dict]:
    out = []
    for loc in ssdp_search("roku:ecp", timeout):
        host = loc.split("//", 1)[-1].split("/", 1)[0].split(":")[0]
        name, model, is_tv = "Roku", "", False
        try:
            with urllib.request.urlopen(
                    f"http://{host}:8060/query/device-info", timeout=2.0) as r:
                root = ET.fromstring(r.read())

            def t(tag):
                el = root.find(tag)
                return (el.text or "") if el is not None else ""

            name = t("user-device-name") or t("friendly-device-name") or "Roku"
            model = t("model-name") or t("model-number")
            is_tv = t("is-tv") == "true"
        except (urllib.error.URLError, OSError, ET.ParseError):
            pass
        out.append({"kind": "roku", "name": name, "ip": host,
                    "model": model, "is_tv": is_tv})
    return out


def _probe_samsung(host: str) -> dict | None:
    try:
        with urllib.request.urlopen(f"http://{host}:8001/api/v2/", timeout=1.2) as r:
            info = json.loads(r.read().decode() or "{}")
    except (urllib.error.URLError, OSError, ValueError):
        return None
    dev = info.get("device", {}) or {}
    if not dev:
        return None
    return {
        "kind": "samsung_tv",
        "name": dev.get("name") or info.get("name") or "Samsung device",
        "ip": host,
        "model": dev.get("modelName", ""),
        "mac": dev.get("wifiMac", ""),
        "type": dev.get("type", ""),
    }


def discover_samsung(timeout: float = 6.0) -> list[dict]:
    """Sweep the local /24 for the Tizen info endpoint.

    A sweep rather than SSDP because Samsung's advertisements are unreliable —
    an idle TV often never announces itself, but it will answer :8001.
    """
    net = _local_subnet()
    if net is None:
        return []
    hosts = [str(h) for h in net.hosts()]
    found = []
    with ThreadPoolExecutor(max_workers=64) as pool:
        for res in pool.map(_probe_samsung, hosts, timeout=timeout):
            if res:
                found.append(res)
    return found


def discover_all(include_samsung: bool = True) -> dict:
    """Every probe at once. Slowest single probe sets the wall-clock, not the sum."""
    results: dict[str, list] = {"roku": [], "govee_lan": [], "samsung_tv": []}
    jobs = {"roku": discover_roku, "govee_lan": lan_discover}
    if include_samsung:
        jobs["samsung_tv"] = discover_samsung
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = {k: pool.submit(fn) for k, fn in jobs.items()}
        for k, fut in futures.items():
            try:
                results[k] = fut.result(timeout=20)
            except Exception:
                results[k] = []
    return results
