"""
Competitors Static — новый раздел: берёт текст из "нашего креатива" (vision-модель
вытаскивает literal on-image text) и генерирует новые рекламные креативы, по одному
на каждый загруженный "референс конкурента" — стиль/визуал берётся из референса,
текст всегда наш собственный.

Переиспользует существующий FAL-инфраструктурный код из audiopng/server.py (upload в
FAL Storage, аутпеинт 3:4 -> 9:16 через flux-2-pro/outpaint) вместо дублирования — тот
же _run_outpaint(), тот же UPLOAD_SAVEZONE_W/H шейп (3:4), тот же OUTPAINT_W/H (720x1280)
целевой холст. Финальный апскейл до 1080x1920 — на фронтенде, тем же normalizeTo1080x1920,
что и в audiopng (см. static/index.html).
"""
import base64
import os
from pathlib import Path

import httpx
from fastapi import APIRouter, Form, HTTPException, UploadFile, File

import server as audiopng  # audiopng/server.py — уже импортирован в review_server.py раньше,
# полагаемся на кэш sys.modules['server']; но добавляем свой sys.path.insert выше по цепочке
# (см. review_server.py) на случай прямого запуска/иного порядка импорта.

BASE = Path(__file__).resolve().parent
STATIC = BASE / "static"

router = APIRouter(prefix="/competitors")

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
VISION_MODEL = "anthropic/claude-haiku-4.5"

def _require_openrouter_key() -> str:
    key = os.environ.get("OPENROUTER_API_KEY")
    if not key:
        raise HTTPException(500, "OPENROUTER_API_KEY не задан (проверьте .env)")
    return key

EXTRACT_TEXT_PROMPT = (
    "Extract every piece of literal text visible in this image, exactly as written (same "
    "wording, same capitalization, same punctuation), in natural visual reading order — "
    "headline first, then subheadline, body copy, labels, button/CTA text, etc. Separate "
    "distinct text elements with a newline each. Reply with ONLY the extracted text itself: "
    "no description of the image, no commentary, no markdown formatting, no quotation marks "
    "around it. If the image contains no readable text, reply with an empty string."
)

async def extract_text_from_image(image_bytes: bytes, content_type: str) -> str:
    api_key = _require_openrouter_key()
    b64 = base64.b64encode(image_bytes).decode()
    data_url = f"data:{content_type};base64,{b64}"
    async with httpx.AsyncClient(timeout=90) as client:
        resp = await client.post(
            OPENROUTER_URL,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={
                "model": VISION_MODEL,
                "temperature": 0,
                "messages": [{
                    "role": "user",
                    "content": [
                        {"type": "text", "text": EXTRACT_TEXT_PROMPT},
                        {"type": "image_url", "image_url": {"url": data_url}},
                    ],
                }],
            },
        )
    if resp.status_code != 200:
        raise HTTPException(resp.status_code, detail=f"Vision extraction failed: {resp.text}")
    return resp.json()["choices"][0]["message"]["content"].strip()

@router.post("/api/extract-text")
async def api_extract_text(file: UploadFile = File(...)):
    content = await file.read()
    text = await extract_text_from_image(content, file.content_type or "image/png")
    return {"text": text}

# Generation models: each is a FAL "edit" endpoint that takes prompt + one or more reference
# image_urls (image-to-image / style-conditioned generation) — NOT the plain text-to-image
# endpoints audiopng's AI Gen uses, since here the reference image's visual style is a required
# input, not just a text description of it.
GENERATION_MODELS = {
    "nano-banana-2": {
        "name": "Nano Banana 2",
        "edit_url": "https://fal.run/fal-ai/nano-banana-2/edit",
        "payload_extra": {"aspect_ratio": "3:4", "resolution": "1K"},
    },
    "nano-banana-pro": {
        "name": "Nano Banana Pro",
        "edit_url": "https://fal.run/fal-ai/nano-banana-pro/edit",
        "payload_extra": {"aspect_ratio": "3:4", "resolution": "1K"},
    },
    "gpt-image-2": {
        "name": "GPT Image 2",
        "edit_url": "https://fal.run/openai/gpt-image-2/edit",
        # gpt-image-2/edit has no aspect_ratio enum — closest 3:4 via explicit width/height
        # (multiples of 16), matching the pattern audiopng's own gpt-image-2 payload already uses.
        "payload_extra": {"image_size": {"width": 864, "height": 1152}, "quality": "high"},
    },
}

