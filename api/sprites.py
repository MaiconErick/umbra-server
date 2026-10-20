"""
xSpriteManager's backend: every bit of image work happens here on the Pi; the app only shows the
results and sends what the user picks.

A project lives in data/sprites/<id>/:
    source.png   what was imported (never changed)
    work.png     after Clean (background removed / colours erased)
    pixels.png   after Grid (real pixels rebuilt) — optional
    history/     earlier work.png's for Undo (last 12)
    meta.json    name, grid, slices (boxes on the current image), animations

The "current" image is pixels.png if it exists, else work.png, else source.png. Slices are boxes on it.

Light work (NumPy/SciPy, well under a second) runs in-process. Grid detection uses the Rust
pixel-art fixer through imagetools._boxed_fix (1 thread, capped memory/time) — unboxed it froze the Pi.
"""
from __future__ import annotations

import io
import json
import os
import shutil
import time
import uuid
import zipfile
from typing import Optional

import numpy as np
from fastapi import APIRouter, File, Form, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response
from PIL import Image
from pydantic import BaseModel
from scipy import ndimage

from api.hub import MEDIA_DIR, require_auth

router = APIRouter(prefix="/api/v2/sprites", tags=["sprites"])
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ROOT = os.path.join(BASE_DIR, "data", "sprites")
MAX_PIXELS = 4_200_000


# ───────────────────────── storage ─────────────────────────

def _dir(pid: str) -> str:
    pid = os.path.basename(pid)
    d = os.path.join(ROOT, pid)
    if not os.path.isdir(d):
        raise HTTPException(404, "project not found")
    return d


def _meta(pid: str) -> dict:
    return json.load(open(os.path.join(_dir(pid), "meta.json"), encoding="utf-8"))


def _save_meta(pid: str, m: dict):
    m["updated"] = time.time()
    p = os.path.join(_dir(pid), "meta.json")
    with open(p + ".tmp", "w", encoding="utf-8") as f:
        json.dump(m, f, ensure_ascii=False)
    os.replace(p + ".tmp", p)


def _load(path: str) -> np.ndarray:
    return np.array(Image.open(path).convert("RGBA"))


def _write(path: str, arr: np.ndarray):
    Image.fromarray(arr.astype(np.uint8), "RGBA").save(path + ".tmp.png")
    os.replace(path + ".tmp.png", path)


def _current_name(pid: str) -> str:
    d = _dir(pid)
    for n in ("pixels.png", "work.png", "source.png"):
        if os.path.exists(os.path.join(d, n)):
            return n
    raise HTTPException(500, "project has no image")


def _work(pid: str) -> np.ndarray:
    d = _dir(pid)
    p = os.path.join(d, "work.png")
    return _load(p if os.path.exists(p) else os.path.join(d, "source.png"))


def _push_history(pid: str):
    d = _dir(pid)
    hd = os.path.join(d, "history"); os.makedirs(hd, exist_ok=True)
    cur = os.path.join(d, "work.png")
    if os.path.exists(cur):
        shutil.copy(cur, os.path.join(hd, f"{time.time():.3f}.png"))
    olds = sorted(os.listdir(hd))
    for f in olds[:-12]:
        os.remove(os.path.join(hd, f))


def _set_work(pid: str, arr: np.ndarray):
    """New Clean result: work.png changes, so later steps (pixels, slices) are out of date."""
    _push_history(pid)
    d = _dir(pid)
    _write(os.path.join(d, "work.png"), arr)
    px = os.path.join(d, "pixels.png")
    if os.path.exists(px):
        os.remove(px)
    m = _meta(pid); m["slices"] = []; m["stage"] = "clean"; _save_meta(pid, m)


