#!/usr/bin/env python3
"""
Единый сервер поверх db/creatives.db, Input/ и audiopng/.

Вкладки в интерфейсе (сайдбар):
  - Creatives — Input/images/ + creatives.csv + creatives.db в одной таблице
  - Generate  — генерация нового брифа (концепция + резонинг + один промпт на выбор)
  - History   — все прошлые сгенерированные брифы
  - AudioPng  — отдельное самостоятельное приложение (генерация 9:16 креативов через
                FAL.ai + видео-рендер в браузере через ffmpeg.wasm), смонтированное
                под префиксом /audiopng. Это полноценный отдельный SPA с canvas/ffmpeg
                логикой — сайдбар делает на него обычный переход (не JS-вкладка).

Раньше это был stdlib http.server. Переведено на FastAPI/uvicorn, потому что второй
инструмент (audiopng/server.py) уже написан на FastAPI и требует multipart file upload
(шаблоны, референсы) — на голом stdlib это болезненно, а FastAPI даёт это бесплатно.
Вся бизнес-логика (build_unified_table, db_*, job runner) не изменилась ни строкой —
поменялся только слой роутинга (было: BaseHTTPRequestHandler, стало: @app.get/post).

Долгие операции (ingest, generate) идут в фоновом потоке/процессе с job_id, фронтенд
опрашивает /api/job/<id> и дописывает лог по мере поступления строк.
"""
import csv
import io
import json
import mimetypes
import os
import sqlite3
import subprocess
import sys
import threading
import uuid
from pathlib import Path
from typing import Any

import requests
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.middleware.base import BaseHTTPMiddleware

SCRIPTS_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPTS_DIR.parent
sys.path.insert(0, str(SCRIPTS_DIR))
from ingest_creatives import BRANCH_ALIASES, IMAGE_EXTS, load_creatives_csv, load_env_file, normalize_key, sha256_of_text  # noqa: E402
import generate_next_creative  # noqa: E402

# Загружаем .env ДО импорта audiopng.server — он читает FAL_KEY из окружения
# прямо на уровне модуля (при импорте), так что порядок здесь принципиален.
load_env_file(PROJECT_ROOT / ".env")
API_KEY = os.environ.get("OPENROUTER_API_KEY")

AUDIOPNG_DIR = PROJECT_ROOT / "audiopng"
sys.path.insert(0, str(AUDIOPNG_DIR))
import server as audiopng  # noqa: E402

IMAGES_DIR = PROJECT_ROOT / "Input" / "images"
CSV_PATH = PROJECT_ROOT / "Input" / "creatives.csv"
DB_PATH = PROJECT_ROOT / "db" / "creatives.db"
LIBRARY_DIR = PROJECT_ROOT / "library"
INGEST_SCRIPT = SCRIPTS_DIR / "ingest_creatives.py"
UI_HTML_PATH = SCRIPTS_DIR / "review_ui.html"
PORT = 8765

JOBS = {}  # job_id -> {"kind", "lines": [...], "done": bool, "error": str|None, "result": dict|None}


# ---------- Creatives (images/ + creatives.csv + creatives.db, merged) ----------

def scan_images() -> dict:
    if not IMAGES_DIR.is_dir():
        return {}
    return {p.name: p for p in sorted(IMAGES_DIR.iterdir()) if p.suffix.lower() in IMAGE_EXTS}


def build_unified_table() -> list:
    """images/ ∪ creatives.csv ∪ creatives.db, по filename, в одну таблицу."""
    images = scan_images()
    try:
        csv_rows = load_creatives_csv(CSV_PATH)
    except RuntimeError:
        csv_rows = {}

    db_by_key = {}
    if DB_PATH.exists():
        conn = sqlite3.connect(DB_PATH)
        for creative_id, branch, score, original_filename, created_at, note_hash, extracted_json in conn.execute(
            "SELECT creative_id, branch, score, original_filename, created_at, note_hash, extracted_json FROM creatives"
        ):
            try:
                d = json.loads(extracted_json)
            except json.JSONDecodeError:
                d = {}
            db_by_key[normalize_key(Path(original_filename).stem)] = {
                "creative_id": creative_id, "branch": branch, "score": score,
                "original_filename": original_filename, "created_at": created_at, "note_hash": note_hash,
                "archetype": d.get("creative_archetype"), "note": d.get("marketer_note_raw"),
            }
        conn.close()

    all_keys = set(csv_rows) | set(db_by_key) | {normalize_key(Path(f).stem) for f in images}
    rows = []
    for key in all_keys:
        disk_filename = next((f for f in images if normalize_key(Path(f).stem) == key), None)
        csv_row = csv_rows.get(key)
        db_row = db_by_key.get(key)

        filename = disk_filename or (csv_row["raw_filename"] if csv_row else None) or (db_row["original_filename"] if db_row else None)
        if disk_filename:
            thumb_url = "/image/" + disk_filename
        elif db_row:
            thumb_url = "/library-image/" + db_row["creative_id"]
        else:
            thumb_url = None

        branch = (csv_row["branch"] if csv_row else None) or (db_row["branch"] if db_row else None) or "certif"
        score = (csv_row["score"] if csv_row else None) or (db_row["score"] if db_row else None)
        note = (csv_row["note"] if csv_row else None) or (db_row["note"] if db_row else None) or ""

        has_file, in_db = disk_filename is not None, db_row is not None
        if in_db and has_file:
            status = "processed"
        elif in_db and not has_file:
            status = "missing_file"
        elif not in_db and has_file:
            status = "new"
        else:
            status = "csv_orphan"

        stale = False
        if status == "processed":
            current_hash = sha256_of_text(note) if note else None
            stale = current_hash != db_row.get("note_hash")

        rows.append({
            "key": key, "filename": filename, "thumb_url": thumb_url,
            "branch": branch, "score": score, "note": note, "status": status, "stale": stale,
            "creative_id": db_row["creative_id"] if db_row else None,
            "archetype": db_row["archetype"] if db_row else None,
            "created_at": db_row["created_at"] if db_row else None,
        })
    return rows


