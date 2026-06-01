#!/usr/bin/env python3
"""Build a structured JSON report for one dancer."""

from __future__ import annotations

import argparse
import json
import math
import re
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
from dance_display import DANCE_CODE_ORDER, normalize_dance_code, sort_dance_codes


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REPORTS_DIR = PROJECT_ROOT / "reports"
ANALYTICS_VERSION = "report_layer_v1"
JUDGE_REPORT_MIN_MARKS = 10
CATEGORY_SLICE_LABELS = {"all": "Все категории"}
CLASS_GROUP_ORDER = ["N", "N+E", "E", "E+D", "D", "D+C", "C", "C+B", "B", "A", "S", "M", "EADC", "Open"]
CLASS_GROUP_KEYS = {label: label.lower().replace("+", "_") for label in CLASS_GROUP_ORDER}
CLASS_GROUP_LABELS = {key: label for label, key in CLASS_GROUP_KEYS.items()}
AGE_GROUP_PATTERNS = [
    r"Дети-\d(?:\+\d)?",
    r"Ювеналы-\d(?:\+\d)?",
    r"Юниоры-\d(?:\+\d)?",
    r"Молод[её]жь",
    r"Взрослые",
    r"Сеньоры",
]
PROGRAM_LABELS = {"standard": "Стандарт", "latin": "Латина"}


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


def build_tournament_payload(marks: pd.DataFrame, dance_results: pd.DataFrame | None = None) -> dict[str, Any]:
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
    if not marks.empty and "dance" in marks.columns:
        dance_codes = (
            marks.dropna(subset=["dance"])
            .drop_duplicates(["event_date", "tournament_id", "tournament_title", "program", "dance"])
            .groupby(["event_date", "tournament_id", "tournament_title"], dropna=False)
            .agg(dance_codes=("dance", lambda items: sort_dance_codes([normalize_dance_code(item) for item in items])))
            .reset_index()
        )
        by_tournament = by_tournament.merge(dance_codes, on=["event_date", "tournament_id", "tournament_title"], how="left")
        by_tournament["dance_codes"] = by_tournament["dance_codes"].apply(lambda value: value if isinstance(value, list) else [])
    else:
        by_tournament["dance_codes"] = [[] for _ in range(len(by_tournament))]
    return {
        "count": int(marks["tournament_id"].nunique()),
        "protocol_count": int(marks["protocol_id"].nunique()),
        "items": df_records(by_tournament),
        "protocols": df_records(tournaments),
    }


def safe_trend_ranking(dynamics: pd.DataFrame) -> pd.DataFrame:
    if dynamics.empty:
        return pd.DataFrame()
    try:
        trends = trend_ranking(dynamics)
    except TypeError:
        rows: list[dict[str, Any]] = []
        for (program, dance), group in dynamics.groupby(["program", "dance"]):
            ordered = group.sort_values("event_date")
            first = ordered.iloc[0]
            last = ordered.iloc[-1]
            rows.append(
                {
                    "program": program,
                    "dance": dance,
                    "trend_over_time": None,
                    "first_date": first["event_date"],
                    "first_avg_place": first["avg_place"],
                    "last_date": last["event_date"],
                    "last_avg_place": last["avg_place"],
                    "first_to_last_delta": round(float(last["avg_place"] - first["avg_place"]), 3),
                    "n_dates": len(ordered),
                    "confidence": "insufficient trend",
                }
            )
        trends = pd.DataFrame(rows)
    if not trends.empty and "trend_over_time" in trends.columns:
        trends["trend_over_time"] = pd.to_numeric(trends["trend_over_time"], errors="coerce").round(3)
    return trends


def class_group_from_category(category: Any) -> str | None:
    text = str(category or "")
    normalized = re.sub(r"\s+", " ", text.upper().replace("–", "-").replace("—", "-")).strip()
    for label in sorted(CLASS_GROUP_ORDER, key=len, reverse=True):
        pattern_label = re.escape(label.upper())
        if re.search(rf"(?<![A-ZА-ЯЁ0-9+]){pattern_label}(?![A-ZА-ЯЁ0-9+])", normalized):
            return label
    return None


