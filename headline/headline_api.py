"""
Headline — заменяет ТОЛЬКО заголовок (headline) готового рекламного креатива, оставляя
body copy/CTA/визуал/бренд без изменений. Инпут: N заголовков (до 20) x M креативов (до 3) —
на выходе N*M картинок, по одному вызову nano-banana-2/edit на каждую пару (1 заголовок +
1 креатив-референс). Модель сама находит, что на картинке является заголовком, и заменяет
только его — остальной макет остаётся как есть.

Named "headline_api.py", NOT "api.py" — competitors/api.py уже занимает модуль с именем "api"
в sys.modules; если бы этот файл тоже назывался api.py, второй `import api as ...` вернул бы
уже закэшированный модуль competitors, а не этот. См. тот же манёвр в competitors/api.py про
audiopng/server.py — тут тот же самый класс бага, только на один уровень глубже.

Переиспользует FAL-инфраструктуру из audiopng/server.py (upload в FAL Storage, общий
job-store для фоновых генераций) — как и competitors/api.py.
"""
from pathlib import Path

import httpx
from fastapi import APIRouter, Form, HTTPException, UploadFile, File

import server as audiopng  # уже импортирован раньше в review_server.py — полагаемся на кэш
# sys.modules['server']; sys.path.insert для этой директории всё равно делаем в
# review_server.py на случай прямого запуска/иного порядка импорта (см. competitors/api.py).

BASE = Path(__file__).resolve().parent
STATIC = BASE / "static"

router = APIRouter(prefix="/headline")

EDIT_URL = "https://fal.run/fal-ai/nano-banana-2/edit"

# aspect_ratio "auto" — модель сама определяет соотношение сторон по инпут-картинке, а не
# фиксирует его жёстко (в отличие от Competitors Static, где вход всегда 3:4 savezone). Тут
# вход — уже готовый креатив произвольного соотношения (9:16, 1:1, 1.91:1 и т.д.), и менять
# его нельзя — правится только текст заголовка, геометрия должна остаться как в оригинале.
GEN_PAYLOAD_EXTRA = {"aspect_ratio": "auto", "resolution": "1K"}

HEADLINE_PROMPT_TEMPLATE = (
    "This is an advertising creative. Find its main headline — the single largest, most "
    "prominent piece of text, distinct from any smaller body copy, disclaimer, or "
    "call-to-action button label — and replace it with the following text exactly as given:\n\n"
    "\"{text}\"\n\n"
    "Match the original headline's font style, weight, letter case, color, and placement as "
    "closely as possible so the replacement looks native to the design. If the new text is a "
    "different length than the original, re-wrap or re-scale it the way the original design "
    "would, without spilling outside its original text area. Do not change anything else in "
    "the image: keep the body copy, CTA button, imagery, colors, layout, and branding exactly "
    "as in the original — only the headline text changes."
)

async def _do_generate(text: str, ref_bytes: bytes, ref_content_type: str, ref_filename: str) -> dict:
    """The actual (slow) work — see audiopng._start_async_job for why this runs in the background
    instead of directly in the request handler."""
    async with httpx.AsyncClient(timeout=280) as client:
        ref_url = await audiopng._fal_storage_upload(client, ref_bytes, ref_content_type, ref_filename)
        prompt = HEADLINE_PROMPT_TEMPLATE.format(text=text.strip())
        payload = {"prompt": prompt, "image_urls": [ref_url], **GEN_PAYLOAD_EXTRA}
        resp = await client.post(EDIT_URL, json=payload, headers={"Authorization": f"Key {audiopng.FAL_KEY}"})
        if resp.status_code != 200:
            raise HTTPException(resp.status_code, detail=f"Generation failed: {resp.text}")
        result_url = audiopng._extract_image_url(resp.json())
        if not result_url:
            raise HTTPException(500, "No image URL from generation model")
    return {"url": result_url}

@router.post("/api/generate")
async def api_generate(
    text: str = Form(...),
    file: UploadFile = File(...),
):
    """One call = one headline x one creative. Frontend calls this once per (headline, creative)
    pair — total calls = headlines x creatives. Kicks off the work in the background and returns
    a job_id immediately — poll GET /api/job/{job_id} (shared job store, see audiopng) for the
    result."""
    audiopng._require_fal_key()
    ref_bytes = await file.read()
    ref_content_type = file.content_type or "image/png"
    ref_filename = file.filename or "creative.png"
    job_id = audiopng._start_async_job(_do_generate(text, ref_bytes, ref_content_type, ref_filename))
    return {"job_id": job_id}

@router.get("/api/job/{job_id}")
async def api_job(job_id: str):
    """Same job store as audiopng — /api/generate above starts jobs via audiopng._start_async_job,
    so polling reuses its lookup rather than keeping a separate one here."""
    job = audiopng._get_job(job_id)
    if job is None:
        raise HTTPException(404, "Job not found")
    return job
