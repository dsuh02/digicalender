"""
Google Gemini client.

Stdlib urllib, like every other integration here — no pip, no build step.

Two deliberate choices:

**The key travels in a header, not the query string.** Google accepts both, and
every published example uses `?key=…`. A key in a URL ends up in access logs,
in `Referer` headers, and in any proxy in between. `x-goog-api-key` costs
nothing and leaks nothing.

**Model names are never hardcoded.** Which models a key can reach depends on the
project, the tier and the month; a literal that was right when it was written
returns 404 for somebody else later. The app asks the key what it can use and
lets the user pick, so a model being renamed or retired is a dropdown that
changed rather than a feature that broke.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request

import store

BASE = "https://generativelanguage.googleapis.com/v1beta"
TIMEOUT = 45.0
KEY_SETTING = "gemini_api_key"
MODEL_SETTING = "gemini_model"


class GeminiError(Exception):
    def __init__(self, message: str, status: int = 0):
        super().__init__(message)
        self.message = message
        self.status = status


def api_key() -> str:
    k = (store.get_setting(KEY_SETTING) or "").strip()
    if not k:
        raise GeminiError("No Gemini API key — add one under Settings › AI", 400)
    return k


def configured() -> bool:
    return bool((store.get_setting(KEY_SETTING) or "").strip())


def model_name() -> str:
    return (store.get_setting(MODEL_SETTING) or "").strip()


def _request(method: str, path: str, body: dict | None = None) -> dict:
    url = f"{BASE}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Content-Type": "application/json",
        "x-goog-api-key": api_key(),
    })
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            d = json.loads(e.read().decode() or "{}")
            detail = ((d.get("error") or {}).get("message") or "")
        except Exception:
            pass
        if e.code == 400 and "API key not valid" in detail:
            detail = "That API key was rejected — check it was copied whole"
        elif e.code == 429 and not detail:
            detail = "Rate limited by Gemini — the free tier has per-minute limits"
        elif e.code == 403 and not detail:
            detail = "Gemini refused the request — check the key's project has the API enabled"
        raise GeminiError(detail or f"Gemini returned {e.code}", e.code)
    except (urllib.error.URLError, OSError) as e:
        raise GeminiError(f"Could not reach Gemini: {e}", 0)


def list_models() -> list[dict]:
    """Models this key can actually use, newest-looking first.

    Filtered to those supporting generateContent — the list also carries
    embedding and other model families that would fail if picked.
    """
    out = []
    for m in (_request("GET", "/models") or {}).get("models") or []:
        methods = m.get("supportedGenerationMethods") or []
        if "generateContent" not in methods:
            continue
        name = str(m.get("name") or "")
        out.append({
            "id": name.removeprefix("models/"),
            "label": m.get("displayName") or name,
            "input_tokens": m.get("inputTokenLimit"),
            "output_tokens": m.get("outputTokenLimit"),
        })
    out.sort(key=lambda x: x["id"])
    return out


def generate(prompt: str, *, model: str = "", system: str = "",
             max_tokens: int = 800, temperature: float = 0.7) -> str:
    """One turn in, text out."""
    mdl = (model or model_name()).strip()
    if not mdl:
        raise GeminiError("No model chosen — pick one under Settings › AI", 400)
    body: dict = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": temperature,
            # Never squeeze this below the worst case: a truncated answer that
            # looks complete is worse than an error.
            "maxOutputTokens": max(64, int(max_tokens)),
        },
    }
    if system:
        body["systemInstruction"] = {"parts": [{"text": system}]}

    res = _request("POST", f"/models/{urllib.parse.quote(mdl)}:generateContent", body)
    cands = res.get("candidates") or []
    if not cands:
        blocked = ((res.get("promptFeedback") or {}).get("blockReason") or "")
        raise GeminiError(f"Gemini returned nothing{f' ({blocked})' if blocked else ''}", 0)
    parts = ((cands[0].get("content") or {}).get("parts") or [])
    text = "".join(p.get("text", "") for p in parts).strip()
    if not text:
        reason = cands[0].get("finishReason") or ""
        raise GeminiError(f"Gemini returned an empty answer{f' ({reason})' if reason else ''}", 0)
    return text


def check() -> dict:
    """Prove the key works, end to end, without spending much."""
    models = list_models()
    mdl = model_name() or (models[0]["id"] if models else "")
    if not mdl:
        raise GeminiError("The key works, but no model supports generateContent", 0)
    reply = generate("Reply with exactly: ok", model=mdl, max_tokens=64, temperature=0)
    return {"ok": True, "model": mdl, "reply": reply[:120], "models": len(models)}
