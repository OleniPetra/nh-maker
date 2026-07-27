#!/usr/bin/env python3
"""
Извлекает элементы креативов через vision-модель на OpenRouter по схеме
schema/creative_schema.json и складывает результат в db/creatives.db.

Структура входа:
  Input/images/*.png|jpg|jpeg|webp   — все креативы, плоско, без подпапок
  Input/creatives.csv                — 4 колонки: filename, score (1-3), note
                                        (заметка маркетолога, необязательно), branch
                                        (certif/claude/aigen)
Открывается и правится в Excel/Numbers/Google Sheets. filename сопоставляется с файлом
в images/ устойчиво к пробелам/регистру/NFC-NFD (частая проблема у имён macOS-скриншотов).
Картинка без строки в CSV и строка без картинки — пропускаются с предупреждением.

Повторный запуск бесплатен для того, что не изменилось. Идентичность креатива — sha256
его содержимого, а не имя файла (переименование в CSV просто перепривязывает строку).
На каждый файл считается статус:
  - new       — новый хэш, не встречался -> вызывает vision-модель, INSERT
  - reprocess — хэш уже в базе, но текст note в CSV изменился (добавили/поправили/убрали)
                -> нужно заново собрать marketer_element_feedback, вызывает vision-модель,
                UPDATE существующей записи (creative_id тот же)
  - relabel   — хэш и note те же, но score/branch в CSV другие -> дешёвый UPDATE без
                вызова модели
  - skip      — всё совпадает с тем, что уже в базе -> ничего не делаем
"""
import argparse
import base64
import copy
import csv
import hashlib
import json
import mimetypes
import re
import shutil
import sqlite3
import sys
import unicodedata
import uuid
from datetime import datetime, timezone
from pathlib import Path

import requests
from jsonschema import Draft7Validator

PROJECT_ROOT = Path(__file__).resolve().parent.parent

BRANCH_ALIASES = {
    "certif": "certif",
    "sertif": "certif",
    "claude": "claude",
    "aigen": "aigen",
}
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp"}

DEFAULT_MODEL = "openai/gpt-5.4"
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

TOP_LEVEL_AUTO_FIELDS = ["creative_id", "branch", "performance", "source_meta", "marketer_note_raw"]


def load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        import os
        os.environ.setdefault(key.strip(), value.strip())


def sha256_of_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_of_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def normalize_key(text: str) -> str:
    """Устойчиво к неразрывным пробелам (частые в именах macOS-скриншотов) и NFC/NFD."""
    normalized = unicodedata.normalize("NFC", text).replace("\xa0", " ")
    return re.sub(r"\s+", " ", normalized).strip().lower()


def load_creatives_csv(csv_path: Path) -> dict:
    """
    filename_norm -> {"score": int, "note": str|None, "branch": str, "raw_filename": str}
    Понимает и запятую, и точку с запятой (частый экспорт Excel в РФ-локали), а также
    utf-8-sig (BOM от Excel) и cp1251 как запасной вариант кодировки.
    """
    if not csv_path.exists():
        raise RuntimeError(f"Не найден {csv_path}. Ожидается CSV с колонками filename,score,note,branch.")

    raw_bytes = csv_path.read_bytes()
    for encoding in ("utf-8-sig", "cp1251"):
        try:
            text = raw_bytes.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    else:
        raise RuntimeError(f"Не удалось определить кодировку {csv_path}")

    sample = text[:2048]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;")
    except csv.Error:
        dialect = csv.excel

    reader = csv.DictReader(text.splitlines(), dialect=dialect)
    if reader.fieldnames is None:
        raise RuntimeError(f"{csv_path}: пустой файл")
    fieldmap = {normalize_key(f): f for f in reader.fieldnames}
    filename_col, score_col = fieldmap.get("filename"), fieldmap.get("score")
    note_col, branch_col = fieldmap.get("note"), fieldmap.get("branch")
    missing = [n for n, c in [("filename", filename_col), ("score", score_col), ("branch", branch_col)] if not c]
    if missing:
        raise RuntimeError(f"{csv_path}: не хватает колонок {missing} (нашёл: {reader.fieldnames})")

    rows = {}
    for i, row in enumerate(reader, start=2):
        filename = (row.get(filename_col) or "").strip()
        if not filename:
            continue
        score_raw = (row.get(score_col) or "").strip()
        branch_raw = (row.get(branch_col) or "").strip().lower()
        note = (row.get(note_col) or "").strip() if note_col else ""

        if score_raw not in {"1", "2", "3"}:
            print(f"[csv:строка {i}] '{filename}': некорректный score '{score_raw}' (нужно 1/2/3) — пропускаю")
            continue
        branch = BRANCH_ALIASES.get(branch_raw)
        if branch is None:
            print(f"[csv:строка {i}] '{filename}': неизвестный branch '{branch_raw}' — пропускаю")
            continue

        rows[normalize_key(Path(filename).stem)] = {
            "score": int(score_raw),
            "note": note or None,
            "branch": branch,
            "raw_filename": filename,
        }
    return rows


