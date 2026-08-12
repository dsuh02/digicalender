"""
Spotify Web API client.

Playback control needs a USER token, not an app token. Client credentials get
you an app-only token that can read catalogue data and nothing else — it cannot
see or command a player. So this uses the authorization-code flow and keeps a
refresh token, which is the only thing that survives long enough to be useful to
an alarm that runs every morning for years.

**The redirect dance is deliberately manual.** Spotify requires redirect URIs to
be HTTPS, with an exception only for loopback addresses, so
`http://10.0.0.90:8080/...` cannot be registered. Rather than stand up TLS on a
wall panel, the user authorises on whatever device has a keyboard, lands on a
127.0.0.1 URL that fails to load, and pastes that failed URL back in. The code
is in its query string; nothing else about the redirect matters. Same shape as
the Plaid hosted link: do the typing where there is a keyboard.

Two limits worth knowing before building on this, both verified rather than
assumed:

**Playback control requires Spotify Premium.** A free account gets 403 on every
/me/player call.

**Spotify's own algorithmic playlists cannot be played through the API** — DJ,
Daily Mix, Discover Weekly and the rest. The DJ has no API at all, and
Spotify-owned playlists return 404 to third-party apps. Point alarms at your own
playlists, albums, artists or tracks.
"""

from __future__ import annotations

import base64
import json
import time
import urllib.error
import urllib.parse
import urllib.request

import store

AUTH_URL = "https://accounts.spotify.com/authorize"
TOKEN_URL = "https://accounts.spotify.com/api/token"
API = "https://api.spotify.com/v1"

# Loopback: the one non-HTTPS form Spotify still accepts. It never has to
# resolve — only the code in the query string is used.
REDIRECT_URI = "http://127.0.0.1:8080/api/spotify/callback"

SCOPES = [
    "user-read-playback-state",
    "user-modify-playback-state",
    "user-read-currently-playing",
    "playlist-read-private",
    "playlist-read-collaborative",
    # Ordering playlists by what you actually listen to needs the history, and
    # the plan name (free vs premium) needs the profile. Both are read-only and
    # both are worth the single re-authorisation they cost together.
    "user-read-recently-played",
    "user-read-private",
    # Listening history and library. All read-only, and all bought by the same
    # single re-authorisation, so there is no reason to stage them.
    "user-top-read",
    "user-library-read",
    "user-follow-read",
]

# Spotify's own scannable image service. Not the Web API and not documented as
# a public endpoint, but it is what the share sheet uses and it needs no auth.
# There is NO Jam equivalent: Jam has no Web API at all — Spotify has said it
# belongs in the playback SDKs — so a running Jam cannot be detected, started,
# or linked to from here. A code for what is playing is the closest real thing.
SCANNABLE = "https://scannables.scdn.co/uri/plain/svg/121619/white/640/{uri}"

TIMEOUT = 12.0


class SpotifyError(Exception):
    def __init__(self, message: str, status: int = 0, code: str = ""):
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code


def configured() -> bool:
    return bool((store.get_setting("spotify_client_id") or "").strip()
                and (store.get_setting("spotify_client_secret") or "").strip())


def connected() -> bool:
    return bool((store.get_setting("spotify_refresh_token") or "").strip())


def _creds() -> tuple[str, str]:
    cid = (store.get_setting("spotify_client_id") or "").strip()
    sec = (store.get_setting("spotify_client_secret") or "").strip()
    if not cid or not sec:
        raise SpotifyError("Spotify is not configured — add the client ID and secret", 400)
    return cid, sec


def _basic() -> str:
    cid, sec = _creds()
    return base64.b64encode(f"{cid}:{sec}".encode()).decode()


def _post_form(url: str, form: dict, headers: dict) -> dict:
    body = urllib.parse.urlencode(form).encode()
    req = urllib.request.Request(url, data=body, headers={
        "Content-Type": "application/x-www-form-urlencoded", **headers})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            d = json.loads(e.read().decode() or "{}")
            detail = d.get("error_description") or d.get("error") or ""
            if isinstance(detail, dict):
                detail = detail.get("message", "")
        except Exception:
            pass
        raise SpotifyError(detail or f"Spotify returned {e.code}", e.code)
    except (urllib.error.URLError, OSError) as e:
        raise SpotifyError(f"Could not reach Spotify: {e}", 0)


# ------------------------------------------------------------------- oauth

def authorize_url(state: str = "digicalender") -> str:
    cid, _ = _creds()
    q = urllib.parse.urlencode({
        "client_id": cid,
        "response_type": "code",
        "redirect_uri": REDIRECT_URI,
        "scope": " ".join(SCOPES),
        "state": state,
        # Force the consent screen: without it a re-auth can silently return no
        # refresh token, and the alarm dies the next time the access token ages
        # out — days later, at 6am, with no clue why.
        "show_dialog": "true",
    })
    return f"{AUTH_URL}?{q}"


