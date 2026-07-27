"""
no-human.AudioPng — lightweight template host + FAL.ai image generation proxy.

Раньше это был отдельный самостоятельный FastAPI-app (server.py создавал свой app и
монтировал статику на "/"). Теперь это APIRouter, который unified_server.py включает
под префиксом "/audiopng" в общее FastAPI-приложение вместе с Creatives/Generate/History.
Вся бизнес-логика (image pipeline, outpaint) не менялась ни строкой.
"""
import os
import io
import uuid
import httpx
from pathlib import Path

from fastapi import APIRouter, UploadFile, File, HTTPException, Form
from PIL import Image, ImageDraw, ImageStat, ImageFilter
import aiofiles

BASE = Path(__file__).resolve().parent
TEMPLATES_DIR = BASE / "templates"
STATIC = BASE / "static"

for d in (TEMPLATES_DIR,):
    d.mkdir(parents=True, exist_ok=True)

FAL_KEY = os.environ.get("FAL_KEY", "")

router = APIRouter(prefix="/audiopng")

# ── Ad-creative pipeline: generate/upload a save-zone image, then outpaint the ──
# background top/bottom via Flux Fill to a compact 720x1280 (9:16) working canvas.
# All text/graphics from the save-zone stay inside the untouched center band, so
# nothing ends up in the newly-painted top/bottom strips.
#
# Flux Fill runs at this smaller 720-wide working resolution (not the final 1080x1920
# delivery size) for speed/cost — the frontend then applies a deterministic artificial
# upscale to 1080x1920 (see normalizeTo1080x1920 in static/index.html), reused via the
# same addImages()/finalizeEntry() path for BOTH flows below, so this resize happens
# exactly once, in one shared place, regardless of which flow produced the image.
OUTPAINT_W, OUTPAINT_H = 720, 1280

# Two save-zone shapes feed the same outpaint target above, each via _run_outpaint():
AIGEN_SAVEZONE_W, AIGEN_SAVEZONE_H = 720, 900     # ✨ AI Gen save-zone — 4:5
UPLOAD_SAVEZONE_W, UPLOAD_SAVEZONE_H = 720, 960   # 🖼️ Upload 3:4 save-zone — 3:4

FLUX_FILL_URL = "https://fal.run/fal-ai/flux-pro/v1/fill"

# Flux Fill has no negative_prompt field, so "don't add X" only works as a strong positive
# framing. Left unchecked, it tends to notice ad-layout content right at the mask seam (a bold
# headline/footer band touching the crop edge) and "continue the design" with more fabricated
# headlines/icons instead of plain background. Two mitigations: (1) ask the save-zone generator
# to leave a plain margin at the very top/bottom so the seam borders clean background, not text,
# and (2) frame the outpaint prompt as a photographic backdrop extension, not a design continuation.
SAVEZONE_MARGIN_PX = 100
SAVEZONE_MARGIN_INSTRUCTION = (
    f" Composition constraint: leave the outermost ~{SAVEZONE_MARGIN_PX}px strip along the very top "
    "and very bottom edges of the frame as plain, uncluttered background (no text, headline, logo, "
    "icon, or UI element touching those edges) — the image will be extended vertically afterward."
)

# NOTE: deliberately avoids ANY concrete noun — "text", "logo", "watermark", "icon", and even
# innocuous scene words like "wall", "surface", "room", "sky" have each independently been observed
# to make Flux render that literal thing (a fence/wall texture, fabricated captions, etc.), whether
# they were listed as forbidden or as a descriptive example. Flux is a strong content renderer and
# treats prompt nouns as generation targets almost regardless of framing. The only vocabulary safe to
# use is abstract, non-renderable qualities: color, tone, texture, blend, gradient, continuity.
OUTPAINT_PROMPT = (
    "A smooth, seamless, gradual continuation of the exact colors, tones and soft texture visible right "
    "at the border of this image, blending outward into the new space with no interruption — same "
    "palette, same softness, same gradual shading throughout. Do not repeat, mirror, or continue any "
    "nearby arrangement or repeating pattern — purely a smooth, uniform color and texture continuation, "
    "nothing structured or distinct from what is already there."
)

# Flux Fill is trained mostly on photographs — asking it to extend a FLAT/solid or gradient design
# background (as in a vector infographic or card) is out-of-distribution and can degrade to solid
# black instead of a continuation. When the source edges are already near-flat, a deterministic
# edge-stretch (see _make_edge_strip / _run_outpaint) is strictly better: free, instant, zero risk of
# hallucinated content, and pixel-perfect for a solid/gradient background. Only call the paid AI
# outpaint model when the edges actually carry photographic texture worth continuing intelligently.
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