def save_review_rows(payload_rows: list) -> None:
    images = scan_images()
    try:
        existing = load_creatives_csv(CSV_PATH)
    except RuntimeError:
        existing = {}

    merged = dict(existing)
    for row in payload_rows:
        raw_filename = row.get("filename")
        if raw_filename not in images:
            continue
        score, branch, note = row.get("score"), row.get("branch"), (row.get("note") or "").strip()
        if score not in (1, 2, 3) or branch not in BRANCH_ALIASES:
            continue
        key = normalize_key(Path(raw_filename).stem)
        merged[key] = {
            "score": score, "note": note or None,
            "branch": BRANCH_ALIASES[branch], "raw_filename": raw_filename,
        }

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["filename", "score", "note", "branch"])
    for row in sorted(merged.values(), key=lambda r: r["raw_filename"]):
        writer.writerow([row["raw_filename"], row["score"], row["note"] or "", row["branch"]])
    CSV_PATH.write_text(buf.getvalue(), encoding="utf-8")


# ---------- Database (creatives.db browsing) ----------

def ensure_briefs_table(conn):
    conn.execute(
        "CREATE TABLE IF NOT EXISTS generated_briefs ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, brief_id TEXT UNIQUE NOT NULL, branch TEXT NOT NULL, "
        "model_used TEXT NOT NULL, input_creative_ids TEXT NOT NULL, output_json TEXT NOT NULL, created_at TEXT NOT NULL)"
    )


def db_summary() -> dict:
    if not DB_PATH.exists():
        return {}
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute("SELECT branch, score, extracted_json FROM creatives").fetchall()
    conn.close()
    summary = {}
    for branch, score, extracted_json in rows:
        s = summary.setdefault(branch, {"total": 0, "score_1": 0, "score_2": 0, "score_3": 0, "with_note": 0})
        s["total"] += 1
        s[f"score_{score}"] += 1
        try:
            if json.loads(extracted_json).get("marketer_note_raw"):
                s["with_note"] += 1
        except json.JSONDecodeError:
            pass
    return summary


def db_get_creative(creative_id: str) -> dict | None:
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute("SELECT extracted_json FROM creatives WHERE creative_id = ?", (creative_id,)).fetchone()
    conn.close()
    return json.loads(row[0]) if row else None


def db_delete_creative(creative_id: str) -> bool:
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute("SELECT library_path FROM creatives WHERE creative_id = ?", (creative_id,)).fetchone()
    if row is None:
        conn.close()
        return False
    conn.execute("DELETE FROM creatives WHERE creative_id = ?", (creative_id,))
    conn.commit()
    conn.close()
    library_file = (PROJECT_ROOT / row[0]).resolve()
    if LIBRARY_DIR.resolve() in library_file.parents and library_file.is_file():
        library_file.unlink()
    return True


def db_get_library_path(creative_id: str) -> Path | None:
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute("SELECT library_path FROM creatives WHERE creative_id = ?", (creative_id,)).fetchone()
    conn.close()
    if not row:
        return None
    path = (PROJECT_ROOT / row[0]).resolve()
    if LIBRARY_DIR.resolve() not in path.parents:
        return None
    return path if path.is_file() else None