def category_slice_key(category: Any) -> str | None:
    class_group = class_group_from_category(category)
    return CLASS_GROUP_KEYS.get(class_group or "")


def age_group_from_category(category: Any) -> str | None:
    text = str(category or "")
    for pattern in AGE_GROUP_PATTERNS:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            return match.group(0)
    return None


def with_category_slice(df: pd.DataFrame) -> pd.DataFrame:
    result = df.copy()
    if result.empty or "category" not in result.columns:
        result["category_slice"] = None
        result["age_group"] = None
        result["class_group"] = None
        return result
    result["class_group"] = result["category"].map(class_group_from_category)
    result["category_slice"] = result["class_group"].map(lambda value: CLASS_GROUP_KEYS.get(str(value or "")))
    result["age_group"] = result["category"].map(age_group_from_category)
    return result


def program_payload(program: str, summary: pd.DataFrame, trends: pd.DataFrame) -> dict[str, Any]:
    program_summary = summary[summary["program"] == program].copy() if "program" in summary.columns else pd.DataFrame()
    program_trends = trends[trends["program"] == program].copy() if "program" in trends.columns else pd.DataFrame()
    if program_summary.empty:
        return {
            "metrics": [],
            "best_by_final_average": None,
            "best_by_median": None,
            "most_stable_dance": None,
            "best_peak": None,
            "worst_by_final_average": None,
            "judge_level_best": None,
            "strongest_dance": None,
            "weakest_dance": None,
            "most_improved_dance": None,
        }
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


def build_category_slices(numeric: pd.DataFrame, dance_results: pd.DataFrame, marks: pd.DataFrame) -> dict[str, dict[str, Any]]:
    numeric_with_slices = with_category_slice(numeric)
    results_with_slices = with_category_slice(dance_results)
    marks_with_slices = with_category_slice(marks)
    payload: dict[str, dict[str, Any]] = {"standard": {}, "latin": {}}

    for program in ["standard", "latin"]:
        present_slice_keys = {
            str(item)
            for item in pd.concat(
                [
                    numeric_with_slices[numeric_with_slices["program"] == program]["category_slice"],
                    results_with_slices[results_with_slices["program"] == program]["category_slice"],
                    marks_with_slices[marks_with_slices["program"] == program]["category_slice"],
                ],
                ignore_index=True,
            ).dropna()
        }
        ordered_slice_keys = ["all"] + [
            CLASS_GROUP_KEYS[label]
            for label in CLASS_GROUP_ORDER
            if CLASS_GROUP_KEYS[label] in present_slice_keys
        ]
        for slice_key in ordered_slice_keys:
            label = CATEGORY_SLICE_LABELS.get(slice_key) or CLASS_GROUP_LABELS.get(slice_key) or slice_key
            if slice_key == "all":
                slice_numeric = numeric_with_slices[numeric_with_slices["program"] == program].copy()
                slice_results = results_with_slices[results_with_slices["program"] == program].copy()
                slice_marks = marks_with_slices[marks_with_slices["program"] == program].copy()
            else:
                slice_numeric = numeric_with_slices[
                    (numeric_with_slices["program"] == program) & (numeric_with_slices["category_slice"] == slice_key)
                ].copy()
                slice_results = results_with_slices[
                    (results_with_slices["program"] == program) & (results_with_slices["category_slice"] == slice_key)
                ].copy()
                slice_marks = marks_with_slices[
                    (marks_with_slices["program"] == program) & (marks_with_slices["category_slice"] == slice_key)
                ].copy()
                if slice_numeric.empty or slice_results.empty:
                    continue

            if slice_numeric.empty or slice_results.empty:
                slice_summary = pd.DataFrame()
                slice_dynamics = pd.DataFrame()
                slice_trends = pd.DataFrame()
                program_data = program_payload(program, slice_summary, slice_trends)
            else:
                slice_summary = dance_summary(slice_numeric, slice_results)
                slice_dynamics = dynamics_by_date(slice_results)
                slice_trends = safe_trend_ranking(slice_dynamics)
                program_data = program_payload(program, slice_summary, slice_trends)

            program_data.update(
                {
                    "key": slice_key,
                    "label": label,
                    "source": "selected_dancer_marks_and_results",
                    "class_group": None if slice_key == "all" else label,
                    "age_groups": sorted(set(str(item) for item in slice_marks["age_group"].dropna())) if not slice_marks.empty and "age_group" in slice_marks.columns else [],
                    "source_categories": sorted(set(str(item) for item in slice_marks["category"].dropna())) if not slice_marks.empty else [],
                    "protocol_count": int(slice_results["protocol_id"].nunique()) if not slice_results.empty else 0,
                    "tournament_count": int(slice_results["tournament_id"].nunique()) if not slice_results.empty else 0,
                    "evidence": {
                        "tournaments": int(slice_marks["tournament_id"].nunique()) if not slice_marks.empty else 0,
                        "protocols": int(slice_marks["protocol_id"].nunique()) if not slice_marks.empty else 0,
                        "marks": int(len(slice_marks)),
                        "results": int(len(slice_results)),
                    },
                    "dynamics_by_date": df_records(slice_dynamics),
                    "trends": df_records(slice_trends),
                    "tournament_dance_results": df_records(build_tournament_dance_results(slice_results)),
                }
            )
            payload[program][slice_key] = program_data
    return payload


