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
import re
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

# Spotify's own algorithmic contexts: the DJ and the personalised mixes. They
# are generated server-side and cannot be offset into by a third-party app.
_ALGORITHMIC = re.compile(r"37i9dQZF1E", re.I)

TIMEOUT = 12.0


class SpotifyError(Exception):
    def __init__(self, message: str, status: int = 0, code: str = ""):
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code


class RateLimited(SpotifyError):
    """Spotify has told us to stop. `until` is a unix timestamp."""

    def __init__(self, until: float, message: str = ""):
        wait = max(0, int(until - time.time()))
        hrs, mins = wait // 3600, (wait % 3600) // 60
        when = f"{hrs}h {mins}m" if hrs else f"{mins}m"
        super().__init__(message or f"Spotify rate limit — retry in {when}", 429,
                         "QUOTA_EXCEEDED")
        self.until = until
        self.wait = wait


# ------------------------------------------------------------- rate limiting
#
# A 429 from Spotify is not a transient blip to retry through. Observed live on
# 2026-08-20: `Retry-After: 39650` — ELEVEN HOURS, with reason QUOTA_EXCEEDED.
# Calling during that window is what earns a window that long in the first
# place, and this app had four widgets polling straight through it.
#
# So the ban is held here, in the process, and no request is made while it
# stands. Every read also goes through a TTL cache, because the widgets are the
# real problem: several of them, on every open page, in every browser pointed at
# the panel, each asking for the same thing on its own timer. One kiosk and one
# laptop double the upstream traffic for identical data.
#
# ⚠️ THE LIMIT IS PER ENDPOINT, not per app. Measured during the same ban:
#
#     200  /me/player            429  /me/player/recently-played
#     200  /me/player/queue      429  /me/top/tracks
#     200  /me/playlists         429  /me
#
# which is exactly why the panel kept showing the current song while the
# history and Top Music went blank. A single global flag would have taken the
# working endpoints down with the banned ones for eleven hours — so the ban is
# held per path.

_BLOCKED: dict[str, float] = {}
_CACHE: dict[tuple, tuple[float, object]] = {}

# How long each read stays fresh. Chosen from how fast the underlying thing
# actually changes, not from how often a widget feels like asking.
TTL = {
    "/me/player": 6.0,                  # the client interpolates progress itself
    "/me/player/queue": 12.0,
    "/me/player/recently-played": 120.0,
    "/me/player/devices": 30.0,
    "/me/top/": 6 * 3600.0,             # your top tracks do not move in an hour
    "/me/playlists": 1800.0,
}
DEFAULT_TTL = 30.0


def _ttl_for(path: str) -> float:
    for prefix, ttl in TTL.items():
        if path.startswith(prefix):
            return ttl
    return DEFAULT_TTL


def blocked_until(path: str) -> float:
    """When this endpoint may be called again; 0 when it is free."""
    until = _BLOCKED.get(path, 0.0)
    if until and until <= time.time():
        _BLOCKED.pop(path, None)
        return 0.0
    return until


def rate_limited() -> dict:
    """Every endpoint currently banned, for diagnostics."""
    now = time.time()
    return {p: round(u - now) for p, u in _BLOCKED.items() if u > now}


def _note_rate_limit(path: str, retry_after: str | None) -> float:
    try:
        wait = int(retry_after or 0)
    except ValueError:
        wait = 0
    # A 429 with no Retry-After still means stop; a minute is the polite floor.
    until = max(_BLOCKED.get(path, 0.0), time.time() + max(wait, 60))
    _BLOCKED[path] = until
    return until


