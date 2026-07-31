"""
Network discovery — find devices instead of making you type IP addresses.

Probes, run concurrently:
  SSDP         M-SEARCH for `roku:ecp`, then GET each responder's device-info.
  Govee LAN    multicast scan (lights with LAN Control on — plugs never answer).
  Govee cloud  account listing when a shared API key is saved — the only way
               plugs are controllable, and it returns device+model ready to add.
  Samsung      probe :8001/api/v2/ across a subnet sweep, since Samsung's SSDP
               advertisement is unreliable and often absent when the TV is idle.
  Govee ARP    after the sweep has populated the neighbour table, any MAC with
               Govee's OUI (98:17:3C) is reported as present — so cloud-only
               devices are still *found*, with a pointer at what they need.

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

from .govee import GoveeCloudAdapter, lan_discover

SSDP_ADDR = ("239.255.255.250", 1900)
GOVEE_OUI = "98:17:3c"


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


def _sweep_arp(timeout: float = 4.0) -> None:
    """Touch every host on the /24 so the kernel's neighbour table fills up.
    A connect to the discard port is enough — we want the ARP exchange, not an
    answer."""
    net = _local_subnet()
    if net is None:
        return

    def poke(host: str):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(0.3)
            s.connect_ex((host, 9))
            s.close()
        except OSError:
            pass

    with ThreadPoolExecutor(max_workers=64) as pool:
        list(pool.map(poke, [str(h) for h in net.hosts()], timeout=timeout))


def _neighbour_macs() -> list[tuple[str, str]]:
    """(ip, mac) pairs from /proc/net/arp — Linux only, which is where this runs."""
    out = []
    try:
        with open("/proc/net/arp") as fh:
            next(fh, None)
            for line in fh:
                parts = line.split()
                if len(parts) >= 4 and parts[3] != "00:00:00:00:00:00":
                    out.append((parts[0], parts[3].lower()))
    except OSError:
        pass
    return out


def discover_govee_cloud() -> list[dict]:
    """Everything on the Govee account, when a shared API key is saved. This is
    the path that makes plugs one-tap: the cloud returns device id AND model."""
    import store
    key = (store.get_setting("govee_api_key") or "").strip()
    if not key:
        return []
    adapter = GoveeCloudAdapter({"config": {"api_key": key}})
    devices, err = adapter.cloud_devices()
    if err:
        return [{"kind": "govee_cloud", "name": f"Govee cloud error: {err}",
                 "error": err}]
    return devices


def discover_govee_presence(exclude_macs: set[str]) -> list[dict]:
    """Govee hardware seen on the LAN by OUI. Run AFTER the subnet sweep so the
    neighbour table is warm. These entries are informational when the device is
    cloud-only — but 'found' is the honest word: it is here, on this network."""
    found = []
    for ip, mac in _neighbour_macs():
        if mac.startswith(GOVEE_OUI) and mac.upper() not in exclude_macs:
            found.append({
                "kind": "govee_cloud",
                "name": f"Govee device {mac.upper()[-8:]}",
                "ip": ip,
                "device": mac.upper(),
                "needs_key": True,
            })
    return found


def discover_all(include_samsung: bool = True) -> dict:
    """Every probe at once. Slowest single probe sets the wall-clock, not the sum."""
    results: dict[str, list] = {"roku": [], "govee_lan": [], "govee_cloud": [],
                                "samsung_tv": []}
    jobs = {"roku": discover_roku, "govee_lan": lan_discover,
            "govee_cloud": discover_govee_cloud}
    if include_samsung:
        jobs["samsung_tv"] = discover_samsung
    else:
        jobs["_arp"] = _sweep_arp          # samsung's sweep normally does this
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {k: pool.submit(fn) for k, fn in jobs.items()}
        for k, fut in futures.items():
            try:
                r = fut.result(timeout=25)
                if k in results:
                    results[k] = r
            except Exception:
                pass

    # Cloud entries carry the model and win; ARP fills in whatever the cloud
    # doesn't know about (no key yet, or a device on someone else's account).
    known = {str(d.get("device", "")).upper() for d in results["govee_cloud"]}
    results["govee_cloud"] += discover_govee_presence(known)
    return results