def build_tournament_dance_results(dance_results: pd.DataFrame) -> pd.DataFrame:
    if dance_results.empty:
        return pd.DataFrame()
    result = (
        dance_results.groupby(
            [
                "event_date",
                "tournament_id",
                "tournament_title",
                "protocol_id",
                "category",
                "program",
                "dance",
            ],
            dropna=False,
            as_index=False,
        )
        .agg(
            final_place=("final_place", "mean"),
            best_place=("final_place", "min"),
            worst_place=("final_place", "max"),
            n_results=("final_place", "count"),
            judge_marks=("judge_marks", "sum"),
        )
    )
    for column in ["final_place", "best_place", "worst_place"]:
        result[column] = result[column].round(3)
    result["dance_code"] = result["dance"].map(normalize_dance_code)
    result["_dance_order"] = result["dance_code"].map(lambda code: DANCE_CODE_ORDER.get(code, 999))
    result = result.sort_values(["event_date", "protocol_id", "program", "_dance_order", "dance_code"]).drop(columns=["_dance_order"])
    return result


def tournament_metric_record(row: pd.Series, metric_type: str, role: str | None = None) -> dict[str, Any]:
    value_key = "cross_count" if metric_type == "cross_count" else "final_place"
    value = row[value_key]
    record: dict[str, Any] = {
        "dance": clean_value(row["dance"]),
        "dance_code": clean_value(row["dance_code"]),
        "metric_value": clean_value(round(float(value), 3) if metric_type == "final_place" else int(value)),
        "metric_type": metric_type,
        "metric_label": "крестов судей" if metric_type == "cross_count" else "итоговое место",
        "category": clean_value(row["category"]),
        "protocol_id": clean_value(row["protocol_id"]),
    }
    if metric_type == "cross_count":
        record["cross_count"] = clean_value(int(value))
        record["interpretation"] = "выше всего оценивался судьями" if role == "best" else "ниже всего оценивался судьями"
    else:
        record["final_place"] = clean_value(round(float(value), 3))
        record["interpretation"] = "по итоговым местам"
    return record


