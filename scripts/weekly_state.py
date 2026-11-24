#!/usr/bin/env python3
"""Durable SQLite snapshot + original source pages in a private Git repository."""
from __future__ import annotations
import argparse
import gzip
import hashlib
import json
import shutil
import sqlite3
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB_REL = Path("database/compreg_spb_2025_2026.sqlite")
REQUIRED = {"tournaments", "protocols", "dancers", "marks", "protocol_dancers"}


def validate(path: Path) -> dict:
    if not path.is_file():
        raise ValueError(f"Нет полной базы: {path}")
    with sqlite3.connect(f"file:{path.resolve().as_posix()}?mode=ro", uri=True) as conn:
        if conn.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise ValueError("База повреждена")
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not REQUIRED.issubset(tables):
            raise ValueError("База не содержит обязательные таблицы")
        counts = {name: conn.execute(f"SELECT count(*) FROM {name}").fetchone()[0] for name in sorted(REQUIRED)}
        if not counts["marks"] or not counts["protocols"]:
            raise ValueError("Нельзя использовать пустую базу вместо истории")
        return counts


def sha(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def compress(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    with source.open("rb") as src, target.open("wb") as out:
        with gzip.GzipFile(filename="", fileobj=out, mode="wb", mtime=0) as dst:
            shutil.copyfileobj(src, dst)
    if target.stat().st_size > 95 * 1024 * 1024:
        raise ValueError(f"Файл превышает лимит GitHub: {target.name}")


def safe_cache_path(root: Path, value: str) -> Path:
    path = root / value
    cache = (root / "data/cache").resolve()
    if not path.resolve().is_relative_to(cache):
        raise ValueError("Путь исходника вне data/cache")
    return path


def save(state: Path, root: Path = ROOT) -> None:
    source = root / DB_REL
    validate(source)
    state.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        snapshot = Path(tmp) / "snapshot.sqlite"
        with sqlite3.connect(f"file:{source.resolve().as_posix()}?mode=ro", uri=True) as src, sqlite3.connect(snapshot) as dst:
            src.backup(dst)
        counts = validate(snapshot)
        compress(snapshot, state / "database.sqlite.gz")
        digest = sha(snapshot)
    with sqlite3.connect(source) as conn:
        paths = {r[0] for r in conn.execute("SELECT protocol_cache_path FROM protocols WHERE protocol_cache_path IS NOT NULL")}
        paths.update(r[0] for r in conn.execute("SELECT listcat_cache_path FROM tournaments WHERE listcat_cache_path IS NOT NULL"))
    # Assigned classes/coaches are read by the report builder from these cached cards.
    paths.update(str(p.relative_to(root)) for p in (root / "data/cache/dancer_profiles").glob("*.html"))
    for value in sorted(paths):
        path = safe_cache_path(root, value)
        if path.is_file():
            dest = state / "raw" / Path(value).relative_to("data/cache")
            compress(path, Path(str(dest) + ".gz"))
    manifest = {"version": 1, "database_sha256": digest, "counts": counts}
    (state / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def restore(state: Path, root: Path = ROOT) -> None:
    manifest = json.loads((state / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("version") != 1:
        raise ValueError("Неизвестная версия снимка базы")
    target = root / DB_REL
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(".incoming")
    try:
        with gzip.open(state / "database.sqlite.gz", "rb") as src, temporary.open("wb") as dst:
            shutil.copyfileobj(src, dst)
        if sha(temporary) != manifest["database_sha256"]:
            raise ValueError("Контрольная сумма базы не совпадает")
        counts = validate(temporary)
        if counts != manifest["counts"]:
            raise ValueError("Снимок базы не соответствует описанию")
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)
    for source in (state / "raw").rglob("*.gz"):
        relative = source.relative_to(state / "raw").with_suffix("")
        dest = safe_cache_path(root, "data/cache/" + relative.as_posix())
        dest.parent.mkdir(parents=True, exist_ok=True)
        with gzip.open(source, "rb") as src, dest.open("wb") as dst:
            shutil.copyfileobj(src, dst)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("action", choices=["save", "restore", "validate"])
    p.add_argument("--state", type=Path, required=True)
    args = p.parse_args()
    if args.action == "validate":
        print(validate(ROOT / DB_REL))
    elif args.action == "save":
        save(args.state)
    else:
        restore(args.state)

if __name__ == "__main__":
    main()
