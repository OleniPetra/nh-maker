#!/usr/bin/env python3
"""
Берёт все обработанные креативы одного branch из db/creatives.db (со всеми их
text_blocks/композицией/цветом/структурой и оценкой 1-3, плюс marketer_element_feedback
там, где есть) и одним лёгким запросом к LLM собирает: новую вертикальную концепцию с реальным
копирайтом, компактный 3-частный резонинг (что переиспользовано, чего избегали, что
придумано новое и почему) и ОДИН промпт для генерации изображения — какой именно,
зависит от --mode:
  - gpt_image_2   -> промпт под GPT Image 2
  - nano_banana_2 -> промпт под Nano Banana 2
  - universal     -> короткий модель-агностичный промпт

Раньше один запрос сразу тянул оба промпта + подробный построчный rationale — это было
избыточно тяжело и медленно. Теперь запрос лёгкий и просит только то, что нужно.

Модель по умолчанию должна копировать ДОМИНИРУЮЩИЙ архетип базы (не изобретать новый
каждый раз) — раньше промпт наоборот требовал "обязательно что-то новое структурно",
из-за чего модель регулярно уходила от реально проверенного/частого паттерна к
самопридуманному. Распределение архетипов (сколько раз встречается каждый и с какой
средней оценкой) считается детерминированно в Python (`build_archetype_stats`) и
подаётся модели готовым текстом — так она не должна вычислять его сама по сырому
JSON на ~80k+ токенов, где легко упустить, какой паттерн реально доминирует.
"Новизна" теперь ограничена поверхностью (ниша, копирайт, конкретные инструменты/иконки,
цветовой акцент), а не структурой/архетипом.

Чтобы новые концепции не повторяли одну и ту же нишу между запусками, скрипт подтягивает
историю всех предыдущих брифов этого branch — только идею (нишу + однострочную концепцию,
без архетипа/структуры/коннектора/фона — это раздувало контекст ненужными деталями) — из
generated_briefs. После получения ответа скрипт также проверяет target_audience_niche на
схожесть с уже использованными — при совпадении просит модель переделать (тот же
retry-цикл, что и для невалидного JSON).

Маркетолог может передать свободную подсказку (--hint / hint=... в UI) — например
"сошлись больше на creative_id X", "попробуй лестницу вместо сетки", "светлая тема" —
она уходит в системный промпт как явное руководство поверх статистики базы.

Картинку на этом шаге не генерирует — только промпт + обоснование (см. договорённость).
"""
import argparse
import difflib
import json
import sqlite3
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

import requests
from jsonschema import Draft7Validator

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_MODEL = "anthropic/claude-sonnet-5"
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

MODES = {
    "gpt_image_2": "image_prompt_gpt_image_2",
    "nano_banana_2": "image_prompt_nano_banana_2",
    "universal": "universal_prompt",
}

# Based on OpenAI's and Google Cloud's public prompting guides for GPT Image 2 /
# Nano Banana 2 (July 2026, see chat). No pixel dimensions or aspect-ratio talk —
# that's already set via the generation API parameters, not the prompt text — canvas
# is just "a vertical creative". Kept in English: these models are most reliably
# steered in English regardless of the target audience's language. The Nexera-logo/
# disclaimer rule lives once in the system prompt (item 4) — not repeated here.
MODE_RULES = {
    "gpt_image_2": """
## How to write image_prompt_gpt_image_2 (GPT Image 2 rules)
- Write in vivid, natural descriptive prose — not a technical spec sheet.
  Order of thought: background/scene -> main subject -> key details -> what stays
  fixed. Short paragraphs are fine; avoid dry "CONSTRAINTS:"-style bullet blocks.
- Describe the canvas as a vertical creative, and describe the top/middle/bottom
  sections proportionally ("in the top fifth of the frame", "centered, taking up
  most of the height") — never pixel values or aspect ratios (already set via the
  generation parameters).
- Any literal on-image text goes in quotes, with the font/color/placement
  described right next to it in plain language.
- Don't overload the prompt with pseudo-camera jargon (lens, film stock) — those
  are for mood/composition, not for pixel-level precision.
""",
    "nano_banana_2": """
## How to write image_prompt_nano_banana_2 (Nano Banana 2 / Gemini rules)
- Connected narrative prose, NOT a comma-separated keyword list.
- Open with a strong verb naming the main action/operation.
- Positive framing — describe what should be in frame, never what should be
  absent.
- Use cinematic/photographic language (angle, lighting, materials) inside full
  sentences.
- Describe the frame format in words ("a tall vertical frame, like a phone
  screen"), and section proportions in words too ("the upper part of the
  frame", "the bottom fifth") — no pixel sizes or aspect ratios (already set via
  the generation parameters).
- Literal text goes in quotes, with the font/effect described in the same
  sentence.
""",
    "universal": """
## How to write universal_prompt (model-agnostic)
- A shorter, plain descriptive prompt not tuned to any single model's quirks —
  a solid general-purpose starting point.
- Natural prose, proportions in words, no pixel sizes or aspect ratios.
- Literal text in quotes with typography noted alongside.
""",
}


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


