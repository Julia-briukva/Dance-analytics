#!/usr/bin/env python3
"""Audit Compreg profile header data across cache, JSON, and HTML reports."""

from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

from bs4 import BeautifulSoup

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

from compreg_encoding import read_compreg_html_file  # noqa: E402
from compreg_profile import parse_profile_html  # noqa: E402


CLUB_CSV = PROJECT_ROOT / "data" / "club_triumph_spb_dancers.csv"
CACHE_DIR = PROJECT_ROOT / "data" / "cache" / "dancer_profiles"
REPORTS_DIR = PROJECT_ROOT / "reports"
AUDIT_CSV = REPORTS_DIR / "dancer_profile_accuracy_audit.csv"

FIELDS = [
    "name",
    "idd",
    "compreg_cache_path",
    "compreg_status",
    "st_class_compreg",
    "st_skr_compreg",
    "st_norm_compreg",
    "st_json",
    "st_html",
    "la_class_compreg",
    "la_skr_compreg",
    "la_norm_compreg",
    "la_json",
    "la_html",
    "coaches_st_compreg",
    "coaches_st_json",
    "coaches_st_html",
    "coaches_la_compreg",
    "coaches_la_json",
    "coaches_la_html",
    "status",
    "reason",
]


def read_club_rows() -> list[dict[str, str]]:
    with CLUB_CSV.open(encoding="utf-8", newline="") as file_obj:
        return [row for row in csv.DictReader(file_obj) if row.get("idd")]


def report_line_for_program(dancer: dict[str, object], program: str) -> str:
    class_value = dancer.get(f"class_{program}") or ""
    skr_value = dancer.get(f"skr_class_{program}") or ""
    norm_value = dancer.get(f"norm_status_{program}") or ""
    parts = []
    if class_value:
        parts.append(str(class_value))
    if skr_value:
        parts.append(f"СКРкл {skr_value}")
        if norm_value:
            parts.append(str(norm_value))
    return " · ".join(parts)


def profile_line_from_html(lines: list[str], prefix: str) -> str:
    for line in lines:
        if line.startswith(prefix):
            return line.split(":", 1)[1].strip()
    return ""


def main() -> int:
    rows: list[dict[str, str]] = []
    for club_row in read_club_rows():
        idd = club_row["idd"].strip()
        name = club_row.get("name", "").strip()
        cache_path = CACHE_DIR / f"{idd}_danceinfo_post_ci_{idd}.html"
        compreg_profile: dict[str, str] = {}
        compreg_status = "missing_cache"
        if cache_path.exists():
            compreg_profile = parse_profile_html(read_compreg_html_file(cache_path))
            compreg_status = "parsed"

        json_path = REPORTS_DIR / f"dancer_{idd}_report.json"
        html_path = REPORTS_DIR / f"dancer_{idd}_report.html"
        dancer: dict[str, object] = {}
        html_lines: list[str] = []
        if json_path.exists():
            dancer = json.loads(json_path.read_text(encoding="utf-8")).get("dancer", {})
        if html_path.exists():
            soup = BeautifulSoup(html_path.read_text(encoding="utf-8"), "html.parser")
            html_lines = [
                " ".join(node.get_text(" ", strip=True).split())
                for node in soup.select(".dancer-profile-lines div")
            ]

        st_json = report_line_for_program(dancer, "st")
        la_json = report_line_for_program(dancer, "la")
        st_html = profile_line_from_html(html_lines, "St:")
        la_html = profile_line_from_html(html_lines, "La:")
        coaches_st_html = profile_line_from_html(html_lines, "Тренеры St:")
        coaches_la_html = profile_line_from_html(html_lines, "Тренеры La:")

        mismatches = []
        report_available = bool(json_path.exists() and html_path.exists())
        checks = [
            ("st", report_line_for_program(compreg_profile, "st"), st_json, st_html),
            ("la", report_line_for_program(compreg_profile, "la"), la_json, la_html),
            (
                "coaches_st",
                compreg_profile.get("coaches_st", ""),
                str(dancer.get("coaches_st") or ""),
                coaches_st_html,
            ),
            (
                "coaches_la",
                compreg_profile.get("coaches_la", ""),
                str(dancer.get("coaches_la") or ""),
                coaches_la_html,
            ),
        ]
        for label, compreg_value, json_value, html_value in checks:
            if compreg_value != json_value or json_value != html_value:
                mismatches.append(label)

        if not report_available:
            status = "no_report"
            reason = "report JSON/HTML is not available"
        else:
            status = "ok" if not mismatches else "mismatch"
            reason = "; ".join(mismatches)

        rows.append(
            {
                "name": name,
                "idd": idd,
                "compreg_cache_path": str(cache_path.relative_to(PROJECT_ROOT)) if cache_path.exists() else "",
                "compreg_status": compreg_status,
                "st_class_compreg": compreg_profile.get("class_st", ""),
                "st_skr_compreg": compreg_profile.get("skr_class_st", ""),
                "st_norm_compreg": compreg_profile.get("norm_status_st", ""),
                "st_json": st_json,
                "st_html": st_html,
                "la_class_compreg": compreg_profile.get("class_la", ""),
                "la_skr_compreg": compreg_profile.get("skr_class_la", ""),
                "la_norm_compreg": compreg_profile.get("norm_status_la", ""),
                "la_json": la_json,
                "la_html": la_html,
                "coaches_st_compreg": compreg_profile.get("coaches_st", ""),
                "coaches_st_json": str(dancer.get("coaches_st") or ""),
                "coaches_st_html": coaches_st_html,
                "coaches_la_compreg": compreg_profile.get("coaches_la", ""),
                "coaches_la_json": str(dancer.get("coaches_la") or ""),
                "coaches_la_html": coaches_la_html,
                "status": status,
                "reason": reason,
            }
        )

    AUDIT_CSV.parent.mkdir(parents=True, exist_ok=True)
    with AUDIT_CSV.open("w", encoding="utf-8", newline="") as file_obj:
        writer = csv.DictWriter(file_obj, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)

    ok_count = sum(1 for row in rows if row["status"] == "ok")
    no_report_count = sum(1 for row in rows if row["status"] == "no_report")
    mismatch_count = sum(1 for row in rows if row["status"] == "mismatch")
    print(
        f"checked={len(rows)} ok={ok_count} no_report={no_report_count} "
        f"mismatch={mismatch_count} audit={AUDIT_CSV}"
    )
    return 0 if mismatch_count == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
