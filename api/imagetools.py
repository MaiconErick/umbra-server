"""
Image tools for the app's picture viewer.

  POST /api/v2/sessions/{sid}/pixelfix    {media | url}      → real pixel art, posted to the chat
  POST /api/v2/sessions/{sid}/post-image  {media, text, role} → put an image in the chat (no AI reply)

Pixel fix = Retro Diffusion's open-source pixel-art-fixer (MIT, vendor/pixelfixer, built from
github.com/Retro-Diffusion/pixel-art-fixer for aarch64): finds the real pixel grid in "fake" AI pixel art
and rebuilds it at native size. It runs BOXED — one thread, low priority, memory and time capped —
because the unboxed version on all six cores froze the Pi (2026-10-08). One fix at a time.
"""
from __future__ import annotations

import io
import json
import logging
import os
import subprocess
import threading
import time
import uuid
from typing import Optional

import requests
from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

from api.hub import HUB, MEDIA_DIR, require_auth
from store import messages as msg_store
from store.manager import update_session

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v2", tags=["imagetools"])
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXER = os.path.join(BASE_DIR, "vendor", "pixelfixer")
_one_at_a_time = threading.Lock()


class FixBody(BaseModel):
    media: Optional[str] = None      # a file in temp/media (what the app shows as /api/media/<name>)
    url: Optional[str] = None        # or any picture on the web
    mode: str = "full"


class PostBody(BaseModel):
    media: str
    text: str = ""
    role: str = "user"


def _src_path(b: FixBody) -> str:
    if b.media:
        name = os.path.basename(b.media)
        p = os.path.join(MEDIA_DIR, name)
        if not os.path.exists(p):
            raise HTTPException(404, "picture not found")
        return p
    if b.url:
        r = requests.get(b.url, timeout=30, headers={"User-Agent": "Mozilla/5.0 (Umbra)"})
        r.raise_for_status()
        p = os.path.join(MEDIA_DIR, f"src_{uuid.uuid4().hex}.png")
        from PIL import Image
        Image.open(io.BytesIO(r.content)).convert("RGBA").save(p)
        return p
    raise HTTPException(400, "send media or url")


def _post(sid: str, role: str, text: str, media: list[str], extra: dict | None = None) -> dict:
    rec = msg_store.append(sid, {"role": role, "text": text, "media": media, "source": "app", **(extra or {})})
    update_session(sid)
    HUB.publish(sid, "message", {"sid": sid, "message": rec})
    try:
        from api.registry import REGISTRY
        REGISTRY.drop(sid)          # the model sees the new picture next turn
    except Exception:
        pass
    return rec


