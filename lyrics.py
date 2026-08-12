"""
Time-synced lyrics, from LRCLIB.

Spotify has no lyrics endpoint — its in-app lyrics are licensed from Musixmatch
and exposed through nothing public. Musixmatch's own API puts synced lyrics
behind a commercial plan, and Genius serves metadata but not words. LRCLIB is a
free, keyless, community-maintained database of LRC files, which is the one
option that fits a project with no budget and no third-party packages.

It is a volunteer service, so this is a polite client: it identifies itself,
caches every answer including the misses, and never retries in a loop. A miss
is cached precisely because most misses are permanent — an obscure track is not
going to appear because it was asked for four times.

Matching is by artist, title, album and DURATION. Duration is what stops a live
version or a remaster picking up the studio timings and drifting further out of
sync with every line.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request

import store

API = "https://lrclib.net/api/get"
SEARCH = "https://lrclib.net/api/search"
# LRCLIB asks clients to identify themselves and link back.
UA = "DigiCalender/1.0 (https://github.com/dsuh02/digicalender)"
TIMEOUT = 10.0
CACHE_DAYS = 90

_LRC_LINE = re.compile(r"\[(\d{1,3}):(\d{2})(?:[.:](\d{1,3}))?\]")


def parse_lrc(text: str) -> list[dict]:
    """LRC to [{ms, line}], sorted.

    One timestamp can carry several stamps for a repeated line, and blank lines
    are meaningful — they are the instrumental gaps a follower should sit in
    rather than skip over.
    """
    out: list[dict] = []
    for raw in (text or "").splitlines():
        stamps = list(_LRC_LINE.finditer(raw))
        if not stamps:
            continue
        words = _LRC_LINE.sub("", raw).strip()
        for m in stamps:
            mins, secs, frac = m.group(1), m.group(2), m.group(3) or "0"
            # The fraction is a DECIMAL fraction of a second, so right-pad it to
            # milliseconds: ".9" is nine tenths (900ms), ".06" is sixty, ".500"
            # is five hundred. Treating every fraction as centiseconds puts a
            # one-digit stamp out by 810ms — enough to highlight the wrong line.
            frac_ms = int(frac.ljust(3, "0")[:3])
            out.append({"ms": int(mins) * 60000 + int(secs) * 1000 + frac_ms,
                        "line": words})
    out.sort(key=lambda x: x["ms"])
    return out


def _key(artist: str, track: str, duration_s: int) -> str:
    return f"{(artist or '').lower()}|{(track or '').lower()}|{duration_s}"


def _fetch(url: str, params: dict) -> dict | list | None:
    req = urllib.request.Request(url + "?" + urllib.parse.urlencode(params),
                                 headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return json.loads(r.read().decode() or "null")
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None                 # a normal answer: nobody has this one
        raise
    except (urllib.error.URLError, OSError, ValueError):
        return None


def lookup(artist: str, track: str, album: str = "", duration_ms: int = 0) -> dict:
    """Lyrics for one track. Always returns a dict; `found` says whether it hit."""
    duration_s = round((duration_ms or 0) / 1000)
    key = _key(artist, track, duration_s)

    cached = store.get_lyrics(key, CACHE_DAYS)
    if cached is not None:
        return {**cached, "cached": True}

    data = _fetch(API, {
        "artist_name": artist or "", "track_name": track or "",
        "album_name": album or "", "duration": duration_s,
    })

    # The exact-match endpoint is strict about duration. Fall back to search,
    # then accept only a candidate within a couple of seconds — a looser match
    # gives lyrics that drift apart from the song, which is worse than none.
    if not data:
        results = _fetch(SEARCH, {"artist_name": artist or "", "track_name": track or ""})
        if isinstance(results, list):
            for cand in results:
                if abs((cand.get("duration") or 0) - duration_s) <= 2:
                    data = cand
                    break

    if not data:
        out = {"found": False, "synced": [], "plain": "", "instrumental": False}
    else:
        out = {
            "found": True,
            "instrumental": bool(data.get("instrumental")),
            "synced": parse_lrc(data.get("syncedLyrics") or ""),
            "plain": (data.get("plainLyrics") or "")[:20000],
            "title": data.get("trackName") or track,
            "artist": data.get("artistName") or artist,
        }

    # Misses are cached too: most are permanent, and re-asking a volunteer
    # service every eight seconds for a track it does not have is rude.
    store.put_lyrics(key, out)
    return {**out, "cached": False}


def active_line(synced: list[dict], position_ms: int) -> int:
    """Index of the line that should be highlighted, or -1 before the first.

    Binary search: this is called on every animation tick, and a linear scan
    over a few hundred lines every 250ms is work for nothing.
    """
    lo, hi, found = 0, len(synced) - 1, -1
    while lo <= hi:
        mid = (lo + hi) // 2
        if synced[mid]["ms"] <= position_ms:
            found = mid
            lo = mid + 1
        else:
            hi = mid - 1
    return found
