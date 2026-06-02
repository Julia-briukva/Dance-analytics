#!/usr/bin/env python3
"""Audit category/class-group coverage for club dancer reports."""

from __future__ import annotations

import argparse
import csv
import json
import re
import sqlite3
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import pandas as pd

from analyze_dancer import DEFAULT_DB_PATH, add_derived_columns, resolve_dancer_by_idd, selected_marks_by_internal_id
from analyze_dances import selected_dance_results_by_internal_id
from build_dancer_report import (
    CLASS_GROUP_KEYS,
    CLASS_GROUP_LABELS,
    DEFAULT_REPORTS_DIR,
    PROJECT_ROOT,
    with_category_slice,
)
from dance_display import normalize_dance_code, sort_dance_codes


DEFAULT_CLUB_CSV = PROJECT_ROOT / "data" / "club_triumph_spb_dancers.csv"
DEFAULT_OUTPUT_CSV = PROJECT_ROOT / "reports" / "club_category_audit.csv"
PROGRAMS = ("standard", "latin")
FIELDNAMES = [
    "idd",
    "name",
    "issue_type",
    "program",
    "expected_category",
    "actual_categories",
    "tournament_id",
    "protocol_id",
    "protocol_title",
    "dances",
    "marks_count",
    "results_count",
    "comment",
]


def clean_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").replace("\xa0", " ")).strip()


def read_club_rows(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as file_obj:
        return [row for row in csv.DictReader(file_obj) if clean_text(row.get("idd"))]


def report_paths(idd: str) -> tuple[Path, Path]:
    return DEFAULT_REPORTS_DIR / f"dancer_{idd}_report.json", DEFAULT_REPORTS_DIR / f"dancer_{idd}_report.html"


def load_report_slices(report_path: Path) -> dict[str, set[str]]:
    if not report_path.exists():
        return {program: set() for program in PROGRAMS}
    report = json.loads(report_path.read_text(encoding="utf-8"))
    return {
        program: {key for key in (report.get("category_slices", {}).get(program, {}) or {}) if key != "all"}
        for program in PROGRAMS
    }


def load_report_payload(report_path: Path) -> dict[str, Any]:
    if not report_path.exists():
        return {}
    return json.loads(report_path.read_text(encoding="utf-8"))


def load_html_slices(html_path: Path) -> dict[str, set[str]]:
    if not html_path.exists():
        return {program: set() for program in PROGRAMS}
    html = html_path.read_text(encoding="utf-8", errors="replace")
    result = {program: set() for program in PROGRAMS}
    for program, key in re.findall(r'id="cat-(standard|latin)-([^"]+)"', html):
        if key != "all":
            result[program].add(key)
    return result


def evidence_by_program_category(marks: pd.DataFrame, results: pd.DataFrame) -> dict[tuple[str, str], dict[str, Any]]:
    evidence: dict[tuple[str, str], dict[str, Any]] = {}
    if not marks.empty:
        marks = with_category_slice(marks)
    if not results.empty:
        results = with_category_slice(results)
    keys: set[tuple[str, str]] = set()
    if not marks.empty:
        keys.update(
            (str(row.program), str(row.category_slice))
            for row in marks[["program", "category_slice"]].dropna().drop_duplicates().itertuples(index=False)
            if row.category_slice
        )
    if not results.empty:
        keys.update(
            (str(row.program), str(row.category_slice))
            for row in results[["program", "category_slice"]].dropna().drop_duplicates().itertuples(index=False)
            if row.category_slice
        )
    for program, category_key in sorted(keys):
        mark_subset = (
            marks[(marks["program"] == program) & (marks["category_slice"] == category_key)].copy()
            if not marks.empty
            else pd.DataFrame()
        )
        result_subset = (
            results[(results["program"] == program) & (results["category_slice"] == category_key)].copy()
            if not results.empty
            else pd.DataFrame()
        )
        combined = mark_subset if not mark_subset.empty else result_subset
        by_protocol = []
        for protocol_id, protocol_marks in combined.groupby("protocol_id", dropna=False):
            protocol_results = (
                result_subset[result_subset["protocol_id"] == protocol_id]
                if not result_subset.empty and "protocol_id" in result_subset.columns
                else pd.DataFrame()
            )
            dances = sort_dance_codes(
                {
                    normalize_dance_code(item) or clean_text(item)
                    for item in pd.concat(
                        [
                            protocol_marks.get("dance", pd.Series(dtype="object")),
                            protocol_results.get("dance", pd.Series(dtype="object")),
                        ],
                        ignore_index=True,
                    ).dropna()
                }
            )
            first = protocol_marks.iloc[0] if not protocol_marks.empty else protocol_results.iloc[0]
            by_protocol.append(
                {
                    "tournament_id": clean_text(first.get("tournament_id")),
                    "protocol_id": clean_text(protocol_id),
                    "protocol_title": clean_text(first.get("category")),
                    "dances": ", ".join(dances),
                    "marks_count": int(len(protocol_marks)),
                    "results_count": int(len(protocol_results)),
                }
            )
        evidence[(program, category_key)] = {
            "marks_count": int(len(mark_subset)),
            "results_count": int(len(result_subset)),
            "protocol_count": int(combined["protocol_id"].nunique()) if not combined.empty else 0,
            "tournament_count": int(combined["tournament_id"].nunique()) if not combined.empty and "tournament_id" in combined.columns else 0,
            "protocols": by_protocol,
        }
    return evidence


def category_label(key: str) -> str:
    return CLASS_GROUP_LABELS.get(key) or key


def write_issues(rows: list[dict[str, Any]], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as file_obj:
        writer = csv.DictWriter(file_obj, fieldnames=FIELDNAMES)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in FIELDNAMES})