def apply_tournament_display_mode(summary: dict[str, Any], metrics: pd.DataFrame, metric_type: str) -> dict[str, Any]:
    value_column = "cross_count" if metric_type == "cross_count" else "final_place"
    metrics = metrics.dropna(subset=[value_column]).copy()
    if metrics.empty:
        summary["display_mode"] = "insufficient_data"
        summary["message"] = "недостаточно данных"
        return summary

    metrics["_dance_order"] = metrics["dance_code"].map(lambda code: DANCE_CODE_ORDER.get(code, 999))
    metrics = metrics.sort_values(["_dance_order", "dance_code"])
    values = [round(float(item), 6) for item in metrics[value_column].tolist()]
    metric_count = len(metrics)

    if metric_count == 1:
        row = metrics.iloc[0]
        summary["data_sufficient"] = True
        summary["display_mode"] = "single_dance"
        summary["evaluated_dance"] = tournament_metric_record(row, metric_type)
        return summary

    if max(values) == min(values):
        value = values[0]
        summary["data_sufficient"] = True
        summary["display_mode"] = "tied"
        summary["tied_dances"] = clean_value(metrics["dance_code"].tolist())
        summary["tied_metric_value"] = clean_value(round(float(value), 3) if metric_type == "final_place" else int(value))
        return summary

    summary["data_sufficient"] = True
    summary["display_mode"] = "best_worst"
    ascending = True if metric_type == "final_place" else False
    best_value = min(values) if metric_type == "final_place" else max(values)
    worst_value = max(values) if metric_type == "final_place" else min(values)
    best_rows = metrics[metrics[value_column].round(6) == best_value].sort_values(["_dance_order", "dance_code"])
    worst_rows = metrics[metrics[value_column].round(6) == worst_value].sort_values(["_dance_order", "dance_code"])
    best_codes = [clean_value(row["dance_code"]) for _, row in best_rows.iterrows()]
    worst_codes = [clean_value(row["dance_code"]) for _, row in worst_rows.iterrows()]
    if set(best_codes) == set(worst_codes):
        value = best_value
        summary["display_mode"] = "tied"
        summary["tied_dances"] = clean_value(metrics["dance_code"].tolist())
        summary["tied_metric_value"] = clean_value(round(float(value), 3) if metric_type == "final_place" else int(value))
        return summary
    best = best_rows.iloc[0]
    worst = worst_rows.iloc[0]
    if len(best_rows) == 1 and len(worst_rows) == 1 and clean_value(best["dance_code"]) == clean_value(worst["dance_code"]):
        summary["display_mode"] = "single_dance"
        summary["evaluated_dance"] = tournament_metric_record(best, metric_type)
        return summary
    summary["best_dances"] = [tournament_metric_record(row, metric_type, role="best") for _, row in best_rows.iterrows()]
    summary["worst_dances"] = [tournament_metric_record(row, metric_type, role="worst") for _, row in worst_rows.iterrows()]
    summary["best_dance"] = summary["best_dances"][0] if summary["best_dances"] else None
    summary["worst_dance"] = summary["worst_dances"][0] if summary["worst_dances"] else None
    return summary


def build_program_tournament_summary(program: str, rows: pd.DataFrame, mark_rows: pd.DataFrame | None = None) -> dict[str, Any]:
    scored = rows.dropna(subset=["final_place"]).copy() if not rows.empty else pd.DataFrame()
    source_rows = mark_rows if mark_rows is not None and not mark_rows.empty else rows
    dance_codes = sort_dance_codes([normalize_dance_code(item) for item in source_rows["dance"].dropna().tolist()]) if not source_rows.empty else []
    scored_dance_count = int(scored["dance"].nunique()) if not scored.empty and "dance" in scored.columns else 0
    protocol_count = int(source_rows["protocol_id"].nunique()) if not source_rows.empty else 0
    summary: dict[str, Any] = {
        "program": program,
        "program_label": PROGRAM_LABELS.get(program, program),
        "dance_codes": dance_codes,
        "overall": {
            "avg_place": round(float(scored["final_place"].mean()), 3) if not scored.empty else None,
            "dance_count": scored_dance_count,
            "protocol_count": protocol_count,
            "dance_codes": dance_codes,
        },
        "metric_type": "final_place" if scored_dance_count >= 2 else None,
        "metric_label": "по итоговым местам" if scored_dance_count >= 2 else None,
        "data_sufficient": False,
        "display_mode": "insufficient_data",
        "best_dance": None,
        "worst_dance": None,
        "best_dances": [],
        "worst_dances": [],
        "evaluated_dance": None,
        "tied_dances": [],
        "tied_metric_value": None,
    }

    if scored_dance_count >= 1:
        summary["metric_type"] = "final_place"
        summary["metric_label"] = "по итоговым местам"
        final_metrics = (
            scored.groupby(["program", "dance_code"], as_index=False)
            .agg(
                dance=("dance", "first"),
                final_place=("final_place", "mean"),
                protocol_id=("protocol_id", "first"),
                category=("category", "first"),
            )
        )
        final_metrics["final_place"] = final_metrics["final_place"].round(3)
        return apply_tournament_display_mode(summary, final_metrics, "final_place")

    cross_rows = source_rows[source_rows["mark_type"] == "cross"].copy() if not source_rows.empty and "mark_type" in source_rows.columns else pd.DataFrame()
    if not cross_rows.empty:
        cross_summary = (
            cross_rows.groupby(["program", "dance"], as_index=False)
            .agg(
                cross_count=("mark", "count"),
                protocol_id=("protocol_id", "first"),
                category=("category", "first"),
            )
        )
        cross_summary["dance_code"] = cross_summary["dance"].map(normalize_dance_code)
        cross_summary["_dance_order"] = cross_summary["dance_code"].map(lambda code: DANCE_CODE_ORDER.get(code, 999))
        if int(cross_summary["dance"].nunique()) >= 1:
            summary["metric_type"] = "cross_count"
            summary["metric_label"] = "по крестам судей"
            summary["overall"]["dance_count"] = int(cross_summary["dance"].nunique())
            return apply_tournament_display_mode(summary, cross_summary, "cross_count")

    summary["message"] = "недостаточно данных"
    return summary


