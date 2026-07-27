#!/usr/bin/env python3
"""
Читаемый вывод результата generate_next_creative.py — без сырого JSON.
Без аргументов показывает самый свежий brief. Можно указать --brief-id или --branch.
"""
import argparse
import json
import sqlite3
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def fetch_brief(db_path: Path, brief_id: str = None, branch: str = None):
    conn = sqlite3.connect(db_path)
    if brief_id:
        row = conn.execute(
            "SELECT brief_id, branch, model_used, created_at, output_json FROM generated_briefs WHERE brief_id = ?",
            (brief_id,),
        ).fetchone()
    elif branch:
        row = conn.execute(
            "SELECT brief_id, branch, model_used, created_at, output_json FROM generated_briefs WHERE branch = ? ORDER BY created_at DESC LIMIT 1",
            (branch,),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT brief_id, branch, model_used, created_at, output_json FROM generated_briefs ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
    conn.close()
    return row


def section(title):
    print(f"\n{'='*70}\n{title}\n{'='*70}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(PROJECT_ROOT / "db" / "creatives.db"))
    parser.add_argument("--brief-id", default=None)
    parser.add_argument("--branch", default=None)
    args = parser.parse_args()

    row = fetch_brief(Path(args.db), args.brief_id, args.branch)
    if row is None:
        raise SystemExit("Ничего не найдено в generated_briefs по этим фильтрам.")

    brief_id, branch, model_used, created_at, output_json = row
    brief = json.loads(output_json)

    section(f"BRIEF {brief_id}  (branch={branch}, model={model_used}, {created_at})")
    print(f"Ниша: {brief.get('target_audience_niche', '-')}")
    print(brief["concept_summary"])

    section("КОПИРАЙТ НОВОГО КРЕАТИВА")
    c = brief["new_creative_copy"]
    print("Заголовок:   ", c["main_headline"])
    print("Подзаголовок:", c.get("subheadline_tagline") or "-")
    print("CTA:         ", c["cta_text"])
    if c.get("step_labels"):
        print("Шаги/уроки:")
        for s in c["step_labels"]:
            print("  -", s)
    if c.get("disclaimer_legal"):
        print("Дисклеймер:  ", c["disclaimer_legal"])

    section("ВЫИГРЫШНЫЕ ПАТТЕРНЫ (score 2-3)")
    for p in brief["winning_patterns"]:
        print(f"- {p['pattern']}")
        print(f"    источники: {', '.join(p['supporting_creative_ids'])}")

    section("ЧЕГО ИЗБЕГАЛИ (score 1)")
    for p in brief["patterns_to_avoid"]:
        print(f"- {p['pattern']}")
        print(f"    источники: {', '.join(p['supporting_creative_ids'])}")

    section("ПРОМПТ — GPT IMAGE 2")
    print(brief["image_prompt_gpt_image_2"])

    section("ПРОМПТ — NANO BANANA 2")
    print(brief["image_prompt_nano_banana_2"])

    section("ОБОСНОВАНИЕ ПО ЭЛЕМЕНТАМ")
    for r in brief["rationale"]:
        print(f"\n[{r['element']}] {r['choice']}")
        print(f"  почему: {r['reasoning']}")
        print(f"  источники: {', '.join(r['supporting_creative_ids'])}")


if __name__ == "__main__":
    main()