def build_llm_schema(full_schema: dict) -> dict:
    """Схема для модели = полная схема минус поля, которые заполняет скрипт, а не LLM."""
    reduced = copy.deepcopy(full_schema)
    for key in TOP_LEVEL_AUTO_FIELDS:
        reduced["properties"].pop(key, None)
    reduced["required"] = [r for r in reduced["required"] if r not in TOP_LEVEL_AUTO_FIELDS]
    reduced["title"] = "StaticCreativeExtraction_LLMSubset"
    reduced["description"] = (
        "Подмножество схемы для заполнения vision-моделью. НЕ включай creative_id, "
        "branch, performance, source_meta, marketer_note_raw — эти поля добавляются "
        "отдельно. marketer_element_feedback ЗАПОЛНЯЙ — это часть твоей задачи."
    )
    return reduced


def init_db(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS creatives (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            creative_id TEXT UNIQUE NOT NULL,
            content_hash TEXT UNIQUE NOT NULL,
            note_hash TEXT,
            branch TEXT NOT NULL,
            score INTEGER NOT NULL,
            source_folder TEXT NOT NULL,
            original_filename TEXT NOT NULL,
            library_path TEXT NOT NULL,
            model_used TEXT NOT NULL,
            extracted_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_creatives_branch ON creatives(branch)")
    existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(creatives)")}
    if "note_hash" not in existing_cols:
        conn.execute("ALTER TABLE creatives ADD COLUMN note_hash TEXT")
    conn.commit()
    return conn


def call_vision_model(model: str, api_key: str, image_path: Path, llm_schema: dict,
                       marketer_note: str | None, max_retries: int) -> dict:
    mime, _ = mimetypes.guess_type(str(image_path))
    mime = mime or "image/png"
    b64 = base64.b64encode(image_path.read_bytes()).decode("ascii")
    data_uri = f"data:{mime};base64,{b64}"

    system_prompt = (
        "You are a precise visual analyst for performance marketing. You are shown "
        "one static advertising creative. Analyze it and return EXACTLY one valid "
        "JSON object matching the following JSON Schema (draft-07). No explanations, "
        "no markdown code fences — raw JSON only.\n\n"
        f"{json.dumps(llm_schema, ensure_ascii=False)}"
    )
    user_text = "Analyze this creative according to the schema. Reply with JSON only."
    if marketer_note:
        user_text += (
            f"\n\nThe marketer left this comment about this creative: \"{marketer_note}\"\n"
            "Break this comment down into specific elements of your own analysis in "
            "the marketer_element_feedback field (element_ref — which part of the "
            "schema the phrase points to, verdict — "
            "worked_well/worked_poorly/neutral_or_unclear, reasoning — a restatement "
            "of the point). If the comment gives no basis for some element of your "
            "analysis, simply don't create an entry for it."
        )
    else:
        user_text += "\n\nThere is no marketer comment — return marketer_element_feedback as an empty array []."

    messages = [
        {"role": "system", "content": system_prompt},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": user_text},
                {"type": "image_url", "image_url": {"url": data_uri}},
            ],
        },
    ]

    validator = Draft7Validator(llm_schema)
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    last_error = None

    for attempt in range(1, max_retries + 1):
        payload = {
            "model": model,
            "messages": messages,
            "temperature": 0.2,
            "response_format": {"type": "json_object"},
        }
        resp = requests.post(OPENROUTER_URL, headers=headers, json=payload, timeout=120)
        if resp.status_code != 200:
            last_error = f"HTTP {resp.status_code}: {resp.text[:500]}"
            messages.append({"role": "user", "content": f"Request failed ({last_error}). Retry, strictly valid JSON."})
            continue

        raw = resp.json()["choices"][0]["message"]["content"]
        try:
            candidate = json.loads(raw)
        except json.JSONDecodeError:
            match = re.search(r"\{.*\}", raw, re.DOTALL)
            candidate = json.loads(match.group(0)) if match else None

        if candidate is None:
            last_error = "не удалось распарсить JSON из ответа модели"
            messages.append({"role": "assistant", "content": raw})
            messages.append({"role": "user", "content": "That is not valid JSON. Return only one correct JSON object, no explanation."})
            continue

        errors = sorted(validator.iter_errors(candidate), key=lambda e: e.path)
        if not errors:
            return candidate

        last_error = "; ".join(f"{'.'.join(str(p) for p in e.path)}: {e.message}" for e in errors[:8])
        messages.append({"role": "assistant", "content": json.dumps(candidate, ensure_ascii=False)})
        messages.append({"role": "user", "content": f"The JSON does not pass schema validation: {last_error}. Return the corrected full JSON object."})

    raise RuntimeError(f"Не удалось получить валидный JSON за {max_retries} попыток. Последняя ошибка: {last_error}")