def code_from_redirect(pasted: str) -> str:
    """Pull ?code= out of whatever the user pasted back.

    Accepts the whole failed redirect URL or a bare code, because 'copy the
    address bar' is the instruction that actually works on a phone.
    """
    s = (pasted or "").strip()
    if not s:
        raise SpotifyError("Paste the address you were redirected to", 400)
    if "code=" in s:
        qs = urllib.parse.urlparse(s).query or s.split("?", 1)[-1]
        got = urllib.parse.parse_qs(qs).get("code")
        if got:
            return got[0]
    if "error=" in s:
        qs = urllib.parse.urlparse(s).query or s.split("?", 1)[-1]
        err = urllib.parse.parse_qs(qs).get("error", ["denied"])[0]
        raise SpotifyError(f"Spotify refused the authorisation: {err}", 400)
    if " " in s or "/" in s:
        raise SpotifyError("That does not look like the redirect address", 400)
    return s


def exchange_code(code: str) -> dict:
    d = _post_form(TOKEN_URL, {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": REDIRECT_URI,
    }, {"Authorization": f"Basic {_basic()}"})
    refresh = d.get("refresh_token")
    if not refresh:
        raise SpotifyError("Spotify did not return a refresh token — re-authorise "
                           "and make sure you approve the consent screen", 400)
    store.set_setting("spotify_refresh_token", refresh)
    _store_access(d)
    return d


def _store_access(d: dict) -> str:
    tok = d.get("access_token") or ""
    # 60s of slack: a token that expires mid-request is the same as expired.
    exp = time.time() + max(0, int(d.get("expires_in") or 3600)) - 60
    store.set_setting("spotify_access_token", tok)
    store.set_setting("spotify_expires_at", str(int(exp)))
    return tok


def token() -> str:
    """A valid access token, refreshing if the stored one has aged out."""
    tok = (store.get_setting("spotify_access_token") or "").strip()
    try:
        exp = float(store.get_setting("spotify_expires_at") or 0)
    except ValueError:
        exp = 0
    if tok and time.time() < exp:
        return tok
    refresh = (store.get_setting("spotify_refresh_token") or "").strip()
    if not refresh:
        raise SpotifyError("Spotify is not connected — authorise it first", 401)
    d = _post_form(TOKEN_URL, {
        "grant_type": "refresh_token", "refresh_token": refresh,
    }, {"Authorization": f"Basic {_basic()}"})
    # Spotify may rotate the refresh token; keeping the old one would work until
    # it silently stopped.
    if d.get("refresh_token"):
        store.set_setting("spotify_refresh_token", d["refresh_token"])
    return _store_access(d)


def disconnect() -> None:
    for k in ("spotify_refresh_token", "spotify_access_token", "spotify_expires_at"):
        store.set_setting(k, "")


# --------------------------------------------------------------------- api

def call(method: str, path: str, body: dict | None = None, params: dict | None = None):
    url = API + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": f"Bearer {token()}",
        "Content-Type": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            raw = r.read().decode()
            # 204 for most player commands — success with nothing to say.
            return json.loads(raw) if raw.strip() else {}
    except urllib.error.HTTPError as e:
        detail, code = "", ""
        try:
            d = json.loads(e.read().decode() or "{}")
            err = d.get("error") or {}
            detail = err.get("message") if isinstance(err, dict) else str(err)
            code = (err.get("reason") or "") if isinstance(err, dict) else ""
        except Exception:
            pass
        # These hints are only true of the PLAYER endpoints. Applied blanket
        # they actively mislead: a 403 from /audio-features (deprecated for apps
        # created after Nov 2024) was being reported as "needs Premium", which
        # sends you chasing a subscription problem that does not exist, and a
        # 404 from /recommendations as "no active device".
        is_player = path.startswith("/me/player")
        if e.code == 403 and not detail:
            detail = ("Spotify refused the command — playback control needs Premium"
                      if is_player else
                      "Spotify refused this endpoint for this app — several catalogue "
                      "endpoints (audio-features, audio-analysis, recommendations, "
                      "browse) are unavailable to apps created after November 2024")
        if e.code == 404 and not detail:
            detail = ("No active Spotify device" if is_player else
                      "Not found, or not available to third-party apps")
        raise SpotifyError(detail or f"Spotify returned {e.code}", e.code, code)
    except (urllib.error.URLError, OSError) as e:
        raise SpotifyError(f"Could not reach Spotify: {e}", 0)