def _view(pid: str) -> dict:
    m = _meta(pid)
    d = _dir(pid)
    cur = _current_name(pid)
    w, h = Image.open(os.path.join(d, cur)).size
    sw, sh = Image.open(os.path.join(d, "source.png")).size
    m.update(current=cur, width=w, height=h, src_w=sw, src_h=sh, has_work=os.path.exists(os.path.join(d, "work.png")),
             has_pixels=os.path.exists(os.path.join(d, "pixels.png")),
             undo=len(os.listdir(os.path.join(d, "history"))) if os.path.isdir(os.path.join(d, "history")) else 0,
             version=int(os.path.getmtime(os.path.join(d, cur)) * 1000))
    return m


# ───────────────────────── image operations ─────────────────────────

def _maxdiff(a: np.ndarray, c) -> np.ndarray:
    return np.abs(a[..., :3].astype(np.int16) - np.array(c[:3], np.int16)).max(axis=-1)


def background_table(a: np.ndarray, tol: int) -> np.ndarray:
    """
    Which colours count as background: the ones the picture's border is made of. Colours are
    binned (8 levels per channel); the bins covering 98% of the border are kept and widened by
    [tol]. Works for solid backgrounds, gradients and the noisy fake "transparency" checkerboards
    image models draw.
    """
    b = np.concatenate([a[0], a[-1], a[:, 0], a[:, -1]])
    b = b[b[:, 3] > 8][:, :3].astype(np.int32) >> 3
    table = np.zeros((32, 32, 32), bool)
    if len(b) == 0:
        return table
    keys, cnt = np.unique(b, axis=0, return_counts=True)
    order = np.argsort(-cnt)
    keep = order[: int(np.searchsorted(np.cumsum(cnt[order]), 0.98 * cnt.sum()) + 1)]
    table[keys[keep, 0], keys[keep, 1], keys[keep, 2]] = True
    r = max(0, int(round(tol / 8)))
    if r:
        table = ndimage.binary_dilation(table, np.ones((3, 3, 3), bool), iterations=r)
    return table


def remove_background(a: np.ndarray, tol: int) -> np.ndarray:
    """Background-coloured pixels connected to the edges become transparent (inner outlines stay)."""
    table = background_table(a, tol)
    q = a[..., :3].astype(np.int32) >> 3
    isbg = (a[..., 3] <= 8) | table[q[..., 0], q[..., 1], q[..., 2]]
    lab, _ = ndimage.label(isbg)
    edge = np.unique(np.concatenate([lab[0], lab[-1], lab[:, 0], lab[:, -1]]))
    edge = edge[edge > 0]
    gone = np.isin(lab, edge)
    # stray specks of background colour left floating in the cleared area (noise in fake checkerboards)
    keep = ~gone & (a[..., 3] > 8)
    klab, kn = ndimage.label(keep)
    if kn:
        sizes = ndimage.sum(keep, klab, range(1, kn + 1))
        bgish = ndimage.mean(isbg, klab, range(1, kn + 1))
        tiny = np.nonzero((sizes <= 24) & (bgish > 0.5))[0] + 1
        gone |= np.isin(klab, tiny)
    out = a.copy()
    out[gone] = 0
    return out


def remove_color(a: np.ndarray, x: int, y: int, tol: int, everywhere: bool) -> np.ndarray:
    c = a[y, x]
    hit = (_maxdiff(a, c) <= tol) & (a[..., 3] > 0)
    if not everywhere:
        lab, _ = ndimage.label(hit)
        if lab[y, x] == 0:
            return a
        hit = lab == lab[y, x]
    out = a.copy(); out[hit] = 0
    return out