def build_trainer_mode_payload(tournament_dance_results: pd.DataFrame, marks: pd.DataFrame) -> dict[str, Any]:
    if marks.empty:
        return {"tournament_summaries": []}

    summaries: list[dict[str, Any]] = []
    group_keys = ["event_date", "tournament_id", "tournament_title"]
    full_mark_source = marks.dropna(subset=["dance"]).copy()
    mark_source = full_mark_source.drop_duplicates(group_keys + ["protocol_id", "category", "program", "dance"]).copy()
    result_source = tournament_dance_results.copy()
    for key_values, tournament_marks in mark_source.groupby(group_keys, dropna=False):
        event_date, tournament_id, tournament_title = key_values
        tournament_full_marks = full_mark_source[
            (full_mark_source["event_date"] == event_date)
            & (full_mark_source["tournament_id"] == tournament_id)
            & (full_mark_source["tournament_title"] == tournament_title)
        ].copy()
        if result_source.empty:
            tournament_rows = pd.DataFrame()
        else:
            tournament_rows = result_source[
                (result_source["event_date"] == event_date)
                & (result_source["tournament_id"] == tournament_id)
                & (result_source["tournament_title"] == tournament_title)
            ].copy()
        program_summaries = []
        for program in ["standard", "latin"]:
            program_mark_rows = tournament_marks[tournament_marks["program"] == program].copy()
            if program_mark_rows.empty:
                continue
            program_full_mark_rows = tournament_full_marks[tournament_full_marks["program"] == program].copy()
            program_rows = tournament_rows[tournament_rows["program"] == program].copy() if not tournament_rows.empty else pd.DataFrame()
            program_summaries.append(build_program_tournament_summary(program, program_rows, program_full_mark_rows))
        if not program_summaries:
            continue
        dance_codes = sort_dance_codes([normalize_dance_code(item) for item in tournament_marks["dance"].dropna().tolist()])
        summaries.append(
            {
                "event_date": clean_value(event_date),
                "tournament_id": clean_value(tournament_id),
                "tournament_title": clean_value(tournament_title),
                "categories": sorted(set(str(item) for item in tournament_marks["category"].dropna())),
                "programs": sorted(set(str(item) for item in tournament_marks["program"].dropna())),
                "protocols": int(tournament_marks["protocol_id"].nunique()),
                "dance_codes": dance_codes,
                "program_summaries": clean_value(program_summaries),
                "dance_results": df_records(tournament_rows),
            }
        )
    return {"tournament_summaries": sorted(summaries, key=lambda item: item.get("event_date") or "")}


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
    trends = safe_trend_ranking(dynamics)
    tournament_dance_results = build_tournament_dance_results(dance_results)

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
        "category_slices": build_category_slices(numeric, dance_results, marks),
        "tournaments": {
            **build_tournament_payload(marks, dance_results),
            "dance_results": df_records(tournament_dance_results),
        },
        "trainer_mode": build_trainer_mode_payload(tournament_dance_results, marks),
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
