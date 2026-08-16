"""
Competitors Static — новый раздел: берёт текст из "нашего креатива" (vision-модель
вытаскивает literal on-image text) и генерирует новые рекламные креативы, по одному
на каждый загруженный "референс конкурента" — стиль/визуал берётся из референса,
текст всегда наш собственный.

Переиспользует существующий FAL-инфраструктурный код из audiopng/server.py (upload в
FAL Storage, аутпеинт через flux-2-pro/outpaint) вместо дублирования — та же _run_outpaint().
Save-zone/target шейп при этом СВОЙ (см. COMP_SAVEZONE_*/COMP_OUTPAINT_* ниже), не общий с
audiopng.UPLOAD_SAVEZONE_W/H/OUTPAINT_W/H — специально не читает их напрямую, чтобы будущие
изменения этих констант в audiopng (например под другое разрешение/пропорции Upload/AI Gen)
не задевали этот, отдельный, пайплайн молча. Финальный апскейл до 1080x1920 — на фронтенде,
тем же normalizeTo1080x1920, что и в audiopng (см. static/index.html).
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

# Own save-zone/outpaint-target shape — deliberately local, not imported from audiopng, so a
# future change to audiopng's own Upload/AI Gen constants can't silently change this pipeline.
COMP_SAVEZONE_W, COMP_SAVEZONE_H = 720, 960     # 3:4 — matches this flow's own generation prompt
COMP_OUTPAINT_W, COMP_OUTPAINT_H = 720, 1280    # 9:16 working canvas; frontend upscales to 1080x1920

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

# Промпт для edit-модели. Логика прежняя: текст — наш (его уже извлекла vision-модель в
# колонке 1 и он приходит сюда готовой строкой), визуальный язык — с приложенного референса.
#
# Адаптирован из внешнего промпта, где на входе было ТРИ картинки (база с контентом, референс
# стиля, карта safe-zone) и модель просили сначала выписать Design Instructions, а затем вернуть
# готовый промпт текстом. Здесь не нужно ни то, ни другое: картинка на входе ровно одна и она
# всегда только референс стиля, а на другом конце стоит image-edit-модель, которая должна сразу
# нарисовать результат, а не написать промпт. Поэтому разбор стиля сформулирован как то, что
# модель делает про себя перед тем, как рисовать. Про safe-zone здесь тоже нечего сказать: в
# этом пайплайне кадрирование решает аутпеинт уже после генерации (см. _run_outpaint).
#
# Запреты («не переноси логотип», «без дисклеймера») оставлены намеренно — в отличие от промпта
# аутпеинта, где негативных формулировок избегают: Flux Fill склонен рисовать ровно то, что ему
# запретили, а эти три edit-модели такого не показывают (проверялось на всех трёх).
GEN_PROMPT_TEMPLATE = (
    "You are an expert visual style analyst and designer. The attached image is a STYLE "
    "REFERENCE ONLY. Study it to derive its design language: mood, typography, colour palette, "
    "visual hierarchy, layout principles, background treatment, decorative elements and overall "
    "design philosophy. Take from it only these reusable design principles — never its content: "
    "none of its wording, numbers or claims, and none of its objects, people, products, brands "
    "or logos.\n\n"
    "Using that design language as creative direction, build a vertical advertising creative "
    "that carries the following text:\n\n\"{text}\"\n\n"
    "TEXT AND INFORMATION — preserve strictly:\n"
    "- Use every line of the given text verbatim: same words, same language, same spelling. You "
    "may reposition, regroup and restyle it freely, but never rewrite, translate, shorten, "
    "invent or remove it. Render each line exactly once — no line may appear twice anywhere "
    "in the creative.\n"
    "- The given lines are the ONLY text in the creative. Do not add any other text: no extra "
    "headings, badges, step or day labels, captions, taglines or fine print, and nothing echoing "
    "the reference's own labels or numbering.\n"
    "- The lines arrive in reading order — headline first, then subheadline, body copy, list or "
    "step items, and finally the call to action. Preserve the logical relationships between "
    "them: what belongs with what, the order of any sequence, and how items group.\n\n"
    "DESIGN — build it freely:\n"
    "- Choose the layout, grid, container shapes, background, supporting graphics, decorative "
    "elements and type treatment that serve this text best, driven by the reference\'s design "
    "language rather than by its own composition. The number of columns or rows, the shape of "
    "cards and the grouping of blocks are yours to decide.\n"
    "- Express importance through a fresh visual hierarchy built for this text.\n"
    "- Do not include any logo, wordmark or brand mark, and do not add a legal disclaimer or "
    "fine print.\n\n"
    "Vertical orientation, 3:4 aspect ratio."
)

async def _do_generate(text: str, model: str, ref_bytes: bytes, ref_content_type: str, ref_filename: str) -> dict:
    """The actual (slow) work — see audiopng._start_async_job for why this runs in the background
    instead of directly in the request handler (GPT Image 2 in particular can take 2-3 minutes,
    well past what Cloudflare's edge lets a single proxied HTTP request run for)."""
    if model not in GENERATION_MODELS:
        raise HTTPException(400, f"Unknown model: {model}")
    cfg = GENERATION_MODELS[model]

    async with httpx.AsyncClient(timeout=280) as client:
        ref_url = await audiopng._fal_storage_upload(client, ref_bytes, ref_content_type, ref_filename)

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
            client, img_resp.content, COMP_SAVEZONE_W, COMP_SAVEZONE_H, COMP_OUTPAINT_W, COMP_OUTPAINT_H
        )

    return {"url": final_url, "savezone_url": savezone_url, "outpaint_method": method}

@router.post("/api/generate")
async def api_generate(
    text: str = Form(...),
    model: str = Form(...),
    file: UploadFile = File(...),
):
    """One call = one reference image = one generated creative. Frontend calls this once per
    competitor reference. Pipeline: upload reference -> call the chosen edit model (text + 1
    reference image) -> 3:4 result -> outpaint (reusing audiopng._run_outpaint) -> 9:16 result.
    Kicks off the work in the background and returns a job_id immediately — poll
    GET /api/job/{job_id} (same endpoint audiopng exposes, shared job store) for the result."""
    audiopng._require_fal_key()
    if model not in GENERATION_MODELS:
        raise HTTPException(400, f"Unknown model: {model}")
    ref_bytes = await file.read()
    ref_content_type = file.content_type or "image/png"
    ref_filename = file.filename or "reference.png"
    job_id = audiopng._start_async_job(_do_generate(text, model, ref_bytes, ref_content_type, ref_filename))
    return {"job_id": job_id}

@router.get("/api/job/{job_id}")
async def api_job(job_id: str):
    """Same job store as audiopng — /api/generate above starts jobs via audiopng._start_async_job,
    so polling reuses its lookup rather than keeping a separate one here."""
    job = audiopng._get_job(job_id)
    if job is None:
        raise HTTPException(404, "Job not found")
    return job

@router.post("/api/outpaint-only")
async def api_outpaint_only(file: UploadFile = File(...)):
    """Re-run just the outpaint step on an already-generated 3:4 save-zone (the frontend re-fetches
    savezone_url from a previous /api/generate call and posts it here) — skips the paid generation-
    model call entirely, for when only the outpaint result needs a retry."""
    audiopng._require_fal_key()
    content = await file.read()
    async with httpx.AsyncClient(timeout=280) as client:
        final_url, method = await audiopng._run_outpaint(
            client, content, COMP_SAVEZONE_W, COMP_SAVEZONE_H, COMP_OUTPAINT_W, COMP_OUTPAINT_H
        )
    return {"url": final_url, "outpaint_method": method}