def load_branch_creatives(db_path: Path, branch: str):
    conn = sqlite3.connect(db_path)
    rows = conn.execute(
        "SELECT creative_id, score, extracted_json FROM creatives WHERE branch = ? ORDER BY score DESC",
        (branch,),
    ).fetchall()
    conn.close()
    return [{"creative_id": r[0], "score": r[1], "data": json.loads(r[2])} for r in rows]


def build_archetype_stats(creatives) -> str:
    """Deterministic (non-LLM) count of how often each archetype appears and how it scores.
    The raw evidence payload is ~80k+ tokens of nested JSON for a modest-sized branch — asking
    the model to eyeball that and correctly infer which single archetype actually dominates is
    unreliable. Surfacing the count/average explicitly, in plain text, up front, means the model
    doesn't have to compute it — and can't miss it."""
    from collections import defaultdict
    stats = defaultdict(lambda: {"count": 0, "scores": []})
    for c in creatives:
        arch = c["data"].get("creative_archetype") or "unknown"
        stats[arch]["count"] += 1
        stats[arch]["scores"].append(c["score"])
    total = len(creatives)
    lines = []
    for arch, s in sorted(stats.items(), key=lambda kv: -kv[1]["count"]):
        scores = s["scores"]
        avg = sum(scores) / len(scores)
        n3 = scores.count(3)
        lines.append(f"- {arch}: {s['count']}/{total} creatives, avg score {avg:.2f}, {n3} scored 3 (excellent)")
    return "\n".join(lines)


def build_evidence_payload(creatives) -> str:
    """Компактное, но полное представление каждого креатива для синтез-LLM."""
    blocks = []
    for c in creatives:
        d = c["data"]
        blocks.append({
            "creative_id": c["creative_id"],
            "score": c["score"],
            "creative_archetype": d.get("creative_archetype"),
            "composition": d.get("composition"),
            "text_blocks": d.get("text_content", {}).get("text_blocks"),
            "cta": d.get("text_content", {}).get("cta"),
            "copy_tone": d.get("text_content", {}).get("copy_tone"),
            "typography": d.get("typography"),
            "color": d.get("color"),
            "imagery": d.get("imagery"),
            "branding": d.get("branding"),
            "graphic_elements": d.get("graphic_elements"),
            "informational_structure": d.get("informational_structure"),
            "psychological_hooks": d.get("psychological_hooks"),
            "marketer_note_raw": d.get("marketer_note_raw"),
            "marketer_element_feedback": d.get("marketer_element_feedback"),
            "summary": d.get("summary"),
        })
    return json.dumps(blocks, ensure_ascii=False, indent=1)


def load_brief_history(db_path: Path, branch: str):
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS generated_briefs ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, brief_id TEXT UNIQUE NOT NULL, branch TEXT NOT NULL, "
        "model_used TEXT NOT NULL, input_creative_ids TEXT NOT NULL, output_json TEXT NOT NULL, created_at TEXT NOT NULL)"
    )
    rows = conn.execute(
        "SELECT brief_id, output_json FROM generated_briefs WHERE branch = ? ORDER BY created_at",
        (branch,),
    ).fetchall()
    conn.close()
    # Только идея (ниша + однострочная концепция) — раньше сюда же тянулись archetype/
    # structure_type/connector_style/background_texture, что раздувало контекст ненужными
    # деталями. Для анти-повтора достаточно знать, какие идеи уже были опробованы.
    history = []
    for brief_id, output_json in rows:
        d = json.loads(output_json)
        history.append({
            "brief_id": brief_id,
            "niche": d.get("target_audience_niche", ""),
            "concept_summary": d.get("concept_summary", ""),
        })
    return history


