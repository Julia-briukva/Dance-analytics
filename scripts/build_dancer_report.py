#!/usr/bin/env python3
"""Build a structured JSON report for one dancer."""

from __future__ import annotations

import argparse
import json
import math
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from analyze_dancer import (
    DEFAULT_DB_PATH,
    add_derived_columns,
    ensure_analytics_view,
    final_judge_stats,
    judge_stats_by_program,
    resolve_dancer_by_idd,
    selected_marks_by_internal_id,
)
from analyze_dances import (
    dance_summary,
    dynamics_by_date,
    incomplete_protocol_warnings,
    missing_dances,
    ranking_tables,
    selected_dance_results_by_internal_id,
    selected_numeric_marks_by_internal_id,
    trend_ranking,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REPORTS_DIR = PROJECT_ROOT / "reports"
ANALYTICS_VERSION = "report_layer_v1"
JUDGE_REPORT_MIN_MARKS = 10


def clean_value(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, list):
        return [clean_value(item) for item in value]
    if isinstance(value, tuple):
        return [clean_value(item) for item in value]
    if isinstance(value, dict):
        return {key: clean_value(item) for key, item in value.items()}
    if isinstance(value, pd.Timestamp):
        return None if pd.isna(value) else value.strftime("%Y-%m-%d")
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return None
        return value
    if pd.isna(value):
        return None
    if hasattr(value, "item"):
        return clean_value(value.item())
    return value


def df_records(df: pd.DataFrame, max_rows: int | None = None) -> list[dict[str, Any]]:
    if df.empty:
        return []
    view = df.head(max_rows) if max_rows is not None else df
    return [{key: clean_value(value) for key, value in row.items()} for row in view.to_dict(orient="records")]


def first_record(df: pd.DataFrame) -> dict[str, Any] | None:
    records = df_records(df, max_rows=1)
    return records[0] if records else None


def selected_all_marks(conn: sqlite3.Connection, internal_dancer_id: int) -> pd.DataFrame:
    ensure_analytics_view(conn)
    marks = add_derived_columns(selected_marks_by_internal_id(conn, internal_dancer_id))
    if marks.empty:
        return marks
    marks["event_date"] = pd.to_datetime(marks["event_date"], errors="coerce")
    return marks


def protocol_status_warnings(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return df_records(
        pd.read_sql_query(
            """
            SELECT status, message, COUNT(*) AS protocols
            FROM protocol_parse_status
            WHERE status != 'parsed'
            GROUP BY status, message
            ORDER BY status, protocols DESC;
            """,
            conn,
        )
    )


def build_tournament_payload(marks: pd.DataFrame) -> dict[str, Any]:
    tournaments = (
        marks[
            [
                "event_date",
                "tournament_id",
                "tournament_title",
                "protocol_id",
                "category",
                "program",
                "entry_type",
            ]
        ]
        .drop_duplicates()
        .sort_values(["event_date", "protocol_id"])
    )
    by_tournament = (
        tournaments.groupby(["event_date", "tournament_id", "tournament_title"], dropna=False)
        .agg(
            protocols=("protocol_id", "nunique"),
            categories=("category", lambda items: sorted(set(str(item) for item in items if pd.notna(item)))),
            programs=("program", lambda items: sorted(set(str(item) for item in items if pd.notna(item)))),
        )
        .reset_index()
        .sort_values(["event_date", "tournament_id"])
    )
    return {
        "count": int(marks["tournament_id"].nunique()),
        "protocol_count": int(marks["protocol_id"].nunique()),
        "items": df_records(by_tournament),
        "protocols": df_records(tournaments),
    }


def program_payload(program: str, summary: pd.DataFrame, trends: pd.DataFrame) -> dict[str, Any]:
    program_summary = summary[summary["program"] == program].copy()
    program_trends = trends[trends["program"] == program].copy()
    tables = ranking_tables(program_summary, program_trends)
    return {
        "metrics": df_records(program_summary),
        "best_by_final_average": first_record(tables["best_by_final_average"]),
        "best_by_median": first_record(tables["best_by_median"]),
        "most_stable_dance": first_record(tables["most_stable"]),
        "best_peak": first_record(tables["best_peak"]),
        "worst_by_final_average": first_record(tables["worst_by_final_average"]),
        "judge_level_best": first_record(tables["judge_level_best"]),
        "strongest_dance": first_record(tables["best_by_final_average"]),
        "weakest_dance": first_record(tables["worst_by_final_average"]),
        "most_improved_dance": first_record(tables["improvement"]),
    }


def build_judges_payload(numeric: pd.DataFrame) -> dict[str, Any]:
    stats = final_judge_stats(numeric)
    reportable = stats[stats["n_marks"] >= JUDGE_REPORT_MIN_MARKS].copy() if not stats.empty else stats
    by_program = judge_stats_by_program(numeric)
    payload = {
        "strictest": df_records(reportable.sort_values(["strictness", "n_marks"], ascending=[False, False]), max_rows=10),
        "softest": df_records(reportable.sort_values(["softness", "n_marks"], ascending=[False, False]), max_rows=10),
        "low_confidence": df_records(stats[stats["n_marks"] < JUDGE_REPORT_MIN_MARKS].sort_values(["strictness", "n_marks"], ascending=[False, False]))
        if not stats.empty
        else [],
        "by_program": {},
    }
    for program in ["standard", "latin"]:
        subset = by_program[(by_program["program"] == program) & (by_program["n_marks"] >= JUDGE_REPORT_MIN_MARKS)].copy()
        payload["by_program"][program] = {
            "strictest": df_records(subset.sort_values(["strictness", "n_marks"], ascending=[False, False]), max_rows=10),
            "softest": df_records(subset.sort_values(["softness", "n_marks"], ascending=[False, False]), max_rows=10),
        }
    return payload


def ranking_mismatch_warnings(summary: pd.DataFrame) -> list[dict[str, Any]]:
    warnings: list[dict[str, Any]] = []
    if summary.empty or "judge_avg_place" not in summary.columns:
        return warnings
    for program in ["standard", "latin"]:
        subset = summary[summary["program"] == program].copy()
        if subset.empty:
            continue
        performance_rank = subset.sort_values(["final_avg_place", "n_marks"], ascending=[True, False])["dance"].tolist()
        judge_rank = subset.sort_values(["judge_avg_place", "judge_marks"], ascending=[True, False])["dance"].tolist()
        if performance_rank != judge_rank:
            warnings.append(
                {
                    "program": program,
                    "message": "Судейская и результативная метрики дают разные рейтинги танцев.",
                    "performance_ranking": performance_rank,
                    "judge_ranking": judge_rank,
                }
            )
    return warnings


def build_warnings_payload(conn: sqlite3.Connection, marks: pd.DataFrame, summary: pd.DataFrame, numeric: pd.DataFrame) -> dict[str, Any]:
    low_confidence_dances = summary[summary["confidence"] != "ok"].copy() if not summary.empty else pd.DataFrame()
    category_mix = (
        marks[["program", "entry_type", "category", "protocol_id"]]
        .drop_duplicates()
        .groupby(["program", "entry_type", "category"], as_index=False)
        .agg(protocols=("protocol_id", "nunique"))
        .sort_values(["program", "entry_type", "category"])
    )
    return {
        "parser_status": protocol_status_warnings(conn),
        "low_confidence_dances": df_records(low_confidence_dances),
        "missing_dances": df_records(missing_dances(numeric)),
        "incomplete_protocols": df_records(incomplete_protocol_warnings(numeric)),
        "ranking_mismatch": ranking_mismatch_warnings(summary),
        "category_mix": df_records(category_mix),
        "notes": [
            "Performance dance analytics use final_avg_place: one dance result per protocol, round, and dance.",
            "judge_avg_place is diagnostic and is not used for strongest/weakest/stability/trend rankings.",
            "Cross analytics are kept separate and are not mixed into place metrics.",
            "Parser intentionally skips unsupported FKT/EADC raw strings when judge-position mapping is not validated.",
        ],
    }


def build_report(conn: sqlite3.Connection, compreg_idd: str | int) -> dict[str, Any]:
    identity = resolve_dancer_by_idd(conn, compreg_idd)
    marks = selected_all_marks(conn, identity.internal_dancer_id)
    if marks.empty:
        raise ValueError(f"Данные по танцору с Compreg IDD {identity.compreg_idd} не найдены.")

    numeric = selected_numeric_marks_by_internal_id(conn, identity.internal_dancer_id)
    dance_results = selected_dance_results_by_internal_id(conn, identity.internal_dancer_id)
    summary = dance_summary(numeric, dance_results)
    dynamics = dynamics_by_date(dance_results)
    trends = trend_ranking(dynamics)

    numeric_all = marks[marks["mark_type"] == "numeric_place"].dropna(subset=["numeric_mark"]).copy()
    crosses = marks[marks["mark_type"] == "cross"].copy()

    report = {
        "metadata": {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "analytics_version": ANALYTICS_VERSION,
            "database": str(DEFAULT_DB_PATH.relative_to(PROJECT_ROOT)),
        },
        "dancer": {
            "internal_dancer_id": identity.internal_dancer_id,
            "idd": identity.compreg_idd,
            "name": identity.name,
            "club": clean_value(identity.club),
            "city": clean_value(identity.city),
        },
        "summary": {
            "protocols": int(marks["protocol_id"].nunique()),
            "tournaments": int(marks["tournament_id"].nunique()),
            "judges": int(marks["judge_id"].nunique()),
            "marks": int(len(marks)),
            "numeric_place_marks": int(len(numeric_all)),
            "numeric_final_marks": int(len(numeric)),
            "cross_marks": int(len(crosses)),
            "date_from": clean_value(marks["event_date"].min()),
            "date_to": clean_value(marks["event_date"].max()),
        },
        "programs": {
            "standard": program_payload("standard", summary, trends),
            "latin": program_payload("latin", summary, trends),
        },
        "judges": build_judges_payload(numeric),
        "dances": {
            "metrics": df_records(summary),
            "trends": df_records(trends),
            "dynamics_by_date": df_records(dynamics),
        },
        "tournaments": build_tournament_payload(marks),
        "warnings": build_warnings_payload(conn, marks, summary, numeric),
    }
    return report


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build a structured JSON dancer report.")
    parser.add_argument("--idd", required=True, help="External Compreg dancer IDD.")
    parser.add_argument("--db-path", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--output", type=Path, default=None)
    if not argv:
        parser.print_help(sys.stderr)
        raise SystemExit(2)
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    output_path = args.output or (DEFAULT_REPORTS_DIR / f"dancer_{args.idd}_report.json")
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with sqlite3.connect(args.db_path) as conn:
        report = build_report(conn, args.idd)

    output_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {output_path.relative_to(PROJECT_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
