"""
no-human.AudioPng — lightweight template host + FAL.ai image generation proxy.

Раньше это был отдельный самостоятельный FastAPI-app (server.py создавал свой app и
монтировал статику на "/"). Теперь это APIRouter, который unified_server.py включает
под префиксом "/audiopng" в общее FastAPI-приложение вместе с Creatives/Generate/History.
Вся бизнес-логика (image pipeline, outpaint) не менялась ни строкой.
"""
import asyncio
import os
import io
import uuid
import httpx
from pathlib import Path

from fastapi import APIRouter, UploadFile, File, HTTPException, Form
from PIL import Image, ImageStat
import aiofiles

BASE = Path(__file__).resolve().parent
TEMPLATES_DIR = BASE / "templates"
STATIC = BASE / "static"

for d in (TEMPLATES_DIR,):
    d.mkdir(parents=True, exist_ok=True)

FAL_KEY = os.environ.get("FAL_KEY", "")

router = APIRouter(prefix="/audiopng")

# ── Background jobs for slow FAL calls ──────────────────────────────────────────
# GPT Image 2 generations have been observed taking 2-3 minutes end to end (t2i + outpaint).
# Cloudflare's edge (both quick tunnels and the named tunnel we run in production) closes a
# single proxied HTTP request around ~100s and returns its own HTML error page instead — which
# breaks the frontend's res.json() with "Unexpected token '<' ... is not valid JSON", since it's
# parsing an HTML page, not our JSON. Fix: never let a single request run that long. The route
# handler starts the real work as a background asyncio task and returns a job_id immediately;
# the frontend polls /api/job/{job_id} every couple seconds instead — each individual HTTP
# request stays well under any proxy's timeout, no matter how long the total job takes. Reused
# by Competitors Static (competitors/api.py) for the same reason on its own /api/generate.
_JOBS: dict[str, dict] = {}

def _start_async_job(coro) -> str:
    job_id = uuid.uuid4().hex
    _JOBS[job_id] = {"done": False, "error": None, "result": None}

    async def runner():
        try:
            _JOBS[job_id]["result"] = await coro
        except HTTPException as e:
            _JOBS[job_id]["error"] = str(e.detail)
        except Exception as e:
            _JOBS[job_id]["error"] = str(e)
        finally:
            _JOBS[job_id]["done"] = True

    asyncio.create_task(runner())
    return job_id

def _get_job(job_id: str) -> dict | None:
    return _JOBS.get(job_id)

@router.get("/api/job/{job_id}")
async def api_job(job_id: str):
    job = _get_job(job_id)
    if job is None:
        raise HTTPException(404, "Job not found")
    return job

# ── Ad-creative pipeline: generate/upload a save-zone image, then outpaint top/bottom
# to the final delivery-resolution 1080x1920 (9:16) canvas directly — no separate smaller
# working canvas + client-side upscale step anymore, the server now delivers Full HD
# vertical straight away. All text/graphics from the save-zone stay inside the untouched
# center band, so nothing ends up in the newly-painted top/bottom strips.
OUTPAINT_W, OUTPAINT_H = 1080, 1920

# Two save-zone shapes feed the same outpaint target above, each via _run_outpaint() — both
# 4:5, scaled up from the old smaller working resolution (720x900 / 720x960) to the new
# 1080-wide delivery resolution, same ratios:
AIGEN_SAVEZONE_W, AIGEN_SAVEZONE_H = 1080, 1350   # ✨ AI Gen save-zone — 4:5
UPLOAD_SAVEZONE_W, UPLOAD_SAVEZONE_H = 1080, 1350  # 🖼️ Upload save-zone — 4:5

# Purpose-built outpaint model: whole image in, just say how many px to add top/bottom — no
# mask, no prompt, one call for both edges at once. Replaced the old flux-pro/v1/fill pipeline
# (masked per-edge crops + a hand-tuned "don't invent content" prompt) because this one simply
# doesn't invent content in the first place — no prompt to get wrong, no mask math to tune.
FLUX2_OUTPAINT_URL = "https://fal.run/fal-ai/flux-2-pro/outpaint"
FLUX2_OUTPAINT_MODE = "fast"

