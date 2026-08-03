"""
Plaid client — stdlib HTTP, no SDK.

Uses **Hosted Link** rather than the embedded Link widget, for two reasons that
both matter here:

  1. This panel has no on-screen keyboard. Bank credentials get typed on a
     laptop or phone, so the flow has to be a URL you can open anywhere.
  2. Plaid's JS widget expects a secure context; the app is served over plain
     HTTP on a LAN address. Hosted Link runs on Plaid's own HTTPS page, so the
     question never arises — and no CDN script is loaded into the panel.

The completed session is collected by polling /link/token/get, which returns
the public_token once the user finishes. That avoids needing a public webhook
endpoint, which a box behind a home router does not have.

Credentials (client_id / secret / environment) live in the settings table and
are supplied by the user; nothing is baked in.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

HOSTS = {
    "sandbox": "https://sandbox.plaid.com",
    "production": "https://production.plaid.com",
}
TIMEOUT = 25.0


class PlaidError(Exception):
    """Carries Plaid's own error_code/message — those are actually useful."""

    def __init__(self, message, code="", etype="", status=0):
        super().__init__(message)
        self.message = message
        self.code = code
        self.etype = etype
        self.status = status


def _creds():
    import store
    cid = (store.get_setting("plaid_client_id") or "").strip()
    sec = (store.get_setting("plaid_secret") or "").strip()
    env = (store.get_setting("plaid_env") or "sandbox").strip()
    if env not in HOSTS:
        env = "sandbox"
    if not cid or not sec:
        raise PlaidError("Plaid isn't configured — add your client ID and secret "
                         "under Settings › Money.")
    return cid, sec, env


def call(path: str, payload: dict) -> dict:
    cid, sec, env = _creds()
    body = json.dumps({**payload, "client_id": cid, "secret": sec}).encode()
    req = urllib.request.Request(
        HOSTS[env] + path, data=body, method="POST",
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        raw = ""
        try:
            raw = e.read().decode()
            d = json.loads(raw or "{}")
        except Exception:
            d = {}
        raise PlaidError(
            d.get("error_message") or f"Plaid returned {e.code}",
            d.get("error_code", ""), d.get("error_type", ""), e.code)
    except (urllib.error.URLError, OSError) as e:
        raise PlaidError(f"could not reach Plaid: {e}")


def env_name() -> str:
    try:
        return _creds()[2]
    except PlaidError:
        return "sandbox"


DEFAULT_PRODUCTS = ["transactions"]
# Asked for where the institution supports them; a plain checking-only bank
# must not fail to link because it has no liabilities or holdings.
OPTIONAL_PRODUCTS = ["liabilities", "investments"]


def create_hosted_link(user_id: str, products=None) -> dict:
    """Returns {link_token, hosted_link_url, expiration}."""
    import store
    prods = products or json.loads(store.get_setting("plaid_products", "null") or "null") \
        or DEFAULT_PRODUCTS
    return call("/link/token/create", {
        "user": {"client_user_id": user_id},
        "client_name": "DigiCalender",
        "language": "en",
        "country_codes": ["US"],
        "products": prods,
        "optional_products": OPTIONAL_PRODUCTS,
        "hosted_link": {"completion_redirect_uri": None},
    })


def get_link_results(link_token: str) -> dict:
    return call("/link/token/get", {"link_token": link_token})


def public_token_from_results(res: dict) -> tuple[str, str]:
    """Dig the public_token + institution out of a completed Link session.

    Plaid nests these under link_sessions[].results.item_add_results[]; the
    shape has moved between versions, so tolerate a missing branch rather than
    exploding on a KeyError mid-link.
    """
    for sess in res.get("link_sessions") or []:
        results = sess.get("results") or {}
        for add in results.get("item_add_results") or []:
            tok = add.get("public_token")
            if tok:
                inst = (add.get("institution") or {}).get("name", "")
                return tok, inst
        # Older/alternate placement
        tok = (results.get("item_add_result") or {}).get("public_token")
        if tok:
            return tok, ((results.get("item_add_result") or {})
                         .get("institution") or {}).get("name", "")
    return "", ""


def exchange(public_token: str) -> dict:
    return call("/item/public_token/exchange", {"public_token": public_token})


def accounts_balance(access_token: str) -> dict:
    return call("/accounts/balance/get", {"access_token": access_token})


def liabilities(access_token: str) -> dict:
    """Due dates, APRs and minimum payments for cards and student loans.
    Not every item supports it — callers treat failure as "no extra detail"."""
    return call("/liabilities/get", {"access_token": access_token})


def investments(access_token: str) -> dict:
    return call("/investments/holdings/get", {"access_token": access_token})


def transactions_sync(access_token: str, cursor: str | None = None, count: int = 500) -> dict:
    """One page of the /transactions/sync cursor stream.

    Cursor-based, not date-range: Plaid replays added/modified/removed since the
    cursor, which is what makes a re-sync idempotent instead of a duplicate
    import. An empty cursor means "from the beginning", and the first call on a
    fresh item can legitimately return nothing while Plaid is still pulling
    history — that is a wait, not an error.
    """
    body = {"access_token": access_token, "count": count}
    if cursor:
        body["cursor"] = cursor
    return call("/transactions/sync", body)


def item_remove(access_token: str) -> dict:
    return call("/item/remove", {"access_token": access_token})


def institution_name(institution_id: str) -> str:
    if not institution_id:
        return ""
    try:
        d = call("/institutions/get_by_id", {
            "institution_id": institution_id, "country_codes": ["US"]})
        return (d.get("institution") or {}).get("name", "")
    except PlaidError:
        return ""