def cached_get(path: str, params: dict | None = None, ttl: float | None = None):
    """A GET that shares one upstream call between every caller and client.

    On a rate limit this returns the last value it saw, however old, rather than
    failing. A widget showing music from four minutes ago is right about almost
    everything; a widget showing "Too many requests" for eleven hours is useful
    to nobody. The staleness is reported separately so the UI can say so.
    """
    key = (path, tuple(sorted((params or {}).items())))
    ttl = _ttl_for(path) if ttl is None else ttl
    now = time.time()
    hit = _CACHE.get(key)
    if hit and now < hit[0]:
        return hit[1], False
    try:
        value = call("GET", path, None, params)
    except RateLimited:
        if hit:
            return hit[1], True         # stale, but real
        raise
    except SpotifyError:
        # A transient failure should not throw away a good answer either.
        if hit and now - (hit[0] - ttl) < 600:
            return hit[1], True
        raise
    _CACHE[key] = (now + ttl, value)
    return value, False


def forget_cached(prefix: str = "") -> None:
    """Drop cached reads — after a command that changes what they would say."""
    for key in [k for k in _CACHE if not prefix or k[0].startswith(prefix)]:
        _CACHE.pop(key, None)


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
    # Spotify returns the GRANTED scopes on both exchange and refresh. Keeping
    # them is the only way to notice that a stored token predates a scope the
    # code now needs — otherwise the failure shows up much later as a bare
    # "Insufficient client scope" from whichever feature needed it.
    if d.get("scope"):
        store.set_setting("spotify_scopes", d["scope"])
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


def granted_scopes() -> list[str]:
    return sorted((store.get_setting("spotify_scopes") or "").split())


def missing_scopes() -> list[str]:
    """Scopes the code needs that the stored token was never granted."""
    if not connected():
        return []
    have = set(granted_scopes())
    # An older token predates the setting entirely; treat unknown as "stale"
    # rather than "fine", because silently assuming fine is how this hid.
    if not have:
        return list(SCOPES)
    return [s for s in SCOPES if s not in have]


SCOPE_PURPOSE = {
    "user-read-recently-played": "ordering playlists by what you last played",
    "user-top-read": "your top artists and tracks",
    "user-library-read": "your saved tracks and albums",
    "user-follow-read": "artists you follow",
    "user-read-private": "knowing whether the account is Premium",
    "playlist-read-collaborative": "collaborative playlists",
}


def disconnect() -> None:
    for k in ("spotify_refresh_token", "spotify_access_token", "spotify_expires_at",
              "spotify_scopes"):
        store.set_setting(k, "")


# --------------------------------------------------------------------- api

def call(method: str, path: str, body: dict | None = None, params: dict | None = None):
    # Refuse before the socket, not after. Every call made inside a ban is a
    # call that can extend it, and the answer is known in advance anyway. Held
    # per path, because only some endpoints are ever banned at once.
    until = blocked_until(path)
    if until:
        raise RateLimited(until)
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
            raw = r.read().decode(errors="replace")
            if not raw.strip():
                return {}                # 204: success with nothing to say
            try:
                return json.loads(raw)
            except ValueError:
                # The player endpoints do not reliably return JSON on success —
                # observed live: pause() came back 2xx with a body that raised
                # "Extra data: line 1 column 2". The request WORKED; only its
                # body was unparseable, and no caller of a command endpoint
                # reads that body anyway.
                #
                # Raising here reported successful commands as failures, which
                # is exactly how an alarm that really did start the music
                # recorded itself as broken.
                return {}
    except urllib.error.HTTPError as e:
        if e.code == 429:
            until = _note_rate_limit(path, e.headers.get("Retry-After"))
            raise RateLimited(until)
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