def rebuild(a: np.ndarray, sx: float, sy: float, ox: float, oy: float) -> np.ndarray:
    """One real pixel per grid cell: the most common colour among 3×3 samples in the cell's centre."""
    h, w = a.shape[:2]
    cols = max(1, int((w - ox) / sx)); rows = max(1, int((h - oy) / sy))
    fr = np.array([0.3, 0.5, 0.7])
    xs = (ox + (np.arange(cols)[:, None] + fr[None, :]) * sx).clip(0, w - 1).astype(int)   # cols × 3
    ys = (oy + (np.arange(rows)[:, None] + fr[None, :]) * sy).clip(0, h - 1).astype(int)   # rows × 3
    s = a[ys[:, None, :, None], xs[None, :, None, :]]                                       # rows, cols, 3, 3, 4
    s = s.reshape(rows, cols, 9, 4).astype(np.int32)
    opaque = s[..., 3] >= 128
    key = ((s[..., 0] >> 3) << 10) | ((s[..., 1] >> 3) << 5) | (s[..., 2] >> 3)
    key = np.where(opaque, key, -1)
    same = (key[..., :, None] == key[..., None, :]) & opaque[..., None, :]                  # rows, cols, 9, 9
    votes = same.sum(-1)
    win = votes.argmax(-1)                                                                  # rows, cols
    wkey = np.take_along_axis(key, win[..., None], -1)
    sel = (key == wkey) & opaque
    n = np.maximum(sel.sum(-1, keepdims=True), 1)
    rgb = (s[..., :3] * sel[..., None]).sum(-2) // n
    out = np.zeros((rows, cols, 4), np.uint8)
    out[..., :3] = rgb
    out[..., 3] = np.where(opaque.sum(-1) * 2 > 9, 255, 0)
    out[out[..., 3] == 0] = 0
    return out


def reduce_colors(a: np.ndarray, k: int) -> np.ndarray:
    vis = a[..., 3] > 0
    px = a[vis][:, :3].astype(np.float32)
    if len(px) == 0:
        return a
    uniq, inv, cnt = np.unique(px, axis=0, return_inverse=True, return_counts=True)
    if len(uniq) <= k or k < 2:
        return a
    cent = uniq[np.argsort(-cnt)][np.linspace(0, len(uniq) - 1, k).astype(int)].copy()
    for _ in range(12):
        d = ((uniq[:, None, :] - cent[None]) ** 2).sum(-1)
        lab = d.argmin(1)
        for j in range(k):
            m = lab == j
            if m.any():
                cent[j] = (uniq[m] * cnt[m, None]).sum(0) / cnt[m].sum()
    lab = ((uniq[:, None, :] - cent[None]) ** 2).sum(-1).argmin(1)
    out = a.copy()
    out[vis, :3] = np.rint(cent[lab[inv.ravel()]]).clip(0, 255).astype(np.uint8)
    return out