@router.post("/sessions/{sid}/pixelfix")
def pixelfix(sid: str, b: FixBody, authorization: Optional[str] = Header(None)):
    require_auth(authorization)
    if not os.path.exists(FIXER):
        raise HTTPException(500, "pixel fixer not installed")
    src = _src_path(b)
    from PIL import Image
    w0, h0 = Image.open(src).size
    out = os.path.join(MEDIA_DIR, f"pixelfix_{uuid.uuid4().hex[:12]}_native.png")
    mode = "fast" if b.mode == "fast" else "full"
    if not _one_at_a_time.acquire(timeout=90):
        raise HTTPException(429, "another picture is being fixed, try again in a moment")
    t0 = time.time()
    try:
        # boxed: 1 thread, lowest priority, ≤ 1 core, ≤ 300 MB, ≤ 60 s
        cmd = ["sudo", "-n", "systemd-run", "--quiet", "--scope", f"--uid={os.getuid()}",
               "-p", "MemoryMax=300M", "-p", "CPUQuota=100%", "-E", "RAYON_NUM_THREADS=1",
               "nice", "-n", "19", "timeout", "60", FIXER, "process", src, out, mode]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
    finally:
        _one_at_a_time.release()
    info = {}
    for line in (r.stdout or "").splitlines():
        if line.strip().startswith("{"):
            try:
                info = json.loads(line)
            except Exception:
                pass
    if r.returncode != 0 or not os.path.exists(out):
        logger.warning(f"pixelfix failed rc={r.returncode}: {r.stderr[-300:]}")
        raise HTTPException(422, "couldn't find a pixel grid in this picture" if r.returncode in (0, 1)
                            else f"pixel fixer stopped (code {r.returncode})")
    native = Image.open(out)
    cols, rows = native.size
    # a big copy for looking at (whole-number zoom, every pixel a sharp square)
    f = max(1, min(16, 768 // max(cols, rows)))
    big_name = out.replace("_native.png", f"_x{f}.png")
    native.resize((cols * f, rows * f), Image.NEAREST).save(big_name)
    secs = time.time() - t0
    conf = info.get("confidence") or ("medium" if info.get("consensus") == "arbitrated" else "")
    text = (f"🧩 **Pixel art fixed**: {w0}×{h0} → **{cols}×{rows}** real pixels"
            f"{f' (shown {f}× bigger)' if f > 1 else ''} in {secs:.1f} s."
            f"{' Grid confidence: ' + conf + '.' if conf else ''}\n\nThe second picture is the native {cols}×{rows} PNG.")
    rec = _post(sid, "assistant", text, [os.path.basename(big_name), os.path.basename(out)],
                {"turn": {"provider": "local", "model": "pixel-art-fixer", "duration_sec": round(secs, 2),
                          "input_tokens": 0, "output_tokens": 0, "cost_usd": 0}})
    return {"ok": True, "cols": cols, "rows": rows, "scale": f, "message": rec}


@router.post("/sessions/{sid}/post-image")
def post_image(sid: str, b: PostBody, authorization: Optional[str] = Header(None)):
    """Put a picture the user made (crop, edit) in the chat without asking the AI anything."""
    require_auth(authorization)
    name = os.path.basename(b.media)
    if not os.path.exists(os.path.join(MEDIA_DIR, name)):
        raise HTTPException(404, "picture not found")
    role = b.role if b.role in ("user", "assistant") else "user"
    return {"ok": True, "message": _post(sid, role, b.text, [name])}


# ───────────────────────── for xSpriteManager ─────────────────────────

from fastapi import File, UploadFile


def _boxed_fix(src: str, out: str, mode: str = "full"):
    """The pixel-art fixer, boxed (see module doc). Returns (returncode, detection dict)."""
    if not _one_at_a_time.acquire(timeout=90):
        raise HTTPException(429, "another picture is being fixed, try again in a moment")
    try:
        cmd = ["sudo", "-n", "systemd-run", "--quiet", "--scope", f"--uid={os.getuid()}",
               "-p", "MemoryMax=300M", "-p", "CPUQuota=100%", "-E", "RAYON_NUM_THREADS=1",
               "nice", "-n", "19", "timeout", "60", FIXER, "process", src, out, mode]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
    finally:
        _one_at_a_time.release()
    info = {}
    for line in (r.stdout or "").splitlines():
        if line.strip().startswith("{"):
            try:
                info = json.loads(line)
            except Exception:
                pass
    return r.returncode, info


@router.post("/sprites/detect")
async def sprites_detect(file: UploadFile = File(...), mode: str = "full", authorization: Optional[str] = Header(None)):
    """xSpriteManager: find the real pixel grid of an uploaded picture. Returns the grid + the rebuilt PNG (base64)."""
    require_auth(authorization)
    import base64
    raw = await file.read()
    if len(raw) > 20 * 1024 * 1024:
        raise HTTPException(413, "picture too big")
    tmp = os.path.join(MEDIA_DIR, f"sprite_src_{uuid.uuid4().hex[:12]}.png")
    out = tmp.replace("_src_", "_fix_")
    from PIL import Image
    Image.open(io.BytesIO(raw)).convert("RGBA").save(tmp)
    try:
        rc, info = _boxed_fix(tmp, out, "fast" if mode == "fast" else "full")
        if rc != 0 or not os.path.exists(out):
            raise HTTPException(422, "couldn't find a pixel grid in this picture")
        png = open(out, "rb").read()
        return {"step_x": info.get("step_x"), "step_y": info.get("step_y"), "cols": info.get("cols"),
                "rows": info.get("rows"), "consensus": info.get("consensus"), "confidence": info.get("confidence"),
                "png": base64.b64encode(png).decode()}
    finally:
        for p in (tmp, out):
            try:
                os.remove(p)
            except OSError:
                pass


@router.get("/media/recent")
def media_recent(limit: int = 60, authorization: Optional[str] = Header(None)):
    """xSpriteManager's import picker: the newest pictures across all chats."""
    require_auth(authorization)
    import glob
    idx = {}
    try:
        idx = json.load(open(os.path.join(BASE_DIR, "data", "chats", "sessions_index.json"), encoding="utf-8"))
    except Exception:
        pass
    found = []
    for f in glob.glob(os.path.join(BASE_DIR, "sessions", "msgs", "*.jsonl")):
        sid = os.path.basename(f)[:-6]
        try:
            for line in open(f, encoding="utf-8"):
                r = json.loads(line)
                for m in r.get("media") or []:
                    if m.lower().endswith((".png", ".jpg", ".jpeg", ".webp", ".gif")) and os.path.exists(os.path.join(MEDIA_DIR, m)):
                        found.append({"name": m, "sid": sid, "chat": (idx.get(sid) or {}).get("summary", ""),
                                      "ts": r.get("ts", 0), "role": r.get("role")})
        except Exception:
            continue
    found.sort(key=lambda x: -float(x["ts"] or 0))
    seen, out = set(), []
    for x in found:
        if x["name"] not in seen:
            seen.add(x["name"]); out.append(x)
        if len(out) >= max(1, min(limit, 300)):
            break
    return {"media": out}
