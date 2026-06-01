#!/usr/bin/env python3
"""Build dancer reports for all dancers in a club CSV."""

from __future__ import annotations

import argparse
import csv
import json
import sqlite3
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from build_dancer_report import DEFAULT_DB_PATH, DEFAULT_REPORTS_DIR, build_report
from render_html_report import build_view_model, output_path_for_report, render_report


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CLUB_CSV = PROJECT_ROOT / "data" / "club_triumph_spb_dancers.csv"
DEFAULT_STATUS_CSV = PROJECT_ROOT / "reports" / "club_report_build_status.csv"
DEFAULT_LOG_PATH = PROJECT_ROOT / "reports" / "build_club_reports.log"
DEFAULT_INDEX_PATH = PROJECT_ROOT / "reports" / "index.html"
STATUS_FIELDS = ["idd", "name", "status", "report_html", "report_json", "error"]


def display_path(path: Path) -> str:
    try:
        return str(path.relative_to(PROJECT_ROOT))
    except ValueError:
        return str(path)


def read_club_rows(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as file_obj:
        return [row for row in csv.DictReader(file_obj) if (row.get("idd") or "").strip()]


def write_status(rows: list[dict[str, str]], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as file_obj:
        writer = csv.DictWriter(file_obj, fieldnames=STATUS_FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def build_one_report(conn: sqlite3.Connection, idd: str) -> tuple[Path, Path]:
    json_path = DEFAULT_REPORTS_DIR / f"dancer_{idd}_report.json"
    html_path = DEFAULT_REPORTS_DIR / f"dancer_{idd}_report.html"
    report = build_report(conn, idd)
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    view_model = build_view_model(report, json_path, html_path)
    html_path.write_text(render_report(view_model), encoding="utf-8")
    return json_path, html_path


def render_index(existing_html: str, status_rows: list[dict[str, str]]) -> str:
    section_start = "<!-- club-reports:start -->"
    section_end = "<!-- club-reports:end -->"
    items = []
    for row in status_rows:
        name = row.get("name") or row.get("idd") or "Без имени"
        idd = row.get("idd") or ""
        if row.get("status") == "success" and row.get("report_html"):
            items.append(f'<li><a href="{Path(row["report_html"]).name}">{name}</a> <span class="muted">IDD {idd}</span></li>')
        else:
            items.append(f'<li>{name} <span class="muted">IDD {idd} · данных пока нет</span></li>')
    block = (
        f"{section_start}\n"
        "    <section class=\"club-section\">\n"
        "      <h2>Танцоры клуба Триумф</h2>\n"
        "      <p>Список построен по данным Compreg. Если отчёт уже доступен, имя ведёт на HTML-отчёт.</p>\n"
        "      <ul class=\"club-list\">\n"
        f"        {' '.join(items)}\n"
        "      </ul>\n"
        "    </section>\n"
        f"    {section_end}"
    )
    if section_start in existing_html and section_end in existing_html:
        before = existing_html.split(section_start, 1)[0]
        after = existing_html.split(section_end, 1)[1]
        return before + block + after
    return existing_html.replace("</main>", block + "\n  </main>")


def ensure_index_styles(html: str) -> str:
    if ".club-section" in html:
        return html
    styles = """

    .club-section {
      margin-top: 34px;
      background: var(--paper);
      border: 1px solid var(--line);
      border-radius: 18px;
      box-shadow: var(--shadow);
      padding: 24px;
    }

    .club-section h2 {
      margin: 0 0 8px;
      font-size: 22px;
      font-weight: 650;
    }

    .club-list {
      columns: 2;
      margin: 18px 0 0;
      padding-left: 18px;
    }

    .club-list li {
      break-inside: avoid;
      margin: 7px 0;
    }

    .club-list a {
      color: var(--accent-dark);
      font-weight: 650;
      text-decoration: none;
    }

    .club-list a:hover { text-decoration: underline; }

    .muted { color: var(--muted); }

    @media (max-width: 760px) {
      .club-list { columns: 1; }
    }
"""
    return html.replace("    @media (max-width: 760px) {", styles + "\n    @media (max-width: 760px) {")


def update_index(status_rows: list[dict[str, str]], index_path: Path) -> None:
    if not index_path.exists():
        from render_index_page import main as render_index_main

        render_index_main()
    html = index_path.read_text(encoding="utf-8")
    html = ensure_index_styles(html)
    html = render_index(html, status_rows)
    index_path.write_text(html, encoding="utf-8")


def build_club_reports(club_csv: Path, db_path: Path, status_csv: Path, log_path: Path, index_path: Path) -> list[dict[str, str]]:
    rows = read_club_rows(club_csv)
    statuses: list[dict[str, str]] = []
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn, log_path.open("w", encoding="utf-8") as log:
        log.write(f"build_club_reports started_at={datetime.now(timezone.utc).isoformat()}\n")
        log.write(f"club_csv={club_csv}\n")
        log.write(f"rows_with_idd={len(rows)}\n\n")
        for index, row in enumerate(rows, start=1):
            idd = (row.get("idd") or "").strip()
            name = (row.get("name") or "").strip()
            status = {
                "idd": idd,
                "name": name,
                "status": "",
                "report_html": "",
                "report_json": "",
                "error": "",
            }
            log.write(f"[{index}/{len(rows)}] idd={idd} name={name}\n")
            try:
                json_path, html_path = build_one_report(conn, idd)
            except ValueError as exc:
                message = str(exc)
                if "не найден в базе" in message or "Данные по танцору" in message:
                    status["status"] = "no_data"
                else:
                    status["status"] = "error"
                status["error"] = message
                log.write(f"  {status['status']}: {message}\n")
            except Exception as exc:  # noqa: BLE001 - batch job must continue per dancer
                status["status"] = "error"
                status["error"] = str(exc)
                log.write(f"  error: {exc}\n")
                log.write(traceback.format_exc())
            else:
                status["status"] = "success"
                status["report_json"] = display_path(json_path)
                status["report_html"] = display_path(html_path)
                log.write(f"  success: {display_path(html_path)}\n")
            statuses.append(status)
        counts = {key: sum(row["status"] == key for row in statuses) for key in ["success", "no_data", "error"]}
        log.write(f"\nsummary={counts}\n")
    write_status(statuses, status_csv)
    update_index(statuses, index_path)
    return statuses


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build reports for all dancers in a club CSV.")
    parser.add_argument("--club-csv", type=Path, default=DEFAULT_CLUB_CSV)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--status-csv", type=Path, default=DEFAULT_STATUS_CSV)
    parser.add_argument("--log", type=Path, default=DEFAULT_LOG_PATH)
    parser.add_argument("--index", type=Path, default=DEFAULT_INDEX_PATH)
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    statuses = build_club_reports(args.club_csv, args.db, args.status_csv, args.log, args.index)
    counts = {key: sum(row["status"] == key for row in statuses) for key in ["success", "no_data", "error"]}
    print(f"Rows processed: {len(statuses)}")
    print(f"Reports created: {counts['success']}")
    print(f"No data: {counts['no_data']}")
    print(f"Errors: {counts['error']}")
    print(f"Status CSV: {display_path(args.status_csv)}")
    print(f"Log: {display_path(args.log)}")
    print(f"Index: {display_path(args.index)}")
    return 0 if counts["error"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