def find_sprites(a: np.ndarray, gap: int, min_area: int) -> list[dict]:
    solid = a[..., 3] > 16
    grown = ndimage.binary_dilation(solid, np.ones((3, 3), bool), iterations=gap) if gap > 0 else solid
    lab, n = ndimage.label(grown)
    boxes = []
    for i, sl in enumerate(ndimage.find_objects(lab), start=1):
        if sl is None:
            continue
        part = solid[sl] & (lab[sl] == i)
        area = int(part.sum())
        if area < min_area:
            continue
        ys, xs = np.nonzero(part)
        boxes.append({"x": int(sl[1].start + xs.min()), "y": int(sl[0].start + ys.min()),
                      "w": int(xs.max() - xs.min() + 1), "h": int(ys.max() - ys.min() + 1)})
    if boxes:
        med = sorted(b["h"] for b in boxes)[len(boxes) // 2]
        boxes.sort(key=lambda b: (b["y"] // max(1, med // 2), b["x"]))
    return boxes


def trim_box(a: np.ndarray, b: dict) -> dict:
    sub = a[b["y"]:b["y"] + b["h"], b["x"]:b["x"] + b["w"], 3] > 16
    if not sub.any():
        return b
    ys, xs = np.nonzero(sub)
    return {**b, "x": b["x"] + int(xs.min()), "y": b["y"] + int(ys.min()), "w": int(xs.max() - xs.min() + 1),
            "h": int(ys.max() - ys.min() + 1)}


def crop(a: np.ndarray, b: dict) -> np.ndarray:
    h, w = a.shape[:2]
    x0 = max(0, min(w - 1, b["x"])); y0 = max(0, min(h - 1, b["y"]))
    x1 = max(x0 + 1, min(w, b["x"] + b["w"])); y1 = max(y0 + 1, min(h, b["y"] + b["h"]))
    return a[y0:y1, x0:x1]


def scale(a: np.ndarray, f: int) -> np.ndarray:
    return a if f <= 1 else np.repeat(np.repeat(a, f, axis=0), f, axis=1)


def sheet(rows: list[list[np.ndarray]], pad: int = 0) -> np.ndarray:
    """One row per animation; equal cells; sprites centred and standing on the cell's bottom."""
    flat = [f for r in rows for f in r] or [np.zeros((1, 1, 4), np.uint8)]
    cw = max(f.shape[1] for f in flat) + 2 * pad; ch = max(f.shape[0] for f in flat) + 2 * pad
    cols = max((len(r) for r in rows), default=1)
    out = np.zeros((ch * max(1, len(rows)), cw * cols, 4), np.uint8)
    for ry, r in enumerate(rows):
        for cx, f in enumerate(r):
            oy = ry * ch + ch - pad - f.shape[0]; ox = cx * cw + (cw - f.shape[1]) // 2
            m = f[..., 3] > 0
            out[oy:oy + f.shape[0], ox:ox + f.shape[1]][m] = f[m]
    return out


def _png(a: np.ndarray) -> bytes:
    bio = io.BytesIO(); Image.fromarray(a.astype(np.uint8), "RGBA").save(bio, "PNG"); return bio.getvalue()


# ───────────────────────── endpoints ─────────────────────────

@router.get("/projects")
def projects(authorization: Optional[str] = Header(None)):
    require_auth(authorization)
    os.makedirs(ROOT, exist_ok=True)
    out = []
    for pid in os.listdir(ROOT):
        try:
            v = _view(pid)
            out.append({k: v.get(k) for k in ("id", "name", "updated", "created", "width", "height", "current", "version", "stage")}
                       | {"slices": len(v.get("slices") or []), "anims": len(v.get("anims") or [])})
        except Exception:
            continue
    out.sort(key=lambda p: -(p.get("updated") or 0))
    return {"projects": out}


@router.post("/projects")
async def create(file: Optional[UploadFile] = File(None), media: str = Form(""), name: str = Form(""),
                 authorization: Optional[str] = Header(None)):
    """New project from an uploaded picture or a picture from an Umbra chat (media = its file name)."""
    require_auth(authorization)
    if file is not None:
        raw = await file.read()
    elif media:
        p = os.path.join(MEDIA_DIR, os.path.basename(media))
        if not os.path.exists(p):
            raise HTTPException(404, "picture not found")
        raw = open(p, "rb").read()
    else:
        raise HTTPException(400, "send a file or a media name")
    im = Image.open(io.BytesIO(raw)).convert("RGBA")
    if im.width * im.height > MAX_PIXELS:
        raise HTTPException(413, f"picture too big ({im.width}×{im.height})")
    pid = uuid.uuid4().hex[:10]
    d = os.path.join(ROOT, pid); os.makedirs(os.path.join(d, "history"), exist_ok=True)
    im.save(os.path.join(d, "source.png"))
    m = {"id": pid, "name": name.strip() or f"Sprites {time.strftime('%d/%m %H:%M')}", "created": time.time(),
         "grid": None, "slices": [], "anims": [], "stage": "new"}
    with open(os.path.join(d, "meta.json"), "w", encoding="utf-8") as f:
        json.dump(m, f, ensure_ascii=False)
    return _view(pid)


@router.get("/projects/{pid}")
def project(pid: str, authorization: Optional[str] = Header(None)):
    require_auth(authorization)
    return _view(pid)


class Rename(BaseModel):
    name: str


@router.patch("/projects/{pid}")
def rename(pid: str, b: Rename, authorization: Optional[str] = Header(None)):
    require_auth(authorization)
    m = _meta(pid); m["name"] = b.name.strip()[:60] or m["name"]; _save_meta(pid, m)
    return _view(pid)


@router.delete("/projects/{pid}")
def delete(pid: str, authorization: Optional[str] = Header(None)):
    require_auth(authorization)
    shutil.rmtree(_dir(pid))
    return {"ok": True}


@router.get("/projects/{pid}/image/{which}")
def image(pid: str, which: str, v: int = 0, authorization: Optional[str] = Header(None)):
    """source | work | pixels | current — as PNG. (?v= is only there to bust the app's cache.)"""
    require_auth(authorization)
    d = _dir(pid)
    name = _current_name(pid) if which == "current" else f"{os.path.basename(which)}.png"
    p = os.path.join(d, name)
    if not os.path.exists(p):
        raise HTTPException(404, "no such image yet")
    return FileResponse(p, media_type="image/png", headers={"Cache-Control": "no-store"})


class CleanBody(BaseModel):
    op: str                 # auto | color | undo | reset
    tol: int = 24
    x: int = 0
    y: int = 0
    everywhere: bool = False


@router.post("/projects/{pid}/clean")
def clean(pid: str, b: CleanBody, authorization: Optional[str] = Header(None)):
    require_auth(authorization)
    d = _dir(pid)
    if b.op == "undo":
        hd = os.path.join(d, "history")
        hist = sorted(os.listdir(hd)) if os.path.isdir(hd) else []
        if not hist:
            raise HTTPException(400, "nothing to undo")
        shutil.move(os.path.join(hd, hist[-1]), os.path.join(d, "work.png"))
        if os.path.exists(os.path.join(d, "pixels.png")):
            os.remove(os.path.join(d, "pixels.png"))
        m = _meta(pid); m["slices"] = []; _save_meta(pid, m)
        return _view(pid)
    if b.op == "reset":
        _push_history(pid)
        for n in ("work.png", "pixels.png"):
            if os.path.exists(os.path.join(d, n)):
                os.remove(os.path.join(d, n))
        m = _meta(pid); m["slices"] = []; m["grid"] = None; m["stage"] = "new"; _save_meta(pid, m)
        return _view(pid)
    a = _work(pid)
    if b.op == "auto":
        out = remove_background(a, max(0, min(120, b.tol)))
    elif b.op == "color":
        if not (0 <= b.x < a.shape[1] and 0 <= b.y < a.shape[0]):
            raise HTTPException(400, "outside the picture")
        out = remove_color(a, b.x, b.y, max(0, min(120, b.tol)), b.everywhere)
    else:
        raise HTTPException(400, "unknown op")
    _set_work(pid, out)
    v = _view(pid)
    v["removed"] = int(((a[..., 3] > 0) & (out[..., 3] == 0)).sum())
    return v


@router.post("/projects/{pid}/grid/detect")
def grid_detect(pid: str, authorization: Optional[str] = Header(None)):
    """Find the pixel size with the Rust pixel-art fixer (boxed). Run Clean first: a background confuses it."""
    require_auth(authorization)
    from api.imagetools import _boxed_fix
    d = _dir(pid)
    src = os.path.join(d, "work.png" if os.path.exists(os.path.join(d, "work.png")) else "source.png")
    out = os.path.join(d, "detect_tmp.png")
    try:
        rc, info = _boxed_fix(src, out, "full")
    finally:
        if os.path.exists(out):
            os.remove(out)
    if rc != 0 or not info.get("step_x"):
        raise HTTPException(422, "couldn't find a pixel grid — set the pixel size by hand")
    g = {"step_x": float(info["step_x"]), "step_y": float(info["step_y"]), "off_x": 0.0, "off_y": 0.0,
         "cols": info.get("cols"), "rows": info.get("rows"), "how": info.get("consensus") or "", "detected": True}
    m = _meta(pid); m["grid"] = g; _save_meta(pid, m)
    return _view(pid)


class GridBody(BaseModel):
    step_x: float = 1
    step_y: float = 1
    off_x: float = 0
    off_y: float = 0
    colors: int = 0          # 0 = keep all
    as_is: bool = False      # the picture already is real pixel art


@router.post("/projects/{pid}/grid/rebuild")
def grid_rebuild(pid: str, b: GridBody, authorization: Optional[str] = Header(None)):
    require_auth(authorization)
    d = _dir(pid)
    a = _work(pid)
    if b.as_is:
        out = a.copy()
    else:
        if not (1 <= b.step_x <= 64 and 1 <= b.step_y <= 64):
            raise HTTPException(400, "pixel size must be between 1 and 64")
        out = rebuild(a, b.step_x, b.step_y, max(0.0, b.off_x), max(0.0, b.off_y))
    if b.colors and b.colors >= 2:
        out = reduce_colors(out, min(256, b.colors))
    _write(os.path.join(d, "pixels.png"), out)
    m = _meta(pid)
    m["grid"] = {**(m.get("grid") or {}), "step_x": b.step_x, "step_y": b.step_y, "off_x": b.off_x, "off_y": b.off_y,
                 "colors": b.colors, "as_is": b.as_is}
    m["slices"] = []; m["stage"] = "grid"; _save_meta(pid, m)
    v = _view(pid)
    v["color_count"] = int(len(np.unique(out[out[..., 3] > 0].reshape(-1, 4), axis=0)))
    return v


class AutoSlice(BaseModel):
    gap: int = 2
    min_area: int = 12


@router.post("/projects/{pid}/slices/auto")
def slices_auto(pid: str, b: AutoSlice, authorization: Optional[str] = Header(None)):
    require_auth(authorization)
    a = _load(os.path.join(_dir(pid), _current_name(pid)))
    boxes = find_sprites(a, max(0, min(32, b.gap)), max(1, b.min_area))
    m = _meta(pid)
    m["slices"] = [{"id": uuid.uuid4().hex[:8], "name": f"sprite {i + 1}", **bx} for i, bx in enumerate(boxes[:400])]
    m["stage"] = "slice"; _save_meta(pid, m)
    return _view(pid)


class SlicesBody(BaseModel):
    slices: list[dict]
    trim: bool = False


@router.put("/projects/{pid}/slices")
def slices_put(pid: str, b: SlicesBody, authorization: Optional[str] = Header(None)):
    """The app's edited boxes (moved, resized, renamed, added, deleted). trim=true shrinks each to its pixels."""
    require_auth(authorization)
    a = _load(os.path.join(_dir(pid), _current_name(pid)))
    h, w = a.shape[:2]
    clean_list = []
    for s in b.slices[:400]:
        bx = {"x": int(s.get("x", 0)), "y": int(s.get("y", 0)), "w": max(1, int(s.get("w", 1))), "h": max(1, int(s.get("h", 1)))}
        bx["x"] = max(0, min(w - 1, bx["x"])); bx["y"] = max(0, min(h - 1, bx["y"]))
        bx["w"] = min(bx["w"], w - bx["x"]); bx["h"] = min(bx["h"], h - bx["y"])
        if b.trim:
            bx = trim_box(a, bx)
        clean_list.append({"id": str(s.get("id") or uuid.uuid4().hex[:8])[:12], "name": str(s.get("name") or "sprite")[:40], **bx})
    m = _meta(pid); m["slices"] = clean_list
    ids = {s["id"] for s in clean_list}
    for an in m.get("anims") or []:
        an["frames"] = [f for f in an.get("frames") or [] if f in ids]
    _save_meta(pid, m)
    return _view(pid)


@router.get("/projects/{pid}/slice/{sid}.png")
def slice_png(pid: str, sid: str, scale_by: int = 1, authorization: Optional[str] = Header(None)):
    require_auth(authorization)
    m = _meta(pid)
    s = next((x for x in m.get("slices") or [] if x["id"] == sid), None)
    if not s:
        raise HTTPException(404, "no such sprite")
    a = _load(os.path.join(_dir(pid), _current_name(pid)))
    return Response(_png(scale(crop(a, s), max(1, min(32, scale_by)))), media_type="image/png", headers={"Cache-Control": "no-store"})


class AnimsBody(BaseModel):
    anims: list[dict]          # [{id, name, frames: [slice ids], fps}]


@router.put("/projects/{pid}/anims")
def anims_put(pid: str, b: AnimsBody, authorization: Optional[str] = Header(None)):
    require_auth(authorization)
    m = _meta(pid)
    ids = {s["id"] for s in m.get("slices") or []}
    m["anims"] = [{"id": str(a.get("id") or uuid.uuid4().hex[:8])[:12], "name": str(a.get("name") or "animation")[:40],
                   "frames": [f for f in a.get("frames") or [] if f in ids][:64],
                   "fps": max(1, min(30, int(a.get("fps") or 8)))} for a in b.anims[:40]]
    _save_meta(pid, m)
    return _view(pid)


def _rows(pid: str, anim: str) -> list[list[np.ndarray]]:
    m = _meta(pid)
    a = _load(os.path.join(_dir(pid), _current_name(pid)))
    by = {s["id"]: s for s in m.get("slices") or []}
    anims = m.get("anims") or []
    if anim == "all-sprites" or not anims:
        return [[crop(a, s) for s in m.get("slices") or []]]
    chosen = anims if anim == "all" else [x for x in anims if x["id"] == anim]
    return [[crop(a, by[f]) for f in x["frames"] if f in by] for x in chosen]


@router.get("/projects/{pid}/sheet.png")
def sheet_png(pid: str, anim: str = "all", scale_by: int = 1, pad: int = 0, authorization: Optional[str] = Header(None)):
    """Spritesheet: one row per animation (or every sprite in one row: anim=all-sprites)."""
    require_auth(authorization)
    out = scale(sheet(_rows(pid, anim), max(0, min(16, pad))), max(1, min(16, scale_by)))
    return Response(_png(out), media_type="image/png", headers={"Cache-Control": "no-store"})


@router.get("/projects/{pid}/export.zip")
def export_zip(pid: str, scale_by: int = 1, authorization: Optional[str] = Header(None)):
    """Everything: each sprite as PNG, one sheet per animation + all of them, and a JSON describing it."""
    require_auth(authorization)
    m = _meta(pid)
    f = max(1, min(16, scale_by))
    a = _load(os.path.join(_dir(pid), _current_name(pid)))
    bio = io.BytesIO()
    with zipfile.ZipFile(bio, "w", zipfile.ZIP_DEFLATED) as z:
        used = set()
        for s in m.get("slices") or []:
            base = "".join(ch if ch.isalnum() or ch in "-_ " else "_" for ch in s["name"]).strip() or s["id"]
            nm = base; k = 2
            while nm in used:
                nm = f"{base}_{k}"; k += 1
            used.add(nm)
            z.writestr(f"sprites/{nm}.png", _png(scale(crop(a, s), f)))
        for an in m.get("anims") or []:
            z.writestr(f"sheets/{an['name']}.png", _png(scale(sheet(_rows(pid, an["id"])), f)))
        if m.get("anims"):
            z.writestr("sheets/_all_animations.png", _png(scale(sheet(_rows(pid, "all")), f)))
        z.writestr("project.json", json.dumps({k: m.get(k) for k in ("name", "grid", "slices", "anims")} | {"scale": f}, indent=1))
    return Response(bio.getvalue(), media_type="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="{m["name"]}.zip"'})


@router.post("/projects/{pid}/grid/preview")
def grid_preview(pid: str, b: GridBody, authorization: Optional[str] = Header(None)):
    """Same as rebuild but nothing is saved: the PNG comes back for the app to show while sliders move."""
    require_auth(authorization)
    a = _work(pid)
    if not (1 <= b.step_x <= 64 and 1 <= b.step_y <= 64):
        raise HTTPException(400, "pixel size must be between 1 and 64")
    out = rebuild(a, b.step_x, b.step_y, max(0.0, b.off_x), max(0.0, b.off_y))
    if b.colors and b.colors >= 2:
        out = reduce_colors(out, min(256, b.colors))
    return Response(_png(out), media_type="image/png", headers={"Cache-Control": "no-store",
                    "X-Size": f"{out.shape[1]}x{out.shape[0]}"})
