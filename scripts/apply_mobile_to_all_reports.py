#!/usr/bin/env python3
"""Rerender existing dancer JSON files without changing data or the homepage."""
import json
import shutil
import tempfile
from datetime import datetime
from pathlib import Path
from render_html_report import build_view_model, render_report


def main():
    root = Path(__file__).resolve().parents[1]
    reports = root / "reports"
    sources = sorted(reports.glob("dancer_*_report.json"))
    if not sources:
        raise SystemExit("Не найдены JSON-отчёты в reports/")
    backup = root / "backups" / ("mobile-all-html-" + datetime.now().strftime("%Y%m%d-%H%M%S-%f"))
    with tempfile.TemporaryDirectory(prefix="dance-mobile-") as folder:
        staged = []
        for source in sources:
            if source.stat().st_size <= 2:
                print("Нет данных:", source.name, flush=True)
                continue
            report = json.loads(source.read_text(encoding="utf-8"))
            if not report.get("dancer", {}).get("idd"):
                raise ValueError("В отчёте отсутствует IDD: " + source.name)
            target = source.with_suffix(".html")
            html = render_report(build_view_model(report, source, target))
            temp = Path(folder) / target.name
            temp.write_text(html, encoding="utf-8")
            staged.append((temp, target))
            print(f"Подготовлен {len(staged)}: {target.name}", flush=True)
        # Replace HTML only after every report has rendered successfully.
        for temp, target in staged:
            for destination in (target, root / target.name):
                if destination.exists():
                    saved = backup / destination.relative_to(root)
                    saved.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(destination, saved)
                shutil.copy2(temp, destination)
    print("Готово. Отчётов обновлено:", len(staged))
    print("Резервная копия:", backup)
    print("Главная, JSON-данные и SQLite-база не изменены.")


if __name__ == "__main__":
    main()
