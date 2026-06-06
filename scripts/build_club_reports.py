#!/usr/bin/env python3
"""Build dancer reports for all dancers in a club CSV."""

from __future__ import annotations

import argparse
import csv
import html
import json
import re
import sqlite3
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from build_dancer_report import DEFAULT_DB_PATH, DEFAULT_REPORTS_DIR, build_report
from deploy_pages_reports import deploy_report
from render_html_report import build_view_model, output_path_for_report, render_report


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CLUB_CSV = PROJECT_ROOT / "data" / "club_triumph_spb_dancers.csv"
DEFAULT_STATUS_CSV = PROJECT_ROOT / "reports" / "club_report_build_status.csv"
DEFAULT_LOG_PATH = PROJECT_ROOT / "reports" / "build_club_reports.log"
DEFAULT_INDEX_PATH = PROJECT_ROOT / "reports" / "index.html"
STATUS_FIELDS = ["idd", "name", "status", "report_html", "report_json", "error"]
PAIR_SEPARATOR_RE = re.compile(r"\s+(?:/|-|–|—)\s+")


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


def is_couple_name(name: str) -> bool:
    return bool(PAIR_SEPARATOR_RE.search(name or ""))


def report_display_name(row: dict[str, str]) -> str:
    report_json = row.get("report_json") or ""
    if report_json:
        path = PROJECT_ROOT / report_json
        if path.exists():
            try:
                report = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                report = {}
            name = ((report.get("dancer") or {}).get("name") or "").strip()
            if name:
                return name
    return (row.get("name") or row.get("idd") or "Без имени").strip()


def report_has_content(report: dict[str, Any]) -> bool:
    summary = report.get("summary") or {}
    if int(summary.get("marks") or 0) > 0:
        return True
    tournaments = report.get("tournaments") or {}
    if tournaments.get("dance_results") or tournaments.get("protocols") or tournaments.get("items"):
        return True
    category_slices = report.get("category_slices") or {}
    for program_slices in category_slices.values():
        for payload in (program_slices or {}).values():
            evidence = payload.get("evidence") or {}
            if int(evidence.get("marks") or 0) > 0 or int(evidence.get("results") or 0) > 0:
                return True
            if payload.get("metrics"):
                return True
    trainer_mode = report.get("trainer_mode") or {}
    if trainer_mode.get("tournament_summaries"):
        return True
    return False


def validate_report_files(report: dict[str, Any], json_path: Path, html_path: Path) -> None:
    if not json_path.exists() or json_path.stat().st_size <= 2:
        raise ValueError("JSON отчёт не создан или пустой.")
    if not html_path.exists() or html_path.stat().st_size <= 128:
        raise ValueError("HTML отчёт не создан или пустой.")
    if not report_has_content(report):
        raise ValueError("Данные по танцору недостаточны для содержательного отчёта.")


def build_one_report(conn: sqlite3.Connection, idd: str) -> tuple[Path, Path]:
    json_path = DEFAULT_REPORTS_DIR / f"dancer_{idd}_report.json"
    html_path = DEFAULT_REPORTS_DIR / f"dancer_{idd}_report.html"
    report = build_report(conn, idd)
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    view_model = build_view_model(report, json_path, html_path)
    html_path.write_text(render_report(view_model), encoding="utf-8")
    validate_report_files(report, json_path, html_path)
    return json_path, html_path


