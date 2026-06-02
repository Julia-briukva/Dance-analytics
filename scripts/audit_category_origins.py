#!/usr/bin/env python3
"""Audit where report category chips originate from for club dancers."""

from __future__ import annotations

import csv
import json
import re
import sqlite3
from collections import Counter
from pathlib import Path
from typing import Any

import pandas as pd

from analyze_dancer import DEFAULT_DB_PATH, add_derived_columns, resolve_dancer_by_idd, selected_marks_by_internal_id
from analyze_dances import selected_dance_results_by_internal_id
from build_dancer_report import (
    CLASS_GROUP_LABELS,
    DEFAULT_REPORTS_DIR,
    PROJECT_ROOT,
    class_group_from_category,
    with_category_slice,
)


CLUB_CSV = PROJECT_ROOT / "data" / "club_triumph_spb_dancers.csv"
OUTPUT_CSV = PROJECT_ROOT / "reports" / "category_origin_audit.csv"
PROGRAMS = ("standard", "latin")
PAIR_CHECKS = {
    "n": "n_e",
    "d": "e_d",
    "d_c": "eadc",
}
FIELDNAMES = [
    "idd",
    "name",
    "program",
    "category",
    "category_key",
    "source_type",
    "protocol_count",
    "marks_count",
    "results_count",
    "raw_categories",
    "raw_class_groups",
    "report_present",
    "only_from_combined_pair",
    "combined_pair",
    "comment",
]


def clean_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").replace("\xa0", " ")).strip()


def read_club_rows() -> list[dict[str, str]]:
    with CLUB_CSV.open(encoding="utf-8", newline="") as file_obj:
        return [row for row in csv.DictReader(file_obj) if clean_text(row.get("idd"))]


def load_report_slice_keys(idd: str) -> dict[str, set[str]]:
    path = DEFAULT_REPORTS_DIR / f"dancer_{idd}_report.json"
    if not path.exists():
        return {program: set() for program in PROGRAMS}
    report = json.loads(path.read_text(encoding="utf-8"))
    return {
        program: set((report.get("category_slices", {}).get(program, {}) or {}).keys())
        for program in PROGRAMS
    }


