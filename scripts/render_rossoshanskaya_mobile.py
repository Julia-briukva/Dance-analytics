#!/usr/bin/env python3
"""Apply mobile cards to 2020091 only; preserve the previous HTML."""
from pathlib import Path
from datetime import datetime
import shutil
import render_html_report as renderer

def main():
    root = Path(__file__).resolve().parents[1]
    source = root / "reports/dancer_2020091_report.json"
    target = source.with_suffix(".html")
    report = renderer.load_report(source)
    renderer.DEFAULT_TEMPLATE_NAME = "report_rossoshanskaya_mobile.html.j2"
    view = renderer.build_view_model(report, source, target)
    for tournament in view["trainer_tournament_summaries"]:
        for entry in tournament.get("category_entries", []):
            for panel in entry.get("judge_panels", []):
                candidates = [row for row in entry["dance_results"]
                              if renderer.normalize_dance_code(row.get("dance_code") or row.get("dance")) == panel["dance"]
                              and row.get("result_source") == "dance_final_place"
                              and row.get("final_place") is not None]
                if panel["round"].strip().casefold() in {"финал", "final"} and len(candidates) == 1:
                    panel["dance_place_label"] = renderer.fmt(candidates[0]["final_place"]) + " место"
    html = renderer.render_report(view)
    backup = root / "backups" / ("rossoshanskaya-mobile-" + datetime.now().strftime("%Y%m%d-%H%M%S-%f"))
    for path in (target, root / target.name):
        if path.exists():
            saved = backup / path.relative_to(root)
            saved.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, saved)
    target.write_text(html, encoding="utf-8")
    shutil.copy2(target, root / target.name)
    print("Готово. Мобильные карточки добавлены только Россошанской (2020091).")
    print("Резервная копия:", backup)
    print("http://localhost:8000/reports/dancer_2020091_report.html?v=mobile1")

if __name__ == "__main__":
    main()