def render_index(existing_html: str, status_rows: list[dict[str, str]]) -> str:
    section_start = "<!-- club-reports:start -->"
    section_end = "<!-- club-reports:end -->"

    def row_html(row: dict[str, str]) -> str:
        idd = html.escape(row.get("idd") or "")
        name = html.escape(report_display_name(row))
        status = row.get("status") or ""
        report_path = PROJECT_ROOT / (row.get("report_html") or "")
        has_report = status == "success" and bool(row.get("report_html")) and report_path.exists() and report_path.stat().st_size > 128
        if has_report:
            report_href = html.escape(Path(row["report_html"]).name)
            name_cell = f'<a class="club-name-link" href="{report_href}">{name}</a>'
            status_label = '<span class="status-pill status-ready">отчёт готов</span>'
        else:
            name_cell = f'<span class="club-name-disabled">{name}</span>'
            status_label = '<span class="status-pill status-empty">данных пока нет</span>'
        return (
          "          <tr>\n"
            f"            <td class=\"club-name\">{name_cell}</td>\n"
            f"            <td><span class=\"idd-value\">{idd}</span> "
            f"<button class=\"copy-idd\" type=\"button\" data-copy=\"{idd}\" title=\"Скопировать IDD\" aria-label=\"Скопировать IDD {idd}\">⧉</button></td>\n"
            f"            <td>{status_label}</td>\n"
            "          </tr>"
        )

    groups = {"solo": [], "couple": []}
    for row in status_rows:
        group = "couple" if is_couple_name(report_display_name(row)) else "solo"
        groups[group].append(row)

    def table_html(title: str, rows: list[dict[str, str]]) -> str:
        report_count = sum(row.get("status") == "success" for row in rows)
        body = "\n".join(row_html(row) for row in rows)
        return (
            "      <section class=\"club-group\">\n"
            f"        <div class=\"club-group-head\"><h3>{title}</h3><span>{len(rows)} записей · {report_count} отчётов</span></div>\n"
            "        <div class=\"club-table-wrap\">\n"
            "        <table class=\"club-table\">\n"
            "          <thead><tr><th>ФИО</th><th>IDD</th><th>Статус</th></tr></thead>\n"
            f"          <tbody>\n{body}\n          </tbody>\n"
            "        </table>\n"
            "        </div>\n"
            "      </section>"
        )

    total = len(status_rows)
    ready = sum(row.get("status") == "success" for row in status_rows)
    no_data = sum(row.get("status") == "no_data" for row in status_rows)
    block = (
        f"{section_start}\n"
        "    <section class=\"club-section\">\n"
        "      <div class=\"club-summary-grid\">\n"
        f"        <div><span class=\"summary-value\">{total}</span><span class=\"summary-label\">всего танцоров и пар</span></div>\n"
        f"        <div><span class=\"summary-value\">{ready}</span><span class=\"summary-label\">отчётов готово</span></div>\n"
        f"        <div><span class=\"summary-value\">{no_data}</span><span class=\"summary-label\">данных пока нет</span></div>\n"
        "      </div>\n"
        f"{table_html('Соло', groups['solo'])}\n"
        f"{table_html('Пары', groups['couple'])}\n"
        "    </section>\n"
        "    <script>\n"
        "      (function () {\n"
        "        function fallbackCopy(text) {\n"
        "          var area = document.createElement('textarea');\n"
        "          area.value = text;\n"
        "          area.setAttribute('readonly', '');\n"
        "          area.style.position = 'fixed';\n"
        "          area.style.left = '-9999px';\n"
        "          document.body.appendChild(area);\n"
        "          area.select();\n"
        "          try { document.execCommand('copy'); } finally { document.body.removeChild(area); }\n"
        "        }\n"
        "        document.querySelectorAll('.copy-idd').forEach(function (button) {\n"
        "          button.addEventListener('click', function () {\n"
        "            var text = button.getAttribute('data-copy') || '';\n"
        "            var done = function () {\n"
        "              var old = button.textContent;\n"
        "              button.textContent = 'Скопировано';\n"
        "              button.classList.add('copied');\n"
        "              window.setTimeout(function () { button.textContent = old; button.classList.remove('copied'); }, 1400);\n"
        "            };\n"
        "            if (navigator.clipboard && navigator.clipboard.writeText) {\n"
        "              navigator.clipboard.writeText(text).then(done, function () { fallbackCopy(text); done(); });\n"
        "            } else { fallbackCopy(text); done(); }\n"
        "          });\n"
        "        });\n"
        "      }());\n"
        "    </script>\n"
        f"    {section_end}"
    )
    if section_start in existing_html and section_end in existing_html:
        before = existing_html.split(section_start, 1)[0]
        after = existing_html.split(section_end, 1)[1]
        return before + block + after
    return existing_html.replace("</main>", block + "\n  </main>")


def ensure_index_styles(html: str) -> str:
    if ".club-table" in html:
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

    .club-summary-grid {
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 12px;
      margin-bottom: 24px;
    }

    .club-summary-grid > div {
      background: var(--accent-light);
      border-radius: 14px;
      padding: 14px;
    }

    .summary-value {
      display: block;
      color: var(--accent-dark);
      font-size: 25px;
      line-height: 1;
      font-weight: 650;
    }

    .summary-label {
      display: block;
      color: var(--muted);
      font-size: 12px;
      margin-top: 6px;
    }

    .club-group {
      margin-top: 24px;
    }

    .club-group-head {
      display: flex;
      align-items: baseline;
      justify-content: space-between;
      gap: 16px;
      margin-bottom: 10px;
    }

    .club-group-head h3 {
      margin: 0;
      font-size: 17px;
      font-weight: 650;
    }

    .club-group-head span {
      color: var(--muted);
      font-size: 13px;
    }

    .club-table-wrap {
      overflow-x: auto;
    }

    .club-table {
      width: 100%;
      border-collapse: collapse;
      font-size: 14px;
    }

    .club-table th {
      color: var(--muted);
      font-size: 12px;
      font-weight: 650;
      text-align: left;
      padding: 8px 10px;
      border-bottom: 1px solid var(--line);
    }

    .club-table td {
      padding: 10px;
      border-bottom: 1px solid rgba(233, 229, 244, .72);
      vertical-align: middle;
    }

    .club-name {
      color: var(--ink);
      font-weight: 560;
    }

    .club-name-link {
      color: var(--ink);
      font-weight: 560;
      text-decoration: none;
    }

    .club-name-link:hover {
      color: var(--accent-dark);
      text-decoration: underline;
    }

    .club-name-disabled {
      color: var(--muted);
      font-weight: 520;
    }

    .idd-value {
      color: var(--ink-soft);
      font-variant-numeric: tabular-nums;
    }

    .status-pill {
      display: inline-flex;
      align-items: center;
      border-radius: 999px;
      padding: 3px 8px;
      font-size: 12px;
      white-space: nowrap;
    }

    .status-ready {
      background: var(--accent-soft);
      color: var(--accent-dark);
    }

    .status-empty {
      background: #F3F2F7;
      color: var(--muted);
    }

    .copy-idd {
      margin-left: 6px;
      border: 0;
      border-radius: 8px;
      background: var(--accent-light);
      color: var(--accent-dark);
      cursor: pointer;
      font: inherit;
      font-size: 12px;
      padding: 3px 7px;
    }

    .copy-idd.copied {
      background: var(--accent-soft);
    }

    .muted { color: var(--muted); }

    @media (max-width: 760px) {
      .club-summary-grid { grid-template-columns: 1fr; }
      .club-group-head { align-items: flex-start; flex-direction: column; gap: 4px; }
      .club-table { min-width: 560px; }
    }
"""
    return html.replace("    @media (max-width: 760px) {", styles + "\n    @media (max-width: 760px) {")


def update_index(status_rows: list[dict[str, str]], index_path: Path) -> None:
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
                deployed = deploy_report(idd)
                log.write(f"  success: {display_path(html_path)}\n")
                log.write(f"  pages: {', '.join(display_path(destination) for _, destination in deployed)}\n")
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