@router.get("/api/generation-models")
async def api_generation_models():
    return [{"id": k, "name": v["name"]} for k, v in GENERATION_MODELS.items()]

# Deliberately DOES tell the model what to leave out (brand mark, disclaimer) — unlike the
# outpaint prompt elsewhere in this app, which avoids naming anything to exclude (Flux Fill reacts
# badly to negation). Tested directly against all three models here: the instruction below reliably
# keeps the source reference's own brand mark out of the result, and these image-edit models don't
# show the same "renders whatever it's told not to" failure mode Flux Fill does.
GEN_PROMPT_TEMPLATE = (
    "Generate a vertical advertising creative that uses the following on-image text exactly as "
    "given, sized and placed to match the reference's typographic hierarchy:\n\n\"{text}\"\n\n"
    "Adopt the visual style of the attached reference image — its color palette, background "
    "texture, imagery treatment, and marketing layout conventions (badges, icons, step markers, "
    "connector lines, CTA button styling, card or grid structure). Re-interpret every visual "
    "element to fit and reinforce the meaning of the given text; do not carry over any of the "
    "reference's own wording, numbers, or claims — only its visual system. Keep the reference's "
    "overall composition and information hierarchy where it fits the new text naturally, adapting "
    "proportions rather than forcing a literal copy. Do not include any logo, wordmark, or brand "
    "mark from the reference image, and do not add a legal disclaimer or fine print. Vertical "
    "orientation, 3:4 aspect ratio."
)

@router.post("/api/generate")
async def api_generate(
    text: str = Form(...),
    model: str = Form(...),
    file: UploadFile = File(...),
):
    """One call = one reference image = one generated creative. Frontend calls this once per
    competitor reference. Pipeline: upload reference -> call the chosen edit model (text + 1
    reference image) -> 3:4 result -> outpaint (reusing audiopng._run_outpaint) -> 9:16 result."""
    audiopng._require_fal_key()
    if model not in GENERATION_MODELS:
        raise HTTPException(400, f"Unknown model: {model}")
    cfg = GENERATION_MODELS[model]

    ref_bytes = await file.read()
    ref_content_type = file.content_type or "image/png"

    async with httpx.AsyncClient(timeout=280) as client:
        ref_url = await audiopng._fal_storage_upload(client, ref_bytes, ref_content_type, file.filename or "reference.png")

        prompt = GEN_PROMPT_TEMPLATE.format(text=text.strip())
        payload = {"prompt": prompt, "image_urls": [ref_url], **cfg["payload_extra"]}
        resp = await client.post(cfg["edit_url"], json=payload, headers={"Authorization": f"Key {audiopng.FAL_KEY}"})
        if resp.status_code != 200:
            raise HTTPException(resp.status_code, detail=f"Generation failed: {resp.text}")
        savezone_url = audiopng._extract_image_url(resp.json())
        if not savezone_url:
            raise HTTPException(500, "No image URL from generation model")

        img_resp = await client.get(savezone_url)
        if img_resp.status_code != 200:
            raise HTTPException(500, "Failed to download generated image")

        final_url, method = await audiopng._run_outpaint(
            client, img_resp.content, audiopng.UPLOAD_SAVEZONE_W, audiopng.UPLOAD_SAVEZONE_H
        )

    return {"url": final_url, "savezone_url": savezone_url, "outpaint_method": method}

@router.post("/api/outpaint-only")
async def api_outpaint_only(file: UploadFile = File(...)):
    """Re-run just the outpaint step on an already-generated 3:4 save-zone (the frontend re-fetches
    savezone_url from a previous /api/generate call and posts it here) — skips the paid generation-
    model call entirely, for when only the outpaint result needs a retry."""
    audiopng._require_fal_key()
    content = await file.read()
    async with httpx.AsyncClient(timeout=280) as client:
        final_url, method = await audiopng._run_outpaint(
            client, content, audiopng.UPLOAD_SAVEZONE_W, audiopng.UPLOAD_SAVEZONE_H
        )
    return {"url": final_url, "outpaint_method": method}