# Residual safety net only — see _flux_fill_edge. The real fix is generation quality (focused
# crops + feathered mask below), not this check; it should rarely trigger once that's working.
DARKENING_RATIO_THRESHOLD = 0.5   # painted strip is "degenerate" if under half as bright as the source edge
DEGENERATE_ABS_MEAN_FLOOR = 12.0  # absolute floor — catches literal black even if the source itself is dark

# Save-zone generators: each returns a FAL request payload for the given prompt.
SAVEZONE_MODELS = {
    "nano-banana-2": {
        "name": "Nano Banana 2",
        "t2i": "https://fal.run/fal-ai/nano-banana-2",
        "payload": lambda prompt: {"prompt": prompt + SAVEZONE_MARGIN_INSTRUCTION, "aspect_ratio": "4:5", "resolution": "1K"},
    },
    "gpt-image-2": {
        "name": "GPT Image 2",
        "t2i": "https://fal.run/openai/gpt-image-2",
        # width/height must be multiples of 16; closest to 1080x1350 (4:5) — exact-cropped server-side after.
        "payload": lambda prompt: {"prompt": prompt + SAVEZONE_MARGIN_INSTRUCTION, "image_size": {"width": 1088, "height": 1360}, "quality": "high"},
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

EDGE_SOURCE_BAND_PX = 40  # sample a band, not a single pixel row
EDGE_BLUR_RADIUS = 40     # smooths busy edges (icons, objects) into a soft wash instead of hard "barcode" stripes
EDGE_DOWNSCALE_PX = 24    # collapse horizontally too before blowing back up — kills fine vertical banding

def _make_edge_strip(img: Image.Image, band_y0: int, height: int) -> Image.Image:
    """Soft, blurred stretch of a source edge band to fill `height` px above/below the image.
    Blurring (and horizontally downscaling) the band before collapsing it to one row avoids the
    hard vertical-stripe ('barcode') artifact that a literal 1px-row stretch produces on busy/
    detailed edges (icons, small objects, etc.)."""
    band_y0 = max(0, min(band_y0, img.height - EDGE_SOURCE_BAND_PX))
    band = img.crop((0, band_y0, img.width, band_y0 + EDGE_SOURCE_BAND_PX))
    band = band.filter(ImageFilter.GaussianBlur(radius=EDGE_BLUR_RADIUS))
    # Downscale-then-upscale horizontally acts as an extra low-pass filter on top of the blur,
    # so tightly-packed small objects (a row of pencils/icons) don't survive as visible bands.
    small = band.resize((EDGE_DOWNSCALE_PX, 1), Image.LANCZOS)
    row = small.resize((img.width, 1), Image.LANCZOS)
    return row.resize((img.width, height), Image.LANCZOS)

# ── Outpaint via two focused per-edge crops instead of one huge portrait canvas ──
# A 1080x1920 canvas with a 285px masked strip gives Flux very little effective resolution/
# attention on the region that actually needs generating, and pushes an unusually tall aspect
# ratio that Flux silently downscales internally (observed: it returned 800x1440 for a 1080x1920
# request), losing even more of the little detail it had. Splitting into two compact top/bottom
# crops — each a much more "normal" aspect ratio, each with a feathered (not hard-edged) mask —
# keeps the model in a comfortable regime and gives it real pixel density on the busy/detailed
# edges (small icons, objects) it was failing on. This is the actual generation-quality fix; the
# darkening check below is a thin residual safety net, not the fix itself.
# Context window (real, unmasked save-zone content given to Flux as an anchor) is kept
# proportional to expand_px, NOT a fixed pixel count: a wider window relative to the masked
# region gives Flux enough visual evidence to recognize "this is a repeating layout" (e.g. a
# card/window edge, an icon+caption row) and fabricate a continuation of it — a fake header,
# nav bar, logo, garbled text — instead of extending plain background. This ratio (roughly
# half the masked height) is what keeps that from happening; it must scale with expand_px,
# since a fixed context size silently drifts toward 1:1 (and the failure mode above) whenever
# expand_px shrinks (e.g. a smaller working resolution) — a bug we hit in practice.
OUTPAINT_CONTEXT_RATIO = 0.5
OUTPAINT_CONTEXT_MIN_PX = 48  # floor so a very small expand_px still carries a usable anchor
MASK_FEATHER_PX = 32          # soft gradient at the mask boundary instead of a hard cut

def _build_edge_crop(img: Image.Image, top: bool, expand_px: int) -> tuple[Image.Image, Image.Image]:
    """Small working canvas + feathered mask for outpainting ONE edge (top or bottom)."""
    w = img.width
    context_px = max(OUTPAINT_CONTEXT_MIN_PX, round(expand_px * OUTPAINT_CONTEXT_RATIO))
    context = img.crop((0, 0, w, context_px)) if top \
        else img.crop((0, img.height - context_px, w, img.height))
    placeholder = _make_edge_strip(img, 0 if top else img.height - EDGE_SOURCE_BAND_PX, expand_px)
    crop_h = expand_px + context_px

    canvas = Image.new("RGB", (w, crop_h))
    mask = Image.new("L", (w, crop_h), 0)
    draw = ImageDraw.Draw(mask)
    if top:
        canvas.paste(placeholder, (0, 0))
        canvas.paste(context, (0, expand_px))
        draw.rectangle([0, 0, w, expand_px], fill=255)
    else:
        canvas.paste(context, (0, 0))
        canvas.paste(placeholder, (0, context_px))
        draw.rectangle([0, context_px, w, crop_h], fill=255)

    mask = mask.filter(ImageFilter.GaussianBlur(radius=MASK_FEATHER_PX))
    return canvas, mask

async def _flux_fill_edge(client: httpx.AsyncClient, img: Image.Image, top: bool, source_mean: float, expand_px: int) -> tuple[Image.Image, str]:
    """Outpaint ONE edge via Flux Fill on a focused crop. Returns (painted expand_px-tall strip, method)."""
    label = "top" if top else "bottom"
    fallback_strip = _make_edge_strip(img, 0 if top else img.height - EDGE_SOURCE_BAND_PX, expand_px)

    canvas, mask = _build_edge_crop(img, top, expand_px)
    canvas_buf = io.BytesIO(); canvas.save(canvas_buf, format="PNG")
    mask_buf = io.BytesIO(); mask.convert("RGB").save(mask_buf, format="PNG")
    canvas_url = await _fal_storage_upload(client, canvas_buf.getvalue(), "image/png", "canvas.png")
    mask_url = await _fal_storage_upload(client, mask_buf.getvalue(), "image/png", "mask.png")

    fill_resp = await client.post(
        FLUX_FILL_URL,
        json={
            "image_url": canvas_url,
            "mask_url": mask_url,
            "prompt": OUTPAINT_PROMPT,
            "num_images": 1,
            "output_format": "png",
            "enhance_prompt": True,
        },
        headers={"Authorization": f"Key {FAL_KEY}"},
    )
    if fill_resp.status_code != 200:
        print(f"[outpaint] {label} flux-fill call failed ({fill_resp.status_code}): {fill_resp.text}", flush=True)
        return fallback_strip, "stretch-fallback"

    result_url = _extract_image_url(fill_resp.json())
    if not result_url:
        print(f"[outpaint] {label} flux-fill returned no image URL", flush=True)
        return fallback_strip, "stretch-fallback"

    result_resp = await client.get(result_url)
    if result_resp.status_code != 200:
        return fallback_strip, "stretch-fallback"

    result_img = Image.open(io.BytesIO(result_resp.content)).convert("RGB")
    if result_img.size != canvas.size:
        result_img = result_img.resize(canvas.size, Image.LANCZOS)  # Flux can snap to its own internal grid
    strip = result_img.crop((0, 0, canvas.width, expand_px)) if top \
        else result_img.crop((0, canvas.height - expand_px, canvas.width, canvas.height))

    stat = ImageStat.Stat(strip)
    mean = sum(stat.mean) / len(stat.mean)
    ratio = mean / source_mean if source_mean > 0 else 1.0
    print(f"[outpaint] {label} flux-fill strip mean={mean:.2f} (source edge mean={source_mean:.2f}, ratio={ratio:.2f})", flush=True)
    if mean < DEGENERATE_ABS_MEAN_FLOOR or ratio < DARKENING_RATIO_THRESHOLD:
        print(f"[outpaint] {label} strip still looked degenerate — falling back to edge-stretch for this edge only", flush=True)
        return fallback_strip, "stretch-fallback"

    return strip, "flux-fill"

async def _run_outpaint(client: httpx.AsyncClient, savezone_bytes: bytes, savezone_w: int, savezone_h: int) -> tuple[str, str]:
    """Given a save-zone creative (any size — center-fit to savezone_w x savezone_h), extend it
    top/bottom via Flux Fill to the shared OUTPAINT_W x OUTPAINT_H working canvas. Returns
    (final_url, method); the caller's frontend applies the artificial upscale to the final
    1080x1920 delivery size afterward (same shared step for every flow — see normalizeTo1080x1920
    in static/index.html)."""
    from PIL import ImageOps

    img = Image.open(io.BytesIO(savezone_bytes)).convert("RGB")
    img = ImageOps.fit(img, (savezone_w, savezone_h), Image.LANCZOS)
    expand_px = (OUTPAINT_H - savezone_h) // 2

    top_mean, top_stdev = _band_stats(img, 0)
    bottom_mean, bottom_stdev = _band_stats(img, savezone_h - 1)
    is_flat = top_stdev < FLAT_EDGE_STDEV_THRESHOLD and bottom_stdev < FLAT_EDGE_STDEV_THRESHOLD
    print(f"[outpaint] {savezone_w}x{savezone_h} source edge top mean={top_mean:.2f} stdev={top_stdev:.2f} | "
          f"bottom mean={bottom_mean:.2f} stdev={bottom_stdev:.2f} | flat={is_flat}", flush=True)

    if is_flat:
        # Solid/gradient design background: the deterministic edge-stretch already IS the
        # correct continuation. Skip the paid AI call — free, instant, no hallucination risk.
        top_strip = _make_edge_strip(img, 0, expand_px)
        bottom_strip = _make_edge_strip(img, savezone_h - EDGE_SOURCE_BAND_PX, expand_px)
        method = "flat-stretch"
    else:
        top_strip, top_method = await _flux_fill_edge(client, img, True, top_mean, expand_px)
        bottom_strip, bottom_method = await _flux_fill_edge(client, img, False, bottom_mean, expand_px)
        method = top_method if top_method == bottom_method else f"top={top_method},bottom={bottom_method}"

    final_canvas = Image.new("RGB", (OUTPAINT_W, OUTPAINT_H))
    final_canvas.paste(top_strip, (0, 0))
    final_canvas.paste(img, (0, expand_px))
    final_canvas.paste(bottom_strip, (0, expand_px + savezone_h))
    final_buf = io.BytesIO(); final_canvas.save(final_buf, format="PNG")
    final_url = await _fal_storage_upload(client, final_buf.getvalue(), "image/png", "final.png")
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

@router.post("/api/generate-creative")
async def generate_creative(
    prompt: str = Form(...),
    model: str = Form("nano-banana-2"),
):
    """Generate a text-safe 720x900 (4:5) creative, then outpaint the background top/bottom
    to the shared 720x1280 working canvas via Flux Fill. Returns {url, savezone_url}."""
    _require_fal_key()
    if model not in SAVEZONE_MODELS:
        raise HTTPException(400, f"Unknown model: {model}")
    cfg = SAVEZONE_MODELS[model]
    payload = cfg["payload"](prompt)
    print(f"[FAL creative] model={model} prompt={prompt!r}", flush=True)

    async with httpx.AsyncClient(timeout=180) as client:
        resp = await client.post(cfg["t2i"], json=payload, headers={"Authorization": f"Key {FAL_KEY}"})
        if resp.status_code != 200:
            raise HTTPException(resp.status_code, detail=f"Save-zone generation failed: {resp.text}")
        savezone_url = _extract_image_url(resp.json())
        if not savezone_url:
            raise HTTPException(500, "No image URL from save-zone model")

        img_resp = await client.get(savezone_url)
        if img_resp.status_code != 200:
            raise HTTPException(500, "Failed to download save-zone image")

        final_url, method = await _run_outpaint(client, img_resp.content, AIGEN_SAVEZONE_W, AIGEN_SAVEZONE_H)

    return {"url": final_url, "savezone_url": savezone_url, "outpaint_method": method}

SAVEZONE_SHAPES = {
    "upload": (UPLOAD_SAVEZONE_W, UPLOAD_SAVEZONE_H),   # 🖼️ Upload 3:4 — 3:4 save-zone
    "aigen": (AIGEN_SAVEZONE_W, AIGEN_SAVEZONE_H),      # ✨ AI Gen retry — 4:5 save-zone
}

@router.post("/api/outpaint-upload")
async def outpaint_upload(file: UploadFile = File(...), savezone: str = Form("upload")):
    """Take an uploaded save-zone creative and outpaint its background top/bottom to the shared
    720x1280 working canvas — same _run_outpaint() pipeline as generate-creative, minus the
    text-to-image step. `savezone` selects the input shape: "upload" (3:4, the normal Upload
    3:4 flow) or "aigen" (4:5, used when retrying just the outpaint step on an AI Gen save-zone
    without re-running the paid generation). Returns {url, outpaint_method}."""
    _require_fal_key()
    if savezone not in SAVEZONE_SHAPES:
        raise HTTPException(400, f"Unknown savezone: {savezone}")
    savezone_w, savezone_h = SAVEZONE_SHAPES[savezone]
    content = await file.read()
    async with httpx.AsyncClient(timeout=180) as client:
        final_url, method = await _run_outpaint(client, content, savezone_w, savezone_h)
    return {"url": final_url, "outpaint_method": method}

# Статика (templates/, static/index.html+ffmpeg) монтируется в unified_server.py
# под префиксом /audiopng — здесь только APIRouter с /audiopng/api/*.