def db_briefs(branch: str | None) -> list:
    if not DB_PATH.exists():
        return []
    conn = sqlite3.connect(DB_PATH)
    ensure_briefs_table(conn)
    q = "SELECT brief_id, branch, model_used, created_at, output_json FROM generated_briefs"
    params = []
    if branch:
        q += " WHERE branch = ?"
        params.append(branch)
    q += " ORDER BY created_at DESC"
    rows = conn.execute(q, params).fetchall()
    conn.close()
    out = []
    for brief_id, br, model_used, created_at, output_json in rows:
        d = json.loads(output_json)
        out.append({
            "brief_id": brief_id, "branch": br, "model_used": model_used, "created_at": created_at,
            "niche": d.get("target_audience_niche"),
            "headline": d.get("new_creative_copy", {}).get("main_headline"),
            "archetype": d.get("design_spec", {}).get("creative_archetype"),
        })
    return out


def db_get_brief(brief_id: str) -> dict | None:
    conn = sqlite3.connect(DB_PATH)
    ensure_briefs_table(conn)
    row = conn.execute("SELECT output_json FROM generated_briefs WHERE brief_id = ?", (brief_id,)).fetchone()
    conn.close()
    return json.loads(row[0]) if row else None


def _delete_brief_output_file(brief_id: str, branch: str) -> None:
    path = PROJECT_ROOT / "generated" / branch / f"{brief_id}.json"
    if path.is_file():
        path.unlink()


def db_delete_brief(brief_id: str) -> bool:
    conn = sqlite3.connect(DB_PATH)
    ensure_briefs_table(conn)
    row = conn.execute("SELECT branch FROM generated_briefs WHERE brief_id = ?", (brief_id,)).fetchone()
    if row is None:
        conn.close()
        return False
    conn.execute("DELETE FROM generated_briefs WHERE brief_id = ?", (brief_id,))
    conn.commit()
    conn.close()
    _delete_brief_output_file(brief_id, row[0])
    return True


def db_clear_briefs(branch: str | None) -> int:
    conn = sqlite3.connect(DB_PATH)
    ensure_briefs_table(conn)
    if branch:
        rows = conn.execute("SELECT brief_id, branch FROM generated_briefs WHERE branch = ?", (branch,)).fetchall()
        conn.execute("DELETE FROM generated_briefs WHERE branch = ?", (branch,))
    else:
        rows = conn.execute("SELECT brief_id, branch FROM generated_briefs").fetchall()
        conn.execute("DELETE FROM generated_briefs")
    conn.commit()
    conn.close()
    for brief_id, br in rows:
        _delete_brief_output_file(brief_id, br)
    return len(rows)


# ---------- Jobs (ingest subprocess / generate thread) ----------

def _run_ingest_job(job_id: str):
    job = JOBS[job_id]
    proc = subprocess.Popen(
        [sys.executable, "-u", str(INGEST_SCRIPT)],
        cwd=str(PROJECT_ROOT), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
    )
    for line in proc.stdout:
        job["lines"].append(line.rstrip("\n"))
    proc.wait()
    job["done"] = True
    if proc.returncode != 0:
        job["error"] = f"ingest_creatives.py завершился с кодом {proc.returncode}"


def _run_generate_job(job_id: str, branch: str, mode: str, hint: str, include_history: bool):
    job = JOBS[job_id]

    def log(msg):
        job["lines"].append(str(msg))

    try:
        result = generate_next_creative.run_generation(branch, API_KEY, mode, hint, include_history, log=log)
        job["result"] = {"brief_id": result["brief_id"], "brief": result["brief"], "mode": result["mode"]}
    except Exception as e:
        job["error"] = str(e)
    finally:
        job["done"] = True


def start_job(kind: str, target, args: tuple) -> str:
    if kind == "ingest" and any(j["kind"] == "ingest" and not j["done"] for j in JOBS.values()):
        raise RuntimeError("ingest уже выполняется")
    job_id = uuid.uuid4().hex
    JOBS[job_id] = {"kind": kind, "lines": [], "done": False, "error": None, "result": None}
    threading.Thread(target=target, args=(job_id, *args), daemon=True).start()
    return job_id


# ---------- FastAPI app ----------

app = FastAPI(title="Static Creo")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


class NoCacheFFmpeg(BaseHTTPMiddleware):
    """Предотвращает кэширование ffmpeg.wasm воркеров (даёт протухшие cross-origin URL)."""
    async def dispatch(self, request, call_next):
        resp = await call_next(request)
        if request.url.path.startswith("/audiopng/ffmpeg/"):
            resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        return resp


app.add_middleware(NoCacheFFmpeg)


def _serve_bytes(data: bytes, content_type: str) -> Response:
    return Response(content=data, media_type=content_type)


@app.get("/", response_class=HTMLResponse)
def index():
    return UI_HTML_PATH.read_text(encoding="utf-8")


@app.get("/api/table")
def api_table():
    return JSONResponse(build_unified_table())


