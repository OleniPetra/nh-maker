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

# Модели генерации — теперь ЧИСТО text-to-image: картинку-референс они не видят вовсе.
# Раньше здесь стояли /edit-эндпоинты, которым референс передавался напрямую, и они тянули из
# него не только стиль, но и содержимое: в тестах модель дописывала подписи вроде
# "Day 1: AI Tools Overview", подсмотренные у референса. Теперь стиль сначала переводится в
# слова отдельным LLM-шагом (см. STYLE_ANALYSIS_PROMPT), а t2i-модель работает только с текстом
# и физически не может ничего скопировать.
GENERATION_MODELS = {
    "nano-banana-2": {
        "name": "Nano Banana 2",
        "t2i_url": "https://fal.run/fal-ai/nano-banana-2",
        "payload_extra": {"aspect_ratio": "3:4", "resolution": "1K"},
    },
    "nano-banana-pro": {
        "name": "Nano Banana Pro",
        "t2i_url": "https://fal.run/fal-ai/nano-banana-pro",
        "payload_extra": {"aspect_ratio": "3:4", "resolution": "1K"},
    },
    "gpt-image-2": {
        "name": "GPT Image 2",
        "t2i_url": "https://fal.run/openai/gpt-image-2",
        # у gpt-image-2 нет enum aspect_ratio — ближайшее 3:4 явными размерами, кратными 16
        "payload_extra": {"image_size": {"width": 864, "height": 1152}, "quality": "high"},
    },
}

@router.get("/api/generation-models")
async def api_generation_models():
    return [{"id": k, "name": v["name"]} for k, v in GENERATION_MODELS.items()]

# Модель, которая смотрит на референс и переводит его стиль в слова. Vision обязателен.
STYLE_MODEL = "google/gemini-3.7-flash"

# Шаг 1 — разбор стиля. Просим ТОЛЬКО Design Instructions, а финальный t2i-промпт собираем
# из них кодом ниже. В промпте-доноре LLM просили вернуть уже собранный промпт целиком, но
# здесь это лишний риск: модель должна была бы дословно переписать весь наш текст внутрь
# ответа, и любая её вольность (сокращение строки, markdown-обёртка, комментарий) молча
# испортила бы креатив. Так LLM отвечает за то, в чём она сильна — за описание стиля, — а
# неизменяемые правила и сам текст подставляет код.
STYLE_ANALYSIS_PROMPT = (
    "You are an expert visual style analyst. Analyse the attached image and describe ONLY its "
    "design characteristics: mood, style, typography, colour palette, visual hierarchy, layout "
    "principles, background style, decorative elements and overall design philosophy.\n\n"
    "Do not describe the image's content, objects, text, people, products, brands or logos. "
    "Extract only reusable design principles — the result must be usable to design a completely "
    "different creative on a different topic.\n\n"
    "Reply with the design description itself and nothing else: no preamble, no headings, no "
    "markdown, no bullet list, no commentary. Aim for a dense paragraph of 120-200 words."
)

# Шаг 2 — сборка промпта для t2i-модели. Design Instructions от LLM плюс наш текст плюс
# неизменяемые правила. Референса t2i-модель не видит, поэтому запреты «не копируй логотип
# референса» больше не нужны — скопировать нечего.
GEN_PROMPT_TEMPLATE = (
    "Design a vertical advertising creative.\n\n"
    "VISUAL DIRECTION — follow this design language:\n{design}\n\n"
    "TEXT — the creative carries exactly these lines:\n\n\"{text}\"\n\n"
    "TEXT RULES:\n"
    "- Render every line verbatim: same words, same language, same spelling. Arrange, group and "
    "style them freely, but never rewrite, translate, shorten, invent or remove them.\n"
    "- Render each line exactly once — no line may appear twice anywhere in the creative.\n"
    "- These lines are the ONLY text in the creative. Add no other text: no extra headings, "
    "badges, step or day labels, captions, taglines or fine print.\n"
    "- The lines arrive in reading order — headline first, then subheadline, body copy, list or "
    "step items, and finally the call to action. Preserve how they group and any sequence order.\n\n"
    "DESIGN RULES:\n"
    "- Choose the layout, grid, container shapes, background, supporting graphics and decorative "
    "elements that serve this text best, expressed through the visual direction above.\n"
    "- Build a clear visual hierarchy: the headline dominates, the call to action reads as the "
    "final action.\n"
    "- Do not include any logo, wordmark or brand mark, and do not add a legal disclaimer or "
    "fine print.\n\n"
    "Vertical orientation, 3:4 aspect ratio."
)

async def analyse_style(client: httpx.AsyncClient, ref_bytes: bytes, content_type: str) -> str:
    """Переводит визуальный стиль референса в текстовое описание (шаг 1 пайплайна)."""
    api_key = _require_openrouter_key()
    b64 = base64.b64encode(ref_bytes).decode()
    resp = await client.post(
        OPENROUTER_URL,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={
            "model": STYLE_MODEL,
            "temperature": 0.4,   # немного свободы: это творческое описание, а не извлечение фактов
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "text", "text": STYLE_ANALYSIS_PROMPT},
                    {"type": "image_url", "image_url": {"url": f"data:{content_type};base64,{b64}"}},
                ],
            }],
        },
    )
    if resp.status_code != 200:
        raise HTTPException(resp.status_code, detail=f"Style analysis failed: {resp.text}")
    return resp.json()["choices"][0]["message"]["content"].strip()


async def _do_generate(text: str, model: str, ref_bytes: bytes, ref_content_type: str, ref_filename: str) -> dict:
    """Пайплайн из двух шагов: LLM переводит стиль референса в слова, затем t2i-модель рисует
    креатив по одному лишь тексту. Референс до генератора картинки не доходит вовсе.

    Запускается фоновой задачей — см. audiopng._start_async_job: теперь шагов два, и суммарно
    это тем более дольше того, что Cloudflare разрешает держать одному HTTP-запросу."""
    if model not in GENERATION_MODELS:
        raise HTTPException(400, f"Unknown model: {model}")
    cfg = GENERATION_MODELS[model]

    async with httpx.AsyncClient(timeout=280) as client:
        design = await analyse_style(client, ref_bytes, ref_content_type)
        prompt = GEN_PROMPT_TEMPLATE.format(design=design, text=text.strip())
        print(f"[competitors] style={STYLE_MODEL} model={model} design={len(design)} chars", flush=True)

        payload = {"prompt": prompt, **cfg["payload_extra"]}
        resp = await client.post(cfg["t2i_url"], json=payload,
                                 headers={"Authorization": f"Key {audiopng.FAL_KEY}"})
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

    # design и prompt возвращаем наружу: когда результат выйдет неудачным, по ним видно,
    # на каком из двух шагов всё пошло не так — на разборе стиля или на отрисовке.
    return {"url": final_url, "savezone_url": savezone_url, "outpaint_method": method,
            "design": design, "prompt": prompt}

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