def audit_dancer(conn: sqlite3.Connection, row: dict[str, str]) -> list[dict[str, Any]]:
    idd = clean_text(row.get("idd"))
    name = clean_text(row.get("name"))
    issues: list[dict[str, Any]] = []
    try:
        identity = resolve_dancer_by_idd(conn, idd)
    except ValueError as exc:
        issues.append(
            {
                "idd": idd,
                "name": name,
                "issue_type": "unresolved_dancer",
                "comment": str(exc),
            }
        )
        return issues

    marks = add_derived_columns(selected_marks_by_internal_id(conn, identity.internal_dancer_id))
    results = selected_dance_results_by_internal_id(conn, identity.internal_dancer_id)
    evidence = evidence_by_program_category(marks, results)
    report_json, report_html = report_paths(idd)
    report_payload = load_report_payload(report_json)
    report_slices = {
        program: {key for key in (report_payload.get("category_slices", {}).get(program, {}) or {}) if key != "all"}
        for program in PROGRAMS
    }
    analysis_ready_slices = {
        program: {
            key
            for key, payload in (report_payload.get("category_slices", {}).get(program, {}) or {}).items()
            if key != "all" and len(payload.get("metrics") or []) > 0
        }
        for program in PROGRAMS
    }
    html_slices = load_html_slices(report_html)

    for trend in report_payload.get("dances", {}).get("trends", []) or []:
        old_value = trend.get("first_avg_place")
        new_value = trend.get("last_avg_place")
        status = trend.get("trend_status")
        try:
            delta = float(new_value) - float(old_value)
        except (TypeError, ValueError):
            continue
        expected_status = "stable" if abs(delta) < 0.001 else ("improving" if delta < 0 else "declining")
        if status != expected_status:
            issues.append(
                {
                    "idd": idd,
                    "name": identity.name,
                    "issue_type": "trend_status_mismatch",
                    "program": trend.get("program"),
                    "expected_category": expected_status,
                    "actual_categories": status,
                    "protocol_title": clean_text(trend.get("dance")),
                    "comment": f"Trend status must follow first→last place: {old_value} → {new_value}.",
                }
            )

    for program in PROGRAMS:
        expected = {
            category
            for (item_program, category), ev in evidence.items()
            if item_program == program
            and (int(ev.get("marks_count") or 0) > 0 or int(ev.get("results_count") or 0) > 0)
        }
        json_actual = report_slices.get(program, set())
        html_actual = html_slices.get(program, set())
        visible_actual = html_actual

        for category_key in sorted((report_slices.get(program, set()) - expected)):
            payload = (report_payload.get("category_slices", {}).get(program, {}) or {}).get(category_key, {})
            ev = payload.get("evidence") or {}
            issues.append(
                {
                    "idd": idd,
                    "name": identity.name,
                    "issue_type": "category_hidden_insufficient_data",
                    "program": program,
                    "expected_category": category_label(category_key),
                    "actual_categories": "",
                    "marks_count": ev.get("marks", 0),
                    "results_count": ev.get("results", 0),
                    "comment": "Category has raw evidence but no dance metrics, so it should not be shown as an active chip.",
                }
            )

        for category_key in sorted(expected - json_actual):
            ev = evidence[(program, category_key)]
            for protocol in ev["protocols"][:5] or [{}]:
                issues.append(
                    {
                        "idd": idd,
                        "name": identity.name,
                        "issue_type": "missing_category_in_json",
                        "program": program,
                        "expected_category": category_label(category_key),
                        "actual_categories": ", ".join(category_label(item) for item in sorted(json_actual)),
                        "marks_count": ev["marks_count"],
                        "results_count": ev["results_count"],
                        "comment": "Category has selected dancer evidence but is absent from report JSON category_slices.",
                        **protocol,
                    }
                )

        for category_key in sorted(expected - html_actual):
            ev = evidence[(program, category_key)]
            for protocol in ev["protocols"][:5] or [{}]:
                issues.append(
                    {
                        "idd": idd,
                        "name": identity.name,
                        "issue_type": "missing_category_in_html",
                        "program": program,
                        "expected_category": category_label(category_key),
                        "actual_categories": ", ".join(category_label(item) for item in sorted(html_actual)),
                        "marks_count": ev["marks_count"],
                        "results_count": ev["results_count"],
                        "comment": "Category has selected dancer evidence but is absent from visible HTML chips.",
                        **protocol,
                    }
                )

        for category_key in sorted(visible_actual - expected):
            issues.append(
                {
                    "idd": idd,
                    "name": identity.name,
                    "issue_type": "category_without_evidence",
                    "program": program,
                    "expected_category": "",
                    "actual_categories": category_label(category_key),
                    "marks_count": 0,
                    "results_count": 0,
                    "comment": "Category is present in report/HTML but no selected dancer marks or results prove this class group.",
                }
            )

        for combined_key, parts in {"n_e": {"n", "e"}, "e_d": {"e", "d"}, "d_c": {"d", "c"}, "c_b": {"c", "b"}}.items():
            if combined_key in expected and combined_key not in json_actual and parts & json_actual:
                ev = evidence[(program, combined_key)]
                issues.append(
                    {
                        "idd": idd,
                        "name": identity.name,
                        "issue_type": "class_group_split",
                        "program": program,
                        "expected_category": category_label(combined_key),
                        "actual_categories": ", ".join(category_label(item) for item in sorted(json_actual)),
                        "marks_count": ev["marks_count"],
                        "results_count": ev["results_count"],
                        "comment": f"{category_label(combined_key)} must remain a whole class group.",
                    }
                )

    return issues


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Audit club dancer category slices against SQLite evidence.")
    parser.add_argument("--club-csv", type=Path, default=DEFAULT_CLUB_CSV)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT_CSV)
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    rows = read_club_rows(args.club_csv)
    issues: list[dict[str, Any]] = []
    with sqlite3.connect(args.db) as conn:
        for row in rows:
            issues.extend(audit_dancer(conn, row))
    write_issues(issues, args.output)
    counts = Counter(row.get("issue_type", "") for row in issues)
    print(f"Dancers checked: {len(rows)}")
    print(f"Issues found: {len(issues)}")
    for issue_type, count in counts.most_common():
        print(f"  {issue_type}: {count}")
    print(f"Audit CSV: {args.output.relative_to(PROJECT_ROOT)}")
    if issues:
        print("First issues:")
        for row in issues[:5]:
            print(
                f"  {row.get('idd')} {row.get('name')} {row.get('program')} "
                f"{row.get('issue_type')} expected={row.get('expected_category')} "
                f"actual={row.get('actual_categories')}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