def find_niche_duplicate(niche: str, history: list, threshold: float = 0.82):
    niche_norm = niche.strip().lower()
    for h in history:
        ratio = difflib.SequenceMatcher(None, niche_norm, h["niche"].strip().lower()).ratio()
        if ratio >= threshold:
            return h
    return None


def build_mode_schema(full_schema: dict, mode: str) -> dict:
    """Полная схема минус промпт-поля других режимов — модель видит и заполняет только одно."""
    import copy
    reduced = copy.deepcopy(full_schema)
    target_field = MODES[mode]
    other_fields = [f for f in MODES.values() if f != target_field]
    for f in other_fields:
        reduced["properties"].pop(f, None)
    reduced["required"] = [r for r in reduced["required"] if r not in other_fields]
    return reduced


def call_synthesis_model(model: str, api_key: str, branch: str, evidence_json: str,
                          archetype_stats: str, history: list, mode: str, hint: str,
                          mode_schema: dict, max_retries: int) -> dict:
    if history:
        history_lines = "\n".join(f"- {h['niche']}: {h['concept_summary']}" for h in history)
        history_block = (
            "\n\nIDEAS ALREADY GENERATED for this branch — don't repeat these niches/concepts, "
            f"treat this as a portfolio of experiments, not one optimal answer:\n{history_lines}\n"
        )
    else:
        history_block = ""

    hint_block = f"\n\nMARKETER GUIDANCE for this specific run — follow it unless it conflicts with a hard constraint below:\n\"{hint.strip()}\"\n" if hint and hint.strip() else ""

    system_prompt = (
        "You are a senior performance-creative strategist. You are given a database "
        f"of already-launched static creatives for branch='{branch}', each with a "
        "human performance rating (1=poor, 2=good, 3=excellent) and a structural "
        "breakdown (text blocks with roles, composition, color, typography, graphic "
        "elements, step-list structure, psychological hooks). Some creatives also "
        "have marketer_element_feedback — direct human feedback on specific elements "
        "(worked_well/worked_poorly). Treat it as a stronger signal than the overall "
        "score when present. The ARCHETYPE DISTRIBUTION below (computed directly from the "
        "database, not your own count) shows how often each archetype appears here and how "
        "it scores on average.\n\n"
        f"ARCHETYPE DISTRIBUTION for branch='{branch}':\n{archetype_stats}\n\n"
        "Task:\n"
        "1. Default to the DOMINANT archetype/structure above (the one with the most "
        "creatives and/or the best average score) as the structural template for this "
        "concept — most new concepts should closely mirror its proven layout, not invent "
        "a different one. Within that structure, pull concrete reusable details (headline "
        "style, icon choices, CTA phrasing, color palette, specific tools/copy) from the "
        "individual score=3 / worked_well creatives in the database below, and note what "
        "you avoided from score=1 / worked_poorly ones.\n"
        "2. Vary the SURFACE for this run — the niche, copy, specific tools/icons "
        "referenced, and color accent — to fit the new audience. Only change the "
        "underlying archetype/structure away from the dominant one if the marketer hint "
        "below explicitly asks for a different style, or if the dominant archetype's "
        "average score is clearly worse than an alternative's. Note in "
        "reasoning.new_experiment what you adapted for this niche.\n"
        "3. Assemble a NEW vertical creative concept with real final copy (no "
        f"placeholders) and explicitly state target_audience_niche.{history_block}"
        f"{hint_block}\n"
        "4. Never add the Nexera logo or the small-print AI disclaimer to the image "
        "prompt or the copy — the designer adds those separately afterward. "
        "Third-party app/tool logos and names (e.g. ChatGPT, Notion, Slack) ARE "
        "allowed and often a strong hook — only Nexera's own branding is excluded.\n"
        f"5. Write ONE ready-to-use image-generation prompt for mode='{mode}'. Follow "
        f"its rules literally:\n{MODE_RULES[mode]}\n"
        "6. Fill `reasoning` with three SHORT entries (1-3 sentences each, not a "
        "list): reused (with inline creative_id refs), avoided (with inline refs), "
        "new_experiment (what you adapted for this niche and why it should still work).\n\n"
        "Reply with EXACTLY one JSON object matching the following JSON Schema "
        "(draft-07), with no explanation and no markdown fencing:\n\n"
        f"{json.dumps(mode_schema, ensure_ascii=False)}"
    )
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": f"Full creative records (branch={branch}):\n{evidence_json}"},
    ]

    validator = Draft7Validator(mode_schema)
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    last_error = None

    for attempt in range(1, max_retries + 1):
        payload = {
            "model": model,
            "messages": messages,
            "temperature": 0.6,
            "max_tokens": 4096,  # actual output is one JSON brief (~1-3k tokens); without this,
            # OpenRouter reserves a model-specific default (65536 for Sonnet 5) and refuses the
            # request if the account can't cover that reservation, even though real usage is tiny.
            "response_format": {"type": "json_object"},
        }
        resp = requests.post(OPENROUTER_URL, headers=headers, json=payload, timeout=180)
        if resp.status_code != 200:
            last_error = f"HTTP {resp.status_code}: {resp.text[:500]}"
            messages.append({"role": "user", "content": f"Request failed ({last_error}). Retry, strictly valid JSON."})
            continue

        raw = resp.json()["choices"][0]["message"]["content"]
        try:
            candidate = json.loads(raw)
        except json.JSONDecodeError:
            import re
            match = re.search(r"\{.*\}", raw, re.DOTALL)
            candidate = json.loads(match.group(0)) if match else None

        if candidate is None:
            last_error = "could not parse JSON from the model's response"
            messages.append({"role": "assistant", "content": raw})
            messages.append({"role": "user", "content": "That is not valid JSON. Return only one correct JSON object."})
            continue

        errors = sorted(validator.iter_errors(candidate), key=lambda e: e.path)
        if errors:
            last_error = "; ".join(f"{'.'.join(str(p) for p in e.path)}: {e.message}" for e in errors[:8])
            messages.append({"role": "assistant", "content": json.dumps(candidate, ensure_ascii=False)})
            messages.append({"role": "user", "content": f"The JSON does not pass schema validation: {last_error}. Return the corrected full JSON object."})
            continue

        duplicate = find_niche_duplicate(candidate.get("target_audience_niche", ""), history)
        if duplicate is not None:
            last_error = f"target_audience_niche '{candidate.get('target_audience_niche')}' is too similar to one already used in brief {duplicate['brief_id']} ('{duplicate['niche']}')"
            messages.append({"role": "assistant", "content": json.dumps(candidate, ensure_ascii=False)})
            messages.append({"role": "user", "content": f"{last_error}. Pick a noticeably different audience niche and rework the concept for it, then return the full JSON object again."})
            continue

        return candidate

    raise RuntimeError(f"Не удалось получить валидный уникальный JSON за {max_retries} попыток. Последняя ошибка: {last_error}")