def transfer(device_id: str, play: bool = False, attempts: int = 4) -> None:
    """Hand playback to a device, tolerating one that has only just appeared.

    A Connect device is listed the moment it registers, but Spotify will 404 a
    transfer to it for a second or two afterwards. The alarm hits that window
    every time: it launches the app and hands over as soon as the device shows
    up. Retrying briefly is the difference between working from cold and only
    working when the device happened to be registered already.
    """
    last = None
    for i in range(attempts):
        try:
            call("PUT", "/me/player", {"device_ids": [device_id], "play": play})
            return
        except SpotifyError as e:
            if e.status not in (404, 202):
                raise
            last = e
            time.sleep(0.8 * (i + 1))
    if last:
        raise last


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
    forget_cached("/me/player")


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
    matched = sum(1 for p in items if p["uri"] in rank)

    # Recently played first, then ALPHABETICAL — not Spotify's own order, which
    # is arbitrary from the outside and useless for finding one playlist among a
    # hundred. Recency only helps if the history actually contains library
    # playlists; a history made entirely of Spotify-owned mixes (DJ, Daily Mix)
    # matches nothing, so alphabetical is what the list falls back to.
    items.sort(key=lambda p: (rank.get(p["uri"], len(rank)), p["name"].lower()))
    for p in items:
        p["recent_rank"] = rank.get(p["uri"])
    return {
        "playlists": items,
        "count": len(items),
        # `recency` said only whether the CALL worked, which read as "ordered by
        # recency" even when nothing was reordered. What matters is how many
        # actually matched.
        "recency": have_recency,
        "recency_matched": matched,
        "recent_contexts": len(recent),
    }


def code_url(uri: str) -> str:
    return SCANNABLE.format(uri=urllib.parse.quote(uri, safe="")) if uri else ""


_CODE_CACHE: dict[str, bytes] = {}
_BG_RECT = re.compile(rb'<rect\s+x="0"\s+y="0"[^>]*?/>')