@app.get("/image/{filename:path}")
def get_image(filename: str):
    path = IMAGES_DIR / filename
    if not path.is_file() or path.parent.resolve() != IMAGES_DIR.resolve():
        raise HTTPException(404)
    mime, _ = mimetypes.guess_type(str(path))
    return _serve_bytes(path.read_bytes(), mime or "application/octet-stream")


@app.get("/library-image/{creative_id}")
def get_library_image(creative_id: str):
    path = db_get_library_path(creative_id)
    if path is None:
        raise HTTPException(404)
    mime, _ = mimetypes.guess_type(str(path))
    return _serve_bytes(path.read_bytes(), mime or "application/octet-stream")


@app.get("/api/db/summary")
def api_db_summary():
    return JSONResponse(db_summary())


@app.get("/api/db/creative/{creative_id}")
def api_db_creative(creative_id: str):
    data = db_get_creative(creative_id)
    if data is None:
        raise HTTPException(404)
    return JSONResponse(data)


@app.get("/api/db/briefs")
def api_db_briefs(branch: str | None = None):
    return JSONResponse(db_briefs(branch))


@app.get("/api/db/briefs/{brief_id}")
def api_db_brief(brief_id: str):
    data = db_get_brief(brief_id)
    if data is None:
        raise HTTPException(404)
    return JSONResponse(data)


@app.get("/api/job/{job_id}")
def api_job(job_id: str):
    job = JOBS.get(job_id)
    if job is None:
        raise HTTPException(404)
    return JSONResponse(job)


@app.get("/api/openrouter/credits")
def api_openrouter_credits():
    if not API_KEY:
        raise HTTPException(400, "нет ключа")
    r = requests.get("https://openrouter.ai/api/v1/credits", headers={"Authorization": f"Bearer {API_KEY}"}, timeout=15)
    return JSONResponse(r.json().get("data", {}))


class SaveBody(BaseModel):
    rows: list[dict[str, Any]] = []


@app.post("/api/save")
def api_save(body: SaveBody):
    save_review_rows(body.rows)
    return {"ok": True}


@app.post("/api/run/ingest")
def api_run_ingest():
    try:
        job_id = start_job("ingest", _run_ingest_job, ())
    except RuntimeError as e:
        raise HTTPException(409, str(e))
    return {"job_id": job_id}


class GenerateBody(BaseModel):
    branch: str
    mode: str = "gpt_image_2"
    hint: str = ""
    include_history: bool = True


@app.post("/api/run/generate")
def api_run_generate(body: GenerateBody):
    if body.branch not in BRANCH_ALIASES:
        raise HTTPException(400, f"неизвестный branch '{body.branch}'")
    if body.mode not in generate_next_creative.MODES:
        raise HTTPException(400, f"неизвестный mode '{body.mode}'")
    if not API_KEY:
        raise HTTPException(400, "OPENROUTER_API_KEY не найден")
    job_id = start_job(
        "generate", _run_generate_job,
        (BRANCH_ALIASES[body.branch], body.mode, body.hint, body.include_history),
    )
    return {"job_id": job_id}


class CreativeIdBody(BaseModel):
    creative_id: str


@app.post("/api/db/delete")
def api_db_delete(body: CreativeIdBody):
    if not db_delete_creative(body.creative_id):
        raise HTTPException(404, "не найдено")
    return {"ok": True}


class BriefIdBody(BaseModel):
    brief_id: str


@app.post("/api/db/briefs/delete")
def api_db_briefs_delete(body: BriefIdBody):
    if not db_delete_brief(body.brief_id):
        raise HTTPException(404, "не найдено")
    return {"ok": True}


class ClearBriefsBody(BaseModel):
    branch: str | None = None


@app.post("/api/db/briefs/clear")
def api_db_briefs_clear(body: ClearBriefsBody):
    count = db_clear_briefs(body.branch or None)
    return {"deleted": count}


# ---------- AudioPng (отдельное приложение, смонтировано под /audiopng) ----------
app.include_router(audiopng.router)
app.mount("/audiopng/templates", StaticFiles(directory=str(audiopng.TEMPLATES_DIR)), name="audiopng_templates")
app.mount("/audiopng", StaticFiles(directory=str(audiopng.STATIC), html=True), name="audiopng_static")

# Общий сайдбар (scripts/shared/sidebar.js) — единственный источник правды для
# навигации, подключается и review_ui.html, и audiopng/static/index.html по
# одному и тому же абсолютному пути.
app.mount("/static-shared", StaticFiles(directory=str(SCRIPTS_DIR / "shared")), name="shared_static")


def main():
    print(f"Открой в браузере: http://127.0.0.1:{PORT}")
    print("Ctrl+C для остановки.")
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning")


if __name__ == "__main__":
    main()