def prepare_frame(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    return with_category_slice(add_derived_columns(df))


def subset_counts(df: pd.DataFrame, program: str, category_key: str) -> tuple[int, int, set[str], set[str]]:
    if df.empty or "program" not in df.columns or "category_slice" not in df.columns:
        return 0, 0, set(), set()
    subset = df[(df["program"] == program) & (df["category_slice"] == category_key)].copy()
    if subset.empty:
        return 0, 0, set(), set()
    raw_categories = {clean_text(item) for item in subset.get("category", pd.Series(dtype="object")).dropna() if clean_text(item)}
    raw_groups = {class_group_from_category(item) or "" for item in raw_categories}
    raw_groups.discard("")
    return int(subset["protocol_id"].nunique()), int(len(subset)), raw_categories, raw_groups


def classify_source(
    category_key: str,
    label: str,
    protocol_count: int,
    marks_count: int,
    results_count: int,
    raw_groups: set[str],
    report_present: bool,
) -> str:
    if category_key == "all":
        return "aggregated"
    if protocol_count or marks_count or results_count:
        if label in raw_groups:
            return "raw_protocol"
        return "normalized"
    if report_present:
        return "derived"
    return "derived"


def audit_dancer(conn: sqlite3.Connection, club_row: dict[str, str]) -> list[dict[str, Any]]:
    idd = clean_text(club_row.get("idd"))
    name = clean_text(club_row.get("name"))
    try:
        identity = resolve_dancer_by_idd(conn, idd)
    except Exception as exc:  # noqa: BLE001 - diagnostic script
        return [
            {
                "idd": idd,
                "name": name,
                "program": "",
                "category": "",
                "category_key": "",
                "source_type": "derived",
                "protocol_count": 0,
                "marks_count": 0,
                "results_count": 0,
                "raw_categories": "",
                "raw_class_groups": "",
                "report_present": "no",
                "only_from_combined_pair": "no",
                "combined_pair": "",
                "comment": f"unresolved dancer: {exc}",
            }
        ]

    marks = prepare_frame(selected_marks_by_internal_id(conn, identity.internal_dancer_id))
    results = prepare_frame(selected_dance_results_by_internal_id(conn, identity.internal_dancer_id))
    report_keys = load_report_slice_keys(idd)
    rows: list[dict[str, Any]] = []

    for program in PROGRAMS:
        raw_keys: set[str] = set()
        for df in [marks, results]:
            if not df.empty and "program" in df.columns and "category_slice" in df.columns:
                raw_keys.update(
                    str(item)
                    for item in df[df["program"] == program]["category_slice"].dropna().unique().tolist()
                    if str(item)
                )
        keys = sorted(raw_keys | report_keys.get(program, set()))
        for category_key in keys:
            label = "Все категории" if category_key == "all" else CLASS_GROUP_LABELS.get(category_key, category_key)
            if category_key == "all":
                mark_subset = marks[marks["program"] == program] if not marks.empty and "program" in marks.columns else pd.DataFrame()
                result_subset = results[results["program"] == program] if not results.empty and "program" in results.columns else pd.DataFrame()
                protocols = set()
                if not mark_subset.empty:
                    protocols.update(mark_subset["protocol_id"].dropna().tolist())
                if not result_subset.empty:
                    protocols.update(result_subset["protocol_id"].dropna().tolist())
                protocol_count = len(protocols)
                marks_count = int(len(mark_subset))
                results_count = int(len(result_subset))
                raw_categories = {clean_text(item) for item in mark_subset.get("category", pd.Series(dtype="object")).dropna()}
                raw_categories.update(clean_text(item) for item in result_subset.get("category", pd.Series(dtype="object")).dropna())
                raw_categories.discard("")
                raw_groups = {class_group_from_category(item) or "" for item in raw_categories}
                raw_groups.discard("")
            else:
                mark_protocol_count, marks_count, mark_categories, mark_groups = subset_counts(marks, program, category_key)
                result_protocol_count, results_count, result_categories, result_groups = subset_counts(results, program, category_key)
                protocol_count = len(
                    {
                        *(
                            marks[(marks["program"] == program) & (marks["category_slice"] == category_key)]["protocol_id"].dropna().tolist()
                            if not marks.empty and "program" in marks.columns and "category_slice" in marks.columns
                            else []
                        ),
                        *(
                            results[(results["program"] == program) & (results["category_slice"] == category_key)]["protocol_id"].dropna().tolist()
                            if not results.empty and "program" in results.columns and "category_slice" in results.columns
                            else []
                        ),
                    }
                )
                raw_categories = mark_categories | result_categories
                raw_groups = mark_groups | result_groups

            report_present = category_key in report_keys.get(program, set())
            source_type = classify_source(category_key, label, protocol_count, marks_count, results_count, raw_groups, report_present)
            combined_pair = PAIR_CHECKS.get(category_key, "")
            only_from_combined_pair = (
                bool(combined_pair)
                and label not in raw_groups
                and bool(raw_groups)
                and CLASS_GROUP_LABELS.get(combined_pair, combined_pair) in raw_groups
            )
            comment = ""
            if source_type == "raw_protocol":
                comment = f"{label} found directly in selected dancer protocol categories."
            elif source_type == "aggregated":
                comment = "Aggregate report slice across visible categories."
            elif source_type == "derived":
                comment = "Present in report without selected dancer raw protocol evidence."
            elif source_type == "normalized":
                comment = "Mapped from raw category text by normalization."

            rows.append(
                {
                    "idd": idd,
                    "name": identity.name or name,
                    "program": program,
                    "category": label,
                    "category_key": category_key,
                    "source_type": source_type,
                    "protocol_count": protocol_count,
                    "marks_count": marks_count,
                    "results_count": results_count,
                    "raw_categories": " | ".join(sorted(raw_categories)),
                    "raw_class_groups": ", ".join(sorted(raw_groups)),
                    "report_present": "yes" if report_present else "no",
                    "only_from_combined_pair": "yes" if only_from_combined_pair else "no",
                    "combined_pair": CLASS_GROUP_LABELS.get(combined_pair, combined_pair) if combined_pair else "",
                    "comment": comment,
                }
            )
    return rows


def main() -> int:
    rows: list[dict[str, Any]] = []
    with sqlite3.connect(DEFAULT_DB_PATH) as conn:
        for club_row in read_club_rows():
            rows.extend(audit_dancer(conn, club_row))

    OUTPUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    with OUTPUT_CSV.open("w", encoding="utf-8", newline="") as file_obj:
        writer = csv.DictWriter(file_obj, fieldnames=FIELDNAMES)
        writer.writeheader()
        writer.writerows(rows)

    counts = Counter(row["source_type"] for row in rows)
    split_cases = [row for row in rows if row["only_from_combined_pair"] == "yes"]
    gruzdeva = [row for row in rows if row["idd"] == "2016461"]

    print(f"Rows written: {len(rows)}")
    print(f"CSV: {OUTPUT_CSV.relative_to(PROJECT_ROOT)}")
    for source_type in ["raw_protocol", "normalized", "derived", "aggregated"]:
        print(f"{source_type}: {counts.get(source_type, 0)}")
    print(f"combined-only split cases: {len(split_cases)}")
    print("Gruzdeva 2016461:")
    for row in gruzdeva:
        if row["category_key"] == "all":
            continue
        print(
            f"- {row['program']} {row['category']}: {row['source_type']}; "
            f"protocols={row['protocol_count']}; marks={row['marks_count']}; results={row['results_count']}; "
            f"raw={row['raw_categories']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