def init_briefs_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS generated_briefs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            brief_id TEXT UNIQUE NOT NULL,
            branch TEXT NOT NULL,
            model_used TEXT NOT NULL,
            input_creative_ids TEXT NOT NULL,
            output_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.commit()


def run_generation(branch: str, api_key: str, mode: str = "gpt_image_2", hint: str = "",
                    include_history: bool = True, db_path: Path = None, schema_path: Path = None,
                    output_dir: Path = None, model: str = DEFAULT_MODEL, max_retries: int = 4,
                    min_creatives: int = 3, log=print) -> dict:
    """Основная логика генерации, переиспользуемая и CLI (main), и review_server.py."""
    if mode not in MODES:
        raise RuntimeError(f"Неизвестный mode='{mode}', ожидается один из {list(MODES)}")

    db_path = db_path or (PROJECT_ROOT / "db" / "creatives.db")
    schema_path = schema_path or (PROJECT_ROOT / "schema" / "generation_schema.json")
    output_dir = output_dir or (PROJECT_ROOT / "generated")

    creatives = load_branch_creatives(db_path, branch)
    if len(creatives) < min_creatives:
        raise RuntimeError(f"В branch='{branch}' только {len(creatives)} креативов в базе (нужно минимум {min_creatives}). Сначала запустите ingest_creatives.py.")

    scores = [c["score"] for c in creatives]
    log(f"branch={branch}, mode={mode}: {len(creatives)} креативов в базе (score 1/2/3 = {scores.count(1)}/{scores.count(2)}/{scores.count(3)})")

    full_schema = json.loads(Path(schema_path).read_text())
    mode_schema = build_mode_schema(full_schema, mode)
    evidence_json = build_evidence_payload(creatives)
    archetype_stats = build_archetype_stats(creatives)
    log(f"Распределение архетипов:\n{archetype_stats}")

    history = load_brief_history(db_path, branch) if include_history else []
    if not include_history:
        log("История прошлых брифов отключена — проверка на повтор ниши не выполняется.")
    elif history:
        log(f"В истории branch уже {len(history)} брифов — попрошу модель не повторять нишу/идею.")
    if hint:
        log(f"Подсказка маркетолога: {hint}")

    log(f"Запрашиваю синтез у {model}...")
    brief = call_synthesis_model(model, api_key, branch, evidence_json, archetype_stats, history, mode, hint, mode_schema, max_retries)

    brief_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()

    conn = sqlite3.connect(db_path)
    init_briefs_table(conn)
    conn.execute(
        "INSERT INTO generated_briefs (brief_id, branch, model_used, input_creative_ids, output_json, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (brief_id, branch, model, json.dumps([c["creative_id"] for c in creatives]), json.dumps(brief, ensure_ascii=False), now),
    )
    conn.commit()
    conn.close()

    branch_output_dir = Path(output_dir) / branch
    branch_output_dir.mkdir(parents=True, exist_ok=True)
    output_path = branch_output_dir / f"{brief_id}.json"
    output_path.write_text(json.dumps(brief, ensure_ascii=False, indent=2))

    log(f"\nГотово. brief_id={brief_id}")
    return {"brief_id": brief_id, "brief": brief, "output_path": str(output_path), "mode": mode}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--branch", required=True, choices=["certif", "claude", "aigen"])
    parser.add_argument("--mode", default="gpt_image_2", choices=list(MODES))
    parser.add_argument("--hint", default="", help="Свободная подсказка маркетолога для этого запуска")
    parser.add_argument("--no-history", action="store_true", help="Не передавать модели историю прошлых брифов (без анти-повтора ниши/архетипа)")
    parser.add_argument("--db", default=str(PROJECT_ROOT / "db" / "creatives.db"))
    parser.add_argument("--schema", default=str(PROJECT_ROOT / "schema" / "generation_schema.json"))
    parser.add_argument("--output-dir", default=str(PROJECT_ROOT / "generated"))
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--max-retries", type=int, default=4)
    parser.add_argument("--min-creatives", type=int, default=3, help="Минимум креативов в branch, чтобы имело смысл искать паттерны")
    args = parser.parse_args()

    load_env_file(PROJECT_ROOT / ".env")
    import os
    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        sys.exit("OPENROUTER_API_KEY не найден ни в окружении, ни в .env")

    try:
        result = run_generation(
            args.branch, api_key, args.mode, args.hint, not args.no_history, Path(args.db),
            Path(args.schema), Path(args.output_dir), args.model, args.max_retries, args.min_creatives,
        )
    except RuntimeError as e:
        sys.exit(str(e))

    brief = result["brief"]
    prompt_field = MODES[result["mode"]]
    print(f"Сохранено: {Path(result['output_path']).relative_to(PROJECT_ROOT)} и в таблицу generated_briefs")
    print(f"\nНиша: {brief['target_audience_niche']}")
    print(f"Концепция: {brief['concept_summary']}")
    print(f"\nЗаголовок: {brief['new_creative_copy']['main_headline']}")
    print(f"CTA: {brief['new_creative_copy']['cta_text']}")
    print(f"\nЧто нового: {brief['reasoning']['new_experiment']}")
    print(f"\n--- {prompt_field} ---")
    print(brief[prompt_field])


if __name__ == "__main__":
    main()