def process_image(image_path: Path, branch: str, score: int, marketer_note: str | None,
                   model: str, api_key: str, llm_schema: dict, full_validator: Draft7Validator,
                   library_dir: Path, max_retries: int, creative_id: str = None) -> dict:
    llm_output = call_vision_model(model, api_key, image_path, llm_schema, marketer_note, max_retries)

    content_hash = sha256_of_file(image_path)
    now = datetime.now(timezone.utc).isoformat()
    branch_dir = library_dir / branch
    branch_dir.mkdir(parents=True, exist_ok=True)
    library_filename = f"{content_hash[:16]}{image_path.suffix.lower()}"
    library_path = branch_dir / library_filename
    if not library_path.exists():
        shutil.copy2(image_path, library_path)

    record = dict(llm_output)
    record["creative_id"] = creative_id or str(uuid.uuid4())
    record["branch"] = branch
    record["performance"] = {"score": score}
    record["marketer_note_raw"] = marketer_note
    record["source_meta"] = {
        "image_filename": image_path.name,
        "date_added": now[:10],
        "added_by": None,
    }

    errors = sorted(full_validator.iter_errors(record), key=lambda e: e.path)
    if errors:
        msg = "; ".join(f"{'.'.join(str(p) for p in e.path)}: {e.message}" for e in errors[:8])
        raise RuntimeError(f"Собранная запись не проходит валидацию по полной схеме: {msg}")

    return {
        "content_hash": content_hash,
        "library_path": str(library_path.relative_to(PROJECT_ROOT)),
        "record": record,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--images-dir", default=str(PROJECT_ROOT / "Input" / "images"))
    parser.add_argument("--creatives-csv", default=str(PROJECT_ROOT / "Input" / "creatives.csv"))
    parser.add_argument("--db", default=str(PROJECT_ROOT / "db" / "creatives.db"))
    parser.add_argument("--schema", default=str(PROJECT_ROOT / "schema" / "creative_schema.json"))
    parser.add_argument("--library-dir", default=str(PROJECT_ROOT / "library"))
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--max-retries", type=int, default=3)
    parser.add_argument("--limit", type=int, default=None, help="Обработать не больше N креативов (new+reprocess) за запуск")
    parser.add_argument("--dry-run", action="store_true", help="Только показать статус каждого файла, без вызова API")
    args = parser.parse_args()

    load_env_file(PROJECT_ROOT / ".env")
    import os
    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key and not args.dry_run:
        sys.exit("OPENROUTER_API_KEY не найден ни в окружении, ни в .env")

    images_dir = Path(args.images_dir)
    if not images_dir.is_dir():
        sys.exit(f"Не найдена папка с картинками: {images_dir}")

    full_schema = json.loads(Path(args.schema).read_text())
    llm_schema = build_llm_schema(full_schema)
    full_validator = Draft7Validator(full_schema)

    conn = init_db(Path(args.db))
    csv_rows = load_creatives_csv(Path(args.creatives_csv))

    image_files = {normalize_key(p.stem): p for p in sorted(images_dir.iterdir()) if p.suffix.lower() in IMAGE_EXTS}

    missing_images = [row["raw_filename"] for key, row in csv_rows.items() if key not in image_files]
    missing_csv_rows = [p.name for key, p in image_files.items() if key not in csv_rows]
    if missing_images:
        print(f"В creatives.csv есть строки без файла в images/: {missing_images}")
    if missing_csv_rows:
        print(f"В images/ есть файлы без строки в creatives.csv (пропущены): {missing_csv_rows}")

    needs_vision, relabel_only, skipped = [], [], 0
    for key, image_path in image_files.items():
        row = csv_rows.get(key)
        if row is None:
            continue
        branch, score, marketer_note = row["branch"], row["score"], row["note"]
        content_hash = sha256_of_file(image_path)
        note_hash = sha256_of_text(marketer_note) if marketer_note else None

        db_row = conn.execute(
            "SELECT creative_id, branch, score, note_hash FROM creatives WHERE content_hash = ?",
            (content_hash,),
        ).fetchone()

        if db_row is None:
            needs_vision.append(("new", None, image_path, branch, score, marketer_note, note_hash))
        elif db_row[3] != note_hash:
            needs_vision.append(("reprocess", db_row[0], image_path, branch, score, marketer_note, note_hash))
        elif db_row[1] != branch or db_row[2] != score:
            relabel_only.append((content_hash, branch, score, note_hash))
        else:
            skipped += 1

    now = datetime.now(timezone.utc).isoformat()
    for content_hash, branch, score, note_hash in relabel_only:
        conn.execute(
            "UPDATE creatives SET branch = ?, score = ?, note_hash = ?, updated_at = ? WHERE content_hash = ?",
            (branch, score, note_hash, now, content_hash),
        )
    if relabel_only:
        conn.commit()
        print(f"Обновлены branch/score у {len(relabel_only)} креативов без изменения заметки (без вызова модели).")
    if skipped:
        print(f"Без изменений (пропущено без вызова модели): {skipped}")

    print(f"Требуют вызова vision-модели (новые + изменившиеся заметки): {len(needs_vision)}")
    if args.limit is not None:
        needs_vision = needs_vision[: args.limit]
        print(f"Ограничение --limit: обработаю {len(needs_vision)}")

    if args.dry_run:
        for status, existing_id, image_path, branch, score, marketer_note, note_hash in needs_vision:
            note_flag = "с заметкой" if marketer_note else "без заметки"
            print(f"[dry-run:{status}] {image_path.name} -> branch={branch}, score={score}, {note_flag}")
        conn.close()
        return

    processed, reprocessed, failed = 0, 0, []
    for status, existing_id, image_path, branch, score, marketer_note, note_hash in needs_vision:
        try:
            result = process_image(
                image_path, branch, score, marketer_note, args.model, api_key,
                llm_schema, full_validator, Path(args.library_dir), args.max_retries,
                creative_id=existing_id,
            )
        except Exception as e:
            print(f"[FAIL] {image_path.name}: {e}")
            failed.append((image_path.name, str(e)))
            continue

        now = datetime.now(timezone.utc).isoformat()
        if status == "new":
            conn.execute(
                """
                INSERT INTO creatives
                    (creative_id, content_hash, note_hash, branch, score, source_folder, original_filename,
                     library_path, model_used, extracted_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    result["record"]["creative_id"], result["content_hash"], note_hash, branch, score, "images",
                    image_path.name, result["library_path"], args.model,
                    json.dumps(result["record"], ensure_ascii=False), now, now,
                ),
            )
            processed += 1
        else:
            conn.execute(
                """
                UPDATE creatives
                SET note_hash = ?, branch = ?, score = ?, model_used = ?,
                    extracted_json = ?, updated_at = ?
                WHERE content_hash = ?
                """,
                (
                    note_hash, branch, score, args.model,
                    json.dumps(result["record"], ensure_ascii=False), now, result["content_hash"],
                ),
            )
            reprocessed += 1
        conn.commit()
        print(f"[{status.upper()}] {image_path.name} -> {result['record']['creative_id']}")

    total = conn.execute("SELECT COUNT(*) FROM creatives").fetchone()[0]
    conn.close()

    print(f"\nГотово. Новых: {processed}. Пересобрано из-за изменившейся заметки: {reprocessed}. Ошибок: {len(failed)}. Всего в базе: {total}.")
    if failed:
        print("Не обработаны (можно перезапустить скрипт — они попробуются снова):")
        for filename, err in failed:
            print(f"  - {filename}: {err}")


if __name__ == "__main__":
    main()
