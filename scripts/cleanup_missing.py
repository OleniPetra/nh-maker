#!/usr/bin/env python3
"""
Находит креативы в db/creatives.db, чьей картинки больше нет в Input/images/
(удалили руками), и по запросу удаляет их из базы вместе с постоянной копией
в library/. Сравнение — по имени файла (normalize_key), как и в ingest_creatives.py.

Без флагов — только список кандидатов на удаление (dry-run). С --delete — удаляет.
"""
import argparse
import sqlite3
import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPTS_DIR.parent
sys.path.insert(0, str(SCRIPTS_DIR))
from ingest_creatives import IMAGE_EXTS, normalize_key  # noqa: E402


def find_orphans(db_path: Path, images_dir: Path):
    present_keys = {normalize_key(p.stem) for p in images_dir.iterdir() if p.suffix.lower() in IMAGE_EXTS} if images_dir.is_dir() else set()
    conn = sqlite3.connect(db_path)
    rows = conn.execute("SELECT creative_id, original_filename, branch, score, library_path FROM creatives").fetchall()
    conn.close()
    return [
        {"creative_id": r[0], "original_filename": r[1], "branch": r[2], "score": r[3], "library_path": r[4]}
        for r in rows if normalize_key(Path(r[1]).stem) not in present_keys
    ]


def delete_orphans(db_path: Path, orphans: list):
    conn = sqlite3.connect(db_path)
    for o in orphans:
        conn.execute("DELETE FROM creatives WHERE creative_id = ?", (o["creative_id"],))
        library_file = PROJECT_ROOT / o["library_path"]
        if library_file.is_file():
            library_file.unlink()
    conn.commit()
    conn.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(PROJECT_ROOT / "db" / "creatives.db"))
    parser.add_argument("--images-dir", default=str(PROJECT_ROOT / "Input" / "images"))
    parser.add_argument("--delete", action="store_true", help="Реально удалить (по умолчанию — только показать список)")
    args = parser.parse_args()

    orphans = find_orphans(Path(args.db), Path(args.images_dir))
    if not orphans:
        print("Осиротевших записей нет — все креативы в базе имеют файл в Input/images/.")
        return

    print(f"Найдено {len(orphans)} записей в базе без файла в Input/images/:")
    for o in orphans:
        print(f"  - {o['original_filename']} (branch={o['branch']}, score={o['score']}, creative_id={o['creative_id']})")

    if args.delete:
        delete_orphans(Path(args.db), orphans)
        print(f"\nУдалено из базы и library/: {len(orphans)}")
    else:
        print("\nЭто был dry-run. Чтобы реально удалить — запустите с флагом --delete.")


if __name__ == "__main__":
    main()