def code_svg(uri: str, ink: str = "white") -> bytes:
    """The scannable with its background removed, so only the bars remain.

    Spotify's service rejects `transparent` as a background colour (400), and
    the SVG it returns opens with one full-bleed <rect> painting the panel. That
    rect is the background — dropping it leaves the bars and the logo over
    whatever is behind them, which is the only way to sit the code on a tinted
    footer without a black slab around it.

    `ink` picks the bar colour at the source rather than inverting afterwards; a
    CSS filter would also invert the logo and any future colour in the artwork.
    """
    ink = "black" if str(ink).lower() == "black" else "white"
    key = f"{uri}|{ink}"
    if key in _CODE_CACHE:
        return _CODE_CACHE[key]

    # Background is requested as the OPPOSITE of the ink purely so the fetched
    # file is legible if anything ever renders it unmodified.
    bg = "ffffff" if ink == "black" else "000000"
    url = (f"https://scannables.scdn.co/uri/plain/svg/{bg}/{ink}/640/"
           + urllib.parse.quote(uri, safe=""))
    req = urllib.request.Request(url, headers={"User-Agent": "DigiCalender/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            raw = r.read()
    except (urllib.error.URLError, OSError) as e:
        raise SpotifyError(f"Could not fetch the code image: {e}", 0)

    out = _BG_RECT.sub(b"", raw, count=1)
    if len(_CODE_CACHE) > 64:            # a wall panel, not a CDN
        _CODE_CACHE.clear()
    _CODE_CACHE[key] = out
    return out


def seek(position_ms: int, device_id: str | None = None) -> None:
    params = {"position_ms": max(0, int(position_ms))}
    if device_id:
        params["device_id"] = device_id
    call("PUT", "/me/player/seek", None, params)
    forget_cached("/me/player")


def play_track(uri: str, context_uri: str = "", device_id: str | None = None) -> dict:
    """Play one specific track — what a tap on the timeline means.

    When the track came from a playlist or album, that context is passed with
    the track as an OFFSET, so the rest keeps playing after it. Sending the
    track alone would play it and then stop, which is not what tapping a song in
    a list means anywhere else.

    A context that Spotify will not accept from a third-party app — the DJ and
    the other algorithmic ones — falls back to playing the track on its own.
    That does end the DJ, and there is no way around it: the DJ has no API.
    """
    u = (uri or "").strip()
    if ":track:" not in u:
        raise SpotifyError("That is not a track", 400)
    params = {"device_id": device_id} if device_id else None
    ctx = (context_uri or "").strip()

    # An algorithmic context (37i9dQZF1E* — the DJ, Daily Mix, Discover Weekly)
    # is generated on Spotify's side and has no stable track list to offset
    # into. Passing one ACCEPTS the request and then plays whatever it wants,
    # so a tap on a specific song would start a different one. Dropping the
    # context plays the song that was actually tapped, which is the whole point
    # of the gesture; it does end the DJ, and there is no API that would not.
    if _ALGORITHMIC.search(ctx):
        ctx = ""

    if ctx and ":track:" not in ctx:
        try:
            call("PUT", "/me/player/play",
                 {"context_uri": ctx, "offset": {"uri": u}}, params)
            forget_cached("/me/player")
            return {"played": u, "context": ctx}
        except SpotifyError as e:
            if e.status not in (403, 404):
                raise
            # Fall through: the context is one Spotify will not hand over.

    call("PUT", "/me/player/play", {"uris": [u]}, params)
    forget_cached("/me/player")
    return {"played": u, "context": ""}


def now_playing() -> dict:
    """A stable shape for the player, whatever Spotify is doing.

    Returns `playing: False` rather than raising when nothing is active — an
    idle player is a normal state, not an error, and a widget should not have to
    tell those apart.
    """
    # An idle player is polled far less than a playing one. Overnight this is
    # the difference between a few hundred wasted calls and a few dozen, and
    # the quota is a daily budget.
    last = _CACHE.get(("/me/player", ()))
    idle = bool(last and not ((last[1] or {}).get("is_playing")))
    r, stale = cached_get("/me/player", None, 45.0 if idle else 10.0)
    r = r or {}
    item = r.get("item") or {}
    if not item:
        return {"playing": False, "track": None, "device": None, "stale": stale}
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


def _track_view(t: dict) -> dict:
    album = (t or {}).get("album") or {}
    images = album.get("images") or []
    return {
        "name": t.get("name") or "",
        "uri": t.get("uri") or "",
        "artists": [a.get("name", "") for a in t.get("artists") or []],
        "album": album.get("name") or "",
        "art": (images[-1] or {}).get("url", "") if images else "",
        "duration_ms": t.get("duration_ms") or 0,
    }


def timeline(history: int = 20) -> dict:
    """Previously played, what is on now, and what is next — one ordered strip.

    History arrives newest-first and is reversed here, so the whole list reads
    forward in time: oldest at the start, the queue at the end. A caller
    rendering top-to-bottom or left-to-right can then just walk it.
    """
    past: list[dict] = []
    stale = False
    # An empty history and an UNREADABLE history are different facts, and
    # collapsing them is how a rate limit spent a day looking like "you have not
    # listened to anything". Whatever went wrong is reported, by name.
    history_error = ""
    try:
        rec, s1 = cached_get("/me/player/recently-played", {"limit": min(50, history)})
        stale = stale or s1
        for item in ((rec or {}).get("items") or []):
            t = (item or {}).get("track")
            if not t:
                continue
            view = _track_view(t)
            view["played_at"] = item.get("played_at") or ""
            ctx = item.get("context") or {}
            view["context_uri"] = ctx.get("uri") or ""
            past.append(view)
        past.reverse()
    except SpotifyError as e:
        history_error = e.message

    current, upcoming = None, []
    queue_error = ""
    try:
        q, s2 = cached_get("/me/player/queue")
        q = q or {}
        stale = stale or s2
        cur = q.get("currently_playing")
        if cur:
            current = _track_view(cur)
        upcoming = [_track_view(t) for t in (q.get("queue") or [])[:20] if t]
    except SpotifyError as e:
        queue_error = e.message

    # The queue endpoint repeats the current track at the head of history for
    # some clients; drop an immediate duplicate so it is not shown twice.
    if current and past and past[-1].get("uri") == current.get("uri"):
        past.pop()

    return {"past": past, "current": current, "next": upcoming, "stale": stale,
            "history_error": history_error, "queue_error": queue_error}


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
    # Cached for hours: this is the widget that broke first under the rate
    # limit, and it is also the one whose answer changes least.
    r, stale = cached_get(f"/me/top/{kind}", {"time_range": tr, "limit": min(50, limit)})
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
    return {"kind": kind, "time_range": tr, "label": TIME_RANGES[tr],
            "items": items, "stale": stale}


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

