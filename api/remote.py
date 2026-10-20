"""
Away-from-home access through a Cloudflare quick tunnel (bin/tunnel.sh).

At home (LAN / Tailscale) the API stays open as before. Anything that arrives
through the tunnel — Cloudflare marks it with cf-* headers — must carry a key,
as "Authorization: Bearer <key>" or "?token=<key>" (WebSocket, images).

The key can be either UMBRA_REMOTE_KEY (the tunnel's own secret) or
XUMBRA_API_TOKEN (the token the app uses everywhere else), so the phone needs
one secret, not two. GET /api/v2/remote hands the app the API token (or the
remote key when no API token is configured) plus the tunnel URL and the ntfy
topic, and it refuses tunnel requests, so the secret never travels through the
tunnel in clear.
"""
from __future__ import annotations

import json
import os
import secrets
from typing import Optional
from urllib.parse import parse_qs

from fastapi import APIRouter, HTTPException, Request

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENV = os.path.join(BASE_DIR, ".env")
router = APIRouter(prefix="/api/v2")


def _env_value(name: str) -> str:
    v = os.getenv(name, "").strip()
    if v:
        return v
    try:
        for line in open(ENV, encoding="utf-8"):
            if line.startswith(name + "="):
                return line.split("=", 1)[1].strip().strip('"')
    except Exception:
        pass
    return ""


def _ensure(name: str, make) -> str:
    """Read a secret from .env, creating it once if missing."""
    v = _env_value(name)
    if not v:
        v = make()
        with open(ENV, "a", encoding="utf-8") as f:
            f.write(f"\n{name}={v}\n")
        os.environ[name] = v
    return v


REMOTE_KEY = _ensure("UMBRA_REMOTE_KEY", lambda: secrets.token_urlsafe(32))
NTFY_TOPIC = _ensure("UMBRA_NTFY_TOPIC", lambda: "umbra-" + secrets.token_hex(12))
# The API token (XUMBRA_API_TOKEN) is accepted by the tunnel gate too, so the
# app keeps using one key at home and away.
API_TOKEN = _env_value("XUMBRA_API_TOKEN")
TUNNEL_FILE = os.path.join(BASE_DIR, "data", "settings", "tunnel.json")


def _via_tunnel(headers: dict) -> bool:
    return any(k in headers for k in ("cf-connecting-ip", "cf-ray", "cf-ipcountry"))


class TunnelGate:
    """ASGI middleware: tunnel requests need the remote key; home requests pass as before."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            return await self.app(scope, receive, send)
        headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers") or []}
        if not _via_tunnel(headers):
            return await self.app(scope, receive, send)
        tok = ""
        auth = headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            tok = auth[7:].strip()
        if not tok:
            qs = parse_qs((scope.get("query_string") or b"").decode())
            tok = (qs.get("token") or qs.get("key") or [""])[0]
        path = scope.get("path", "")
        if path == "/api/v2/remote":
            # Never through the tunnel, whatever key it carries: this endpoint
            # is where the secret itself is handed out.
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 4403})
                return
            await send({"type": "http.response.start", "status": 403,
                        "headers": [(b"content-type", b"application/json")]})
            await send({"type": "http.response.body", "body": b'{"detail":"only from home"}'})
            return
        good = bool(tok) and (secrets.compare_digest(tok, REMOTE_KEY)
                              or (bool(API_TOKEN) and secrets.compare_digest(tok, API_TOKEN)))
        ok = good
        if path == "/api/health":
            ok = True   # lets the app probe the tunnel before it knows it's the right one
        if ok:
            return await self.app(scope, receive, send)
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 4401})
            return
        await send({"type": "http.response.start", "status": 401,
                    "headers": [(b"content-type", b"application/json")]})
        await send({"type": "http.response.body", "body": b'{"detail":"remote key required"}'})


def tunnel_url() -> Optional[str]:
    try:
        return json.load(open(TUNNEL_FILE)).get("url")
    except Exception:
        return None


@router.get("/remote")
def remote(request: Request):
    """Only answers at home: the key + where to find the tunnel when away."""
    if _via_tunnel({k.lower(): v for k, v in request.headers.items()}):
        raise HTTPException(403, "only from home")
    # The app stores this as its API token: hand it the one that works, and
    # prefer the API token now that the API requires it.
    return {"key": API_TOKEN or REMOTE_KEY, "ntfy_topic": NTFY_TOPIC, "tunnel": tunnel_url()}