def me() -> dict:
    return call("GET", "/me")


def devices() -> list[dict]:
    return (call("GET", "/me/player/devices") or {}).get("devices") or []


def find_device(name_contains: str) -> dict | None:
    """Match a Connect device by name, case-insensitively.

    Names are what the user sees and what the Roku reports, and device ids
    change between sessions — so the name is the only stable handle an alarm can
    be configured against.
    """
    want = (name_contains or "").strip().lower()
    if not want:
        return None
    for d in devices():
        if want in str(d.get("name", "")).lower():
            return d
    return None


def transfer(device_id: str, play: bool = False) -> None:
    call("PUT", "/me/player", {"device_ids": [device_id], "play": play})


def start(device_id: str | None, uri: str = "", shuffle: bool = False) -> None:
    """Start playback of a URI. Track URIs go in `uris`, everything else in
    `context_uri` — sending a track as a context is a silent no-op."""
    params = {"device_id": device_id} if device_id else None
    if shuffle:
        try:
            call("PUT", "/me/player/shuffle", None, {"state": "true",
                                                     **({"device_id": device_id} if device_id else {})})
        except SpotifyError:
            pass                      # shuffle is a nicety, not the point
    body: dict = {}
    u = (uri or "").strip()
    if u:
        if ":track:" in u:
            body["uris"] = [u]
        else:
            body["context_uri"] = u
    call("PUT", "/me/player/play", body, params)


def set_volume(percent: int, device_id: str | None = None) -> None:
    p = max(0, min(100, int(percent)))
    params = {"volume_percent": p}
    if device_id:
        params["device_id"] = device_id
    call("PUT", "/me/player/volume", None, params)


def _playlist_page(offset: int, attempts: int = 3) -> dict:
    """One page, retried when Spotify reports a total but returns nothing.

    Observed live: /me/playlists intermittently answers 200 with `total: 113`
    and `items: []`. Retrying fixes it. This is deliberately NOT a blanket
    retry — it fires only on that specific self-contradictory response, so a
    genuinely empty library still returns immediately instead of stalling.
    """
    page = {}
    for i in range(attempts):
        page = call("GET", "/me/playlists", None, {"limit": 50, "offset": offset})
        items = page.get("items") or []
        if items or not page.get("total"):
            return page
        time.sleep(0.6 * (i + 1))
    return page


def playlists(max_items: int = 400) -> list[dict]:
    """Every playlist in the library, in Spotify's own order."""
    out, offset = [], 0
    while offset < max_items:
        page = _playlist_page(offset)
        items = [p for p in (page.get("items") or []) if p]
        for p in items:
            owner = p.get("owner") or {}
            out.append({
                "uri": p.get("uri", ""),
                "id": p.get("id", ""),
                "name": p.get("name") or "(untitled)",
                "owner": owner.get("display_name") or owner.get("id") or "",
                "owner_id": owner.get("id") or "",
                "tracks": (p.get("tracks") or {}).get("total"),
                "image": ((p.get("images") or [{}])[0] or {}).get("url", ""),
                "collaborative": bool(p.get("collaborative")),
            })
        if len(items) < 50:
            break
        offset += 50
    return out


def recent_context_uris(limit: int = 50) -> list[str]:
    """Playlist/album URIs most recently played, newest first, deduped.

    Spotify has no "recently played playlists" endpoint. It has recently played
    TRACKS, each carrying the context it was played from — so recency is derived
    from that. Needs `user-read-recently-played`; a token issued before that
    scope was requested returns 403, which the caller treats as "no ordering
    available" rather than an error.
    """
    res = call("GET", "/me/player/recently-played", None, {"limit": min(50, limit)})
    seen, order = set(), []
    for item in res.get("items") or []:
        ctx = (item or {}).get("context") or {}
        uri = ctx.get("uri") or ""
        if uri and uri not in seen:
            seen.add(uri)
            order.append(uri)
    return order


def library(max_items: int = 400) -> dict:
    """Playlists ordered by what has actually been played most recently.

    Degrades honestly: without the history scope the list is still returned, in
    Spotify's order, with `recency` false so the UI can say why.
    """
    items = playlists(max_items)
    try:
        recent = recent_context_uris()
        have_recency = True
    except SpotifyError:
        recent, have_recency = [], False

    rank = {uri: i for i, uri in enumerate(recent)}
    # Played ones first in recency order; everything else keeps Spotify's order
    # behind them, rather than being sorted arbitrarily.
    items.sort(key=lambda p: (rank.get(p["uri"], len(rank)),))
    for p in items:
        p["recent_rank"] = rank.get(p["uri"])
    return {"playlists": items, "recency": have_recency, "count": len(items)}