# Photographic/textured edges go through flux-2-pro/outpaint above; flat/solid/gradient design
# backgrounds (vector infographics, cards) are cheaper and safer to extend with a deterministic
# stretch — free, instant, zero risk of hallucinated content, pixel-perfect on a flat source.
FLAT_EDGE_STDEV_THRESHOLD = 8.0
EDGE_BAND_PX = 10  # sample a small band, not a single pixel row — one stray icon/star shouldn't flip the verdict

def _band_stats(img: Image.Image, y_center: int, band: int = EDGE_BAND_PX) -> tuple[float, float]:
    """Returns (mean_brightness, stdev) for a horizontal band centered at y_center."""
    y0 = max(0, y_center - band // 2)
    y1 = min(img.height, y0 + band)
    region = img.crop((0, y0, img.width, y1))
    stat = ImageStat.Stat(region)
    mean = sum(stat.mean) / len(stat.mean)
    stddev = sum(stat.stddev) / len(stat.stddev)
    return mean, stddev

# Save-zone generators: each returns a FAL request payload for the given prompt. Prompt is
# passed through exactly as typed — no appended instructions (a "leave a plain margin" hint
# here didn't measurably change what the models produced; flux-2-pro/outpaint's expand_top/
# expand_bottom approach doesn't need a clean margin the way the old masked-fill approach did).
SAVEZONE_MODELS = {
    "nano-banana-2": {
        "name": "Nano Banana 2",
        "t2i": "https://fal.run/fal-ai/nano-banana-2",
        "payload": lambda prompt: {"prompt": prompt, "aspect_ratio": "4:5", "resolution": "1K"},
    },
    "gpt-image-2": {
        "name": "GPT Image 2",
        "t2i": "https://fal.run/openai/gpt-image-2",
        # width/height must be multiples of 16; closest to 1080x1350 (4:5) — exact-resized server-side after.
        "payload": lambda prompt: {"prompt": prompt, "image_size": {"width": 1088, "height": 1360}, "quality": "high"},
    },
}

_templates: list[dict] = []

def _load_templates():
    _templates.clear()
    for f in sorted(TEMPLATES_DIR.glob("*")):
        if f.is_file() and f.suffix.lower() in (".png", ".webp"):
            _templates.append({"id": f.stem, "name": f.name, "path": str(f)})

_load_templates()

# ── API: templates (global, persist on disk) ────────────────
@router.get("/api/templates")
async def list_templates():
    _load_templates()
    return [{"id": t["id"], "name": t["name"]} for t in _templates]

@router.post("/api/templates")
async def upload_template(file: UploadFile = File(...)):
    if not file.filename:
        raise HTTPException(400, "No file")
    ext = Path(file.filename).suffix.lower()
    if ext not in (".png", ".webp"):
        raise HTTPException(400, "Only PNG/WebP supported")
    tid = uuid.uuid4().hex[:8]
    name = f"{tid}{ext}"
    path = TEMPLATES_DIR / name
    content = await file.read()
    async with aiofiles.open(path, "wb") as out:
        await out.write(content)
    _templates.append({"id": tid, "name": file.filename, "path": str(path)})
    return {"id": tid, "name": file.filename}

@router.delete("/api/templates/{tid}")
async def delete_template(tid: str):
    for i, t in enumerate(_templates):
        if t["id"] == tid:
            try:
                path = Path(t["path"])
                if path.exists():
                    path.unlink()
            except OSError:
                pass
            _templates.pop(i)
            return {"ok": True}
    raise HTTPException(404, "Template not found")

def _require_fal_key() -> None:
    if not FAL_KEY:
        raise HTTPException(500, "FAL_KEY не задан (проверьте .env) — без него FAL.ai вернёт ошибку авторизации")

async def _fal_storage_upload(client: httpx.AsyncClient, content: bytes, content_type: str, filename: str) -> str:
    """Upload raw bytes to FAL Storage and return the public fal.media URL."""
    init_resp = await client.post(
        "https://rest.fal.ai/storage/upload/initiate",
        headers={"Authorization": f"Key {FAL_KEY}", "Content-Type": "application/json"},
        json={"content_type": content_type, "file_name": filename},
    )
    if init_resp.status_code != 200:
        raise HTTPException(500, f"FAL init failed: {init_resp.text}")
    init_data = init_resp.json()
    upload_url = init_data["upload_url"]
    file_url = init_data["file_url"]

    put_resp = await client.put(upload_url, headers={"Content-Type": content_type}, content=content)
    if put_resp.status_code not in (200, 201):
        raise HTTPException(500, f"FAL upload failed: {put_resp.text}")
    return file_url

def _extract_image_url(data: dict) -> str | None:
    if "image" in data and isinstance(data["image"], dict):
        return data["image"].get("url")
    if "images" in data and data["images"]:
        return data["images"][0].get("url")
    return None

EDGE_SOURCE_BAND_PX = 3  # "растягиваем первые 3 пикселя по краям" — thin strip, no blur needed

def _make_edge_strip(img: Image.Image, band_y0: int, height: int) -> Image.Image:
    """Stretch the outermost EDGE_SOURCE_BAND_PX rows to fill `height` px above/below the image —
    the free/deterministic path for flat/solid/gradient backgrounds (see _run_outpaint)."""
    band_y0 = max(0, min(band_y0, img.height - EDGE_SOURCE_BAND_PX))
    band = img.crop((0, band_y0, img.width, band_y0 + EDGE_SOURCE_BAND_PX))
    return band.resize((img.width, height), Image.LANCZOS)

async def _run_outpaint(client: httpx.AsyncClient, savezone_bytes: bytes, savezone_w: int, savezone_h: int,
                         target_w: int, target_h: int) -> tuple[str, str]:
    """Given a save-zone creative (any size — stretched, not cropped, to savezone_w x savezone_h),
    extend it top/bottom to target_w x target_h (same width as the save-zone; only height grows).
    Flat/solid/gradient edges get a free deterministic stretch; photographic/textured edges go
    through flux-2-pro/outpaint (whole image, no mask/prompt, one call for both edges). Returns
    (final_url, method). target_w/target_h are explicit params (not read from module-level
    constants) so callers — audiopng's own routes AND Competitors Static, which reuses this same
    function with its own different save-zone/target shape — can't accidentally affect each other
    if one side's constants change."""
    img = Image.open(io.BytesIO(savezone_bytes)).convert("RGB")
    img = img.resize((savezone_w, savezone_h), Image.LANCZOS)  # stretch/squash to fit exactly, no crop
    expand_px = (target_h - savezone_h) // 2

    top_mean, top_stdev = _band_stats(img, 0)
    bottom_mean, bottom_stdev = _band_stats(img, savezone_h - 1)
    is_flat = top_stdev < FLAT_EDGE_STDEV_THRESHOLD and bottom_stdev < FLAT_EDGE_STDEV_THRESHOLD
    print(f"[outpaint] {savezone_w}x{savezone_h} source edge top mean={top_mean:.2f} stdev={top_stdev:.2f} | "
          f"bottom mean={bottom_mean:.2f} stdev={bottom_stdev:.2f} | flat={is_flat}", flush=True)

    if not is_flat:
        img_buf = io.BytesIO(); img.save(img_buf, format="PNG")
        img_url = await _fal_storage_upload(client, img_buf.getvalue(), "image/png", "savezone.png")
        resp = await client.post(
            FLUX2_OUTPAINT_URL,
            json={
                "image_url": img_url,
                "expand_top": expand_px,
                "expand_bottom": expand_px,
                "mode": FLUX2_OUTPAINT_MODE,
                "output_format": "png",
            },
            headers={"Authorization": f"Key {FAL_KEY}"},
        )
        if resp.status_code == 200:
            result_url = _extract_image_url(resp.json())
            if result_url:
                return result_url, "flux2-outpaint"
            print("[outpaint] flux-2-pro returned no image URL — falling back to edge-stretch", flush=True)
        else:
            print(f"[outpaint] flux-2-pro call failed ({resp.status_code}): {resp.text} — falling back to edge-stretch", flush=True)

    # Flat background, OR flux-2-pro call failed — free deterministic edge-stretch.
    top_strip = _make_edge_strip(img, 0, expand_px)
    bottom_strip = _make_edge_strip(img, savezone_h - EDGE_SOURCE_BAND_PX, expand_px)
    final_canvas = Image.new("RGB", (target_w, target_h))
    final_canvas.paste(top_strip, (0, 0))
    final_canvas.paste(img, (0, expand_px))
    final_canvas.paste(bottom_strip, (0, expand_px + savezone_h))
    final_buf = io.BytesIO(); final_canvas.save(final_buf, format="PNG")
    final_url = await _fal_storage_upload(client, final_buf.getvalue(), "image/png", "final.png")
    method = "flat-stretch" if is_flat else "stretch-fallback"
    return final_url, method

# ── API: upload reference image to FAL Storage ─────────────
@router.post("/api/upload-ref")
async def upload_ref_image(file: UploadFile = File(...)):
    """Upload image to FAL Storage and return a public URL on fal.media."""
    _require_fal_key()
    content = await file.read()
    ct = file.content_type or "image/png"
    fname = file.filename or "ref.png"
    async with httpx.AsyncClient(timeout=60) as client:
        file_url = await _fal_storage_upload(client, content, ct, fname)
    return {"url": file_url}

# ── API: ad-creative pipeline (generate save-zone, then outpaint to 9:16) ──
@router.get("/api/creative-models")
async def list_creative_models():
    return [{"id": k, "name": v["name"]} for k, v in SAVEZONE_MODELS.items()]

async def _do_generate_creative(prompt: str, model: str) -> dict:
    """The actual (slow) work — see _start_async_job above for why this runs in the background
    instead of directly in the request handler."""
    if model not in SAVEZONE_MODELS:
        raise HTTPException(400, f"Unknown model: {model}")
    cfg = SAVEZONE_MODELS[model]
    payload = cfg["payload"](prompt)
    print(f"[FAL creative] model={model} prompt={prompt!r}", flush=True)

    async with httpx.AsyncClient(timeout=280) as client:
        resp = await client.post(cfg["t2i"], json=payload, headers={"Authorization": f"Key {FAL_KEY}"})
        if resp.status_code != 200:
            raise HTTPException(resp.status_code, detail=f"Save-zone generation failed: {resp.text}")
        savezone_url = _extract_image_url(resp.json())
        if not savezone_url:
            raise HTTPException(500, "No image URL from save-zone model")

        img_resp = await client.get(savezone_url)
        if img_resp.status_code != 200:
            raise HTTPException(500, "Failed to download save-zone image")

        final_url, method = await _run_outpaint(client, img_resp.content, AIGEN_SAVEZONE_W, AIGEN_SAVEZONE_H, OUTPAINT_W, OUTPAINT_H)

    return {"url": final_url, "savezone_url": savezone_url, "outpaint_method": method}

@router.post("/api/generate-creative")
async def generate_creative(
    prompt: str = Form(...),
    model: str = Form("nano-banana-2"),
):
    """Generate a text-safe 1080x1350 (4:5) creative, then outpaint the background top/bottom
    to the final 1080x1920 delivery canvas. Kicks off the work in the background and returns a
    job_id immediately (see _start_async_job) — poll GET /api/job/{job_id} for {url, savezone_url,
    outpaint_method} once done."""
    _require_fal_key()
    job_id = _start_async_job(_do_generate_creative(prompt, model))
    return {"job_id": job_id}

SAVEZONE_SHAPES = {
    "upload": (UPLOAD_SAVEZONE_W, UPLOAD_SAVEZONE_H),   # 🖼️ Upload save-zone — 4:5
    "aigen": (AIGEN_SAVEZONE_W, AIGEN_SAVEZONE_H),      # ✨ AI Gen retry — 4:5 save-zone
}

@router.post("/api/outpaint-upload")
async def outpaint_upload(file: UploadFile = File(...), savezone: str = Form("upload")):
    """Take an uploaded save-zone creative and outpaint its background top/bottom to the final
    1080x1920 delivery canvas — same _run_outpaint() pipeline as generate-creative, minus the
    text-to-image step. `savezone` selects the input shape: "upload" (the normal Upload flow) or
    "aigen" (used when retrying just the outpaint step on an AI Gen save-zone without re-running
    the paid generation) — both 4:5 today, kept as separate named shapes in case they diverge
    again. Returns {url, outpaint_method}."""
    _require_fal_key()
    if savezone not in SAVEZONE_SHAPES:
        raise HTTPException(400, f"Unknown savezone: {savezone}")
    savezone_w, savezone_h = SAVEZONE_SHAPES[savezone]
    content = await file.read()
    async with httpx.AsyncClient(timeout=180) as client:
        final_url, method = await _run_outpaint(client, content, savezone_w, savezone_h, OUTPAINT_W, OUTPAINT_H)
    return {"url": final_url, "outpaint_method": method}

# Статика (templates/, static/index.html+ffmpeg) монтируется в unified_server.py
# под префиксом /audiopng — здесь только APIRouter с /audiopng/api/*.