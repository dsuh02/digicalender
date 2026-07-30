"""
Weather via Open-Meteo — no API key, no account, free for non-commercial use.

Chosen over OpenWeatherMap/WeatherAPI precisely because it needs no credential:
one less secret to provision on a wall panel, and one less thing to expire
silently and leave a blank tile.

Responses are cached in-process; the panel polls far more often than the
forecast changes, and hammering a free service from a device that runs 24/7 is
how you get blocked.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

ENDPOINT = "https://api.open-meteo.com/v1/forecast"
CACHE_TTL = 600  # 10 minutes

_cache: dict[str, tuple[float, dict]] = {}
_lock = threading.Lock()

# WMO code -> (label, icon key the frontend maps to an SVG)
WMO = {
    0: ("Clear", "sun"), 1: ("Mainly clear", "sun"), 2: ("Partly cloudy", "cloud-sun"),
    3: ("Overcast", "cloud"), 45: ("Fog", "fog"), 48: ("Rime fog", "fog"),
    51: ("Light drizzle", "drizzle"), 53: ("Drizzle", "drizzle"),
    55: ("Heavy drizzle", "drizzle"), 56: ("Freezing drizzle", "sleet"),
    57: ("Freezing drizzle", "sleet"), 61: ("Light rain", "rain"),
    63: ("Rain", "rain"), 65: ("Heavy rain", "rain"),
    66: ("Freezing rain", "sleet"), 67: ("Freezing rain", "sleet"),
    71: ("Light snow", "snow"), 73: ("Snow", "snow"), 75: ("Heavy snow", "snow"),
    77: ("Snow grains", "snow"), 80: ("Showers", "rain"), 81: ("Showers", "rain"),
    82: ("Violent showers", "rain"), 85: ("Snow showers", "snow"),
    86: ("Snow showers", "snow"), 95: ("Thunderstorm", "storm"),
    96: ("Thunderstorm, hail", "storm"), 99: ("Thunderstorm, hail", "storm"),
}


def describe(code) -> dict:
    label, icon = WMO.get(int(code or 0), ("Unknown", "cloud"))
    return {"code": code, "label": label, "icon": icon}


def fetch(lat: float, lon: float, units: str = "imperial",
          days: int = 5) -> tuple[dict | None, str]:
    key = f"{lat:.3f},{lon:.3f},{units},{days}"
    now = time.time()
    with _lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < CACHE_TTL:
            return hit[1], ""

    imperial = units != "metric"
    params = {
        "latitude": f"{lat:.4f}",
        "longitude": f"{lon:.4f}",
        "current": ("temperature_2m,relative_humidity_2m,apparent_temperature,"
                    "is_day,weather_code,wind_speed_10m"),
        "daily": ("weather_code,temperature_2m_max,temperature_2m_min,"
                  "precipitation_probability_max,sunrise,sunset"),
        "hourly": "temperature_2m,precipitation_probability,weather_code",
        "forecast_days": str(max(1, min(days, 7))),
        "timezone": "auto",
    }
    if imperial:
        params.update({"temperature_unit": "fahrenheit",
                       "wind_speed_unit": "mph", "precipitation_unit": "inch"})

    url = ENDPOINT + "?" + urllib.parse.urlencode(params)
    try:
        with urllib.request.urlopen(url, timeout=8.0) as r:
            raw = json.loads(r.read().decode())
    except (urllib.error.URLError, OSError, ValueError) as e:
        # Serve a stale cache rather than a blank tile if we have one.
        with _lock:
            hit = _cache.get(key)
        if hit:
            return hit[1], "showing cached data — weather service unreachable"
        return None, f"weather unavailable: {e}"

    cur = raw.get("current", {}) or {}
    daily = raw.get("daily", {}) or {}
    hourly = raw.get("hourly", {}) or {}

    out = {
        "units": "imperial" if imperial else "metric",
        "temp_unit": "F" if imperial else "C",
        "current": {
            "temp": cur.get("temperature_2m"),
            "feels_like": cur.get("apparent_temperature"),
            "humidity": cur.get("relative_humidity_2m"),
            "wind": cur.get("wind_speed_10m"),
            "is_day": bool(cur.get("is_day", 1)),
            **describe(cur.get("weather_code")),
        },
        "daily": [],
        "hourly": [],
    }
    for i, day in enumerate(daily.get("time", []) or []):
        out["daily"].append({
            "date": day,
            "high": (daily.get("temperature_2m_max") or [None])[i],
            "low": (daily.get("temperature_2m_min") or [None])[i],
            "precip": (daily.get("precipitation_probability_max") or [None])[i],
            "sunrise": (daily.get("sunrise") or [None])[i],
            "sunset": (daily.get("sunset") or [None])[i],
            **describe((daily.get("weather_code") or [0])[i]),
        })
    times = hourly.get("time", []) or []
    for i in range(min(len(times), 24)):
        out["hourly"].append({
            "time": times[i],
            "temp": (hourly.get("temperature_2m") or [None])[i],
            "precip": (hourly.get("precipitation_probability") or [None])[i],
            **describe((hourly.get("weather_code") or [0])[i]),
        })

    with _lock:
        _cache[key] = (now, out)
    return out, ""