def code_url(uri: str) -> str:
    return SCANNABLE.format(uri=urllib.parse.quote(uri, safe="")) if uri else ""


def now_playing() -> dict:
    """A stable shape for the player, whatever Spotify is doing.

    Returns `playing: False` rather than raising when nothing is active — an
    idle player is a normal state, not an error, and a widget should not have to
    tell those apart.
    """
    r = call("GET", "/me/player") or {}
    item = r.get("item") or {}
    if not item:
        return {"playing": False, "track": None, "device": None}
    album = item.get("album") or {}
    images = album.get("images") or []
    ctx = r.get("context") or {}
    dev = r.get("device") or {}
    return {
        "playing": bool(r.get("is_playing")),
        "progress_ms": r.get("progress_ms") or 0,
        "shuffle": bool(r.get("shuffle_state")),
        "repeat": r.get("repeat_state") or "off",
        "track": {
            "name": item.get("name") or "",
            "uri": item.get("uri") or "",
            "artists": [a.get("name", "") for a in item.get("artists") or []],
            "album": album.get("name") or "",
            "release_date": album.get("release_date") or "",
            "duration_ms": item.get("duration_ms") or 0,
            "explicit": bool(item.get("explicit")),
            # Largest first; a wall panel wants the 640 and a list wants the 64.
            "art": [{"url": i.get("url"), "w": i.get("width")} for i in images],
        },
        "device": {
            "name": dev.get("name") or "",
            "type": dev.get("type") or "",
            "volume": dev.get("volume_percent"),
            "supports_volume": bool(dev.get("supports_volume")),
        },
        "context": {"type": ctx.get("type") or "", "uri": ctx.get("uri") or ""},
        # Scan with a phone to open whatever is playing. The closest thing
        # available to "share this" — Jam itself has no API.
        "code_url": code_url(ctx.get("uri") or item.get("uri") or ""),
    }


def queue() -> list[dict]:
    """What is coming up. Spotify returns the current track first; dropped."""
    r = call("GET", "/me/player/queue") or {}
    out = []
    for t in (r.get("queue") or [])[:20]:
        if not t:
            continue
        album = t.get("album") or {}
        images = album.get("images") or []
        out.append({
            "name": t.get("name") or "",
            "uri": t.get("uri") or "",
            "artists": [a.get("name", "") for a in t.get("artists") or []],
            "album": album.get("name") or "",
            "art": (images[-1] or {}).get("url", "") if images else "",
            "duration_ms": t.get("duration_ms") or 0,
        })
    return out


TIME_RANGES = {"short_term": "last 4 weeks", "medium_term": "last 6 months",
               "long_term": "all time"}


def top(kind: str = "artists", time_range: str = "medium_term", limit: int = 20) -> dict:
    """Top artists or tracks. Needs user-top-read, which an older token lacks."""
    kind = "tracks" if kind == "tracks" else "artists"
    tr = time_range if time_range in TIME_RANGES else "medium_term"
    r = call("GET", f"/me/top/{kind}", None, {"time_range": tr, "limit": min(50, limit)})
    items = []
    for it in r.get("items") or []:
        if not it:
            continue
        if kind == "artists":
            imgs = it.get("images") or []
            items.append({
                "name": it.get("name") or "", "uri": it.get("uri") or "",
                "art": (imgs[-1] or {}).get("url", "") if imgs else "",
                "genres": (it.get("genres") or [])[:3],
                "popularity": it.get("popularity"),
            })
        else:
            album = it.get("album") or {}
            imgs = album.get("images") or []
            items.append({
                "name": it.get("name") or "", "uri": it.get("uri") or "",
                "artists": [a.get("name", "") for a in it.get("artists") or []],
                "art": (imgs[-1] or {}).get("url", "") if imgs else "",
                "album": album.get("name") or "",
            })
    return {"kind": kind, "time_range": tr, "label": TIME_RANGES[tr], "items": items}


def pause() -> None:
    call("PUT", "/me/player/pause")


def resume(device_id: str | None = None) -> None:
    call("PUT", "/me/player/play", {}, {"device_id": device_id} if device_id else None)


def skip(direction: str = "next") -> None:
    call("POST", f"/me/player/{'previous' if direction == 'previous' else 'next'}")


def set_shuffle(on: bool) -> None:
    call("PUT", "/me/player/shuffle", None, {"state": "true" if on else "false"})


def search(q: str, kinds: str = "playlist,album,artist,track", limit: int = 8) -> dict:
    return call("GET", "/search", None, {"q": q, "type": kinds, "limit": limit})


def now_playing() -> dict:
    return call("GET", "/me/player") or {}
