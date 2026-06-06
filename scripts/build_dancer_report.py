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
    infer_program,
    judge_stats_by_program,
    resolve_dancer_by_idd,
    selected_marks_by_internal_id,
)
from analyze_dances import (
    confidence_for_dance,
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
VISIBLE_CATEGORY_MIN_MARKS = 20
PANEL_REACTION_SPREAD_THRESHOLD = 0.15
CATEGORY_SLICE_LABELS = {"all": "Все категории"}
CLASS_GROUP_ORDER = ["N", "N+E", "E", "E+D", "D", "D+C", "C", "C+B", "B", "A", "S", "M", "EADC", "Open"]
CLASS_GROUP_KEYS = {label: label.lower().replace("+", "_") for label in CLASS_GROUP_ORDER}
CLASS_GROUP_LABELS = {key: label for label, key in CLASS_GROUP_KEYS.items()}
PARENT_CATEGORY_GROUPS = {
    "n_e": ["n_e", "n"],
    "e_d": ["e_d", "d"],
    "eadc": ["eadc", "d_c"],
}
PARENT_CATEGORY_GROUP_LABELS = {
    "all": "Все категории",
    "n_e": "N+E",
    "e_d": "E+D",
    "eadc": "EADC",
}
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


def parse_place_value(value: Any) -> float | None:
    if value is None or pd.isna(value):
        return None
    text = str(value).strip().replace("–", "-").replace("—", "-").replace(",", ".")
    if not text:
        return None
    numbers = re.findall(r"\d+(?:\.\d+)?", text)
    if not numbers:
        return None
    values = [float(item) for item in numbers]
    if len(values) >= 2 and "-" in text:
        return sum(values[:2]) / 2
    return values[0]


def compact_number_text(value: str | float | int) -> str:
    numeric = float(value)
    rounded = round(numeric)
    if abs(numeric - rounded) < 0.001:
        return str(int(rounded))
    return f"{numeric:.3f}".rstrip("0").rstrip(".")


def place_label(value: Any) -> str | None:
    if value is None or pd.isna(value):
        return None
    text = str(value).strip().replace("—", "–")
    if not text:
        return None
    text = re.sub(r"\s*-\s*", "–", text)
    text = re.sub(r"\d+(?:[.,]\d+)?", lambda match: compact_number_text(match.group(0).replace(",", ".")), text)
    return f"{text} место"


def selected_protocol_results(conn: sqlite3.Connection, internal_dancer_id: int) -> pd.DataFrame:
    rows = pd.read_sql_query(
        """
        SELECT
            p.id AS protocol_db_id,
            p.protocol_id AS protocol_id,
            p.tournament_id AS tournament_id,
            p.tournament_title AS tournament_title,
            p.event_date AS event_date,
            p.city AS city,
            p.category AS category,
            pd.competitor_number AS competitor_number,
            pd.place AS protocol_place
        FROM protocol_dancers pd
        JOIN protocols p ON p.id = pd.protocol_id
        WHERE pd.dancer_id = ?
        ORDER BY p.event_date, p.protocol_id;
        """,
        conn,
        params=(internal_dancer_id,),
    )
    if rows.empty:
        return rows
    rows["event_date"] = pd.to_datetime(rows["event_date"], errors="coerce")
    rows["program"] = rows["category"].map(lambda value: infer_program(None, value))
    rows["protocol_place_value"] = rows["protocol_place"].map(parse_place_value)
    rows["protocol_place_label"] = rows["protocol_place"].map(place_label)
    return rows


def dance_summary_from_results(numeric: pd.DataFrame, dance_results: pd.DataFrame) -> pd.DataFrame:
    if dance_results.empty:
        return pd.DataFrame()
    if not numeric.empty:
        return dance_summary(numeric, dance_results)
    performance = (
        dance_results.groupby(["program", "dance"], as_index=False)
        .agg(
            final_avg_place=("final_place", "mean"),
            median_place=("final_place", "median"),
            std_deviation=("final_place", "std"),
            variance=("final_place", "var"),
            n_marks=("final_place", "count"),
            n_protocols=("protocol_id", "nunique"),
            n_dates=("event_date", "nunique"),
            best_place=("final_place", "min"),
            worst_place=("final_place", "max"),
            categories=("category", lambda items: "; ".join(sorted(set(str(item) for item in items if pd.notna(item))))),
        )
        .sort_values(["program", "dance"])
    )
    performance["avg_place"] = performance["final_avg_place"]
    performance["final_median_place"] = performance["median_place"]
    performance["final_std_deviation"] = performance["std_deviation"]
    performance["std_deviation"] = performance["std_deviation"].fillna(0)
    performance["variance"] = performance["variance"].fillna(0)
    performance["judge_avg_place"] = None
    performance["judge_std_deviation"] = 0
    performance["judge_variance"] = 0
    performance["judge_marks"] = 0
    performance["consistency_score"] = 1 / (1 + performance["std_deviation"])
    performance["volatility_score"] = performance["std_deviation"]
    performance["confidence"] = performance.apply(lambda row: confidence_for_dance(int(row["n_marks"]), int(row["n_dates"])), axis=1)
    for column in [
        "final_avg_place",
        "avg_place",
        "median_place",
        "final_median_place",
        "std_deviation",
        "final_std_deviation",
        "variance",
        "best_place",
        "worst_place",
        "consistency_score",
        "volatility_score",
    ]:
        performance[column] = pd.to_numeric(performance[column], errors="coerce").round(3)
    return performance


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


def build_tournament_payload(
    marks: pd.DataFrame,
    dance_results: pd.DataFrame | None = None,
    protocol_results: pd.DataFrame | None = None,
) -> dict[str, Any]:
    tournaments = (
        marks[
            [
                "event_date",
                "tournament_id",
                "tournament_title",
                "tournament_city",
                "protocol_id",
                "category",
                "program",
                "entry_type",
            ]
        ]
        .drop_duplicates()
        .sort_values(["event_date", "protocol_id"])
    )
    tournaments = tournaments.rename(columns={"tournament_city": "city"})
    by_tournament = (
        tournaments.groupby(["event_date", "tournament_id", "tournament_title"], dropna=False)
        .agg(
            city=("city", lambda items: next((str(item) for item in items if pd.notna(item) and str(item).strip()), "")),
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
    if protocol_results is not None and not protocol_results.empty:
        result_perf = (
            protocol_results.dropna(subset=["protocol_place_value"])
            .groupby(["event_date", "tournament_id", "tournament_title"], dropna=False, as_index=False)
            .agg(
                avg_result_place=("protocol_place_value", "mean"),
                best_result_place=("protocol_place_value", "min"),
                result_places=("protocol_place_label", lambda items: sorted(set(str(item) for item in items if pd.notna(item) and str(item).strip()))),
            )
        )
        by_tournament = by_tournament.merge(result_perf, on=["event_date", "tournament_id", "tournament_title"], how="left")
    else:
        by_tournament["avg_result_place"] = None
        by_tournament["best_result_place"] = None
        by_tournament["result_places"] = [[] for _ in range(len(by_tournament))]

    if protocol_results is not None and not protocol_results.empty:
        perf = (
            protocol_results.dropna(subset=["protocol_place_value"])
            .groupby(["event_date", "tournament_id", "tournament_title"], dropna=False, as_index=False)
            .agg(avg_final_place=("protocol_place_value", "mean"))
            .sort_values(["avg_final_place", "event_date", "tournament_id"])
            .reset_index(drop=True)
        )
        if not perf.empty:
            total = len(perf)
            top_limit = 2 if total <= 5 else 3
            best_keys = set()
            ranks: dict[tuple[Any, Any, Any], dict[str, Any]] = {}
            for rank, row in enumerate(perf.itertuples(index=False), start=1):
                key = (row.event_date, row.tournament_id, row.tournament_title)
                ranks[key] = {"tournament_rank": rank, "rank_direction": ""}
                if rank <= top_limit:
                    ranks[key]["rank_direction"] = "best"
                    best_keys.add(key)
            hardest_candidates = list(reversed([row for row in perf.itertuples(index=False) if (row.event_date, row.tournament_id, row.tournament_title) not in best_keys]))
            for row in hardest_candidates[:top_limit]:
                key = (row.event_date, row.tournament_id, row.tournament_title)
                ranks[key]["rank_direction"] = "hardest"
            by_tournament["tournament_rank"] = by_tournament.apply(
                lambda row: ranks.get((row["event_date"], row["tournament_id"], row["tournament_title"]), {}).get("tournament_rank"),
                axis=1,
            )
            by_tournament["rank_direction"] = by_tournament.apply(
                lambda row: ranks.get((row["event_date"], row["tournament_id"], row["tournament_title"]), {}).get("rank_direction", ""),
                axis=1,
            )
        else:
            by_tournament["tournament_rank"] = None
            by_tournament["rank_direction"] = ""
    else:
        by_tournament["tournament_rank"] = None
        by_tournament["rank_direction"] = ""
    by_tournament["date"] = by_tournament["event_date"]
    tournaments["date"] = tournaments["event_date"]
    return {
        "count": int(marks["tournament_id"].nunique()),
        "protocol_count": int(marks["protocol_id"].nunique()),
        "items": df_records(by_tournament),
        "protocols": df_records(tournaments),
    }


def add_tournament_city_to_results(dance_results: pd.DataFrame, marks: pd.DataFrame) -> pd.DataFrame:
    if dance_results.empty or marks.empty or "tournament_city" not in marks.columns:
        return dance_results
    city_map = (
        marks[["event_date", "tournament_id", "tournament_title", "protocol_id", "tournament_city"]]
        .dropna(subset=["tournament_city"])
        .drop_duplicates(["event_date", "tournament_id", "tournament_title", "protocol_id"])
    )
    if city_map.empty:
        return dance_results
    result = dance_results.copy()
    merge_keys = ["event_date", "tournament_id", "tournament_title", "protocol_id"]
    result = result.merge(city_map, on=merge_keys, how="left")
    result["city"] = result["tournament_city"]
    result = result.drop(columns=["tournament_city"])
    return result


def safe_trend_ranking(dynamics: pd.DataFrame) -> pd.DataFrame:
    trend_metric_label = "среднее место"
    trend_explanation = "Сравнивается среднее место на ранней и последней доступной дате выбранного периода."
    trend_direction_rule = "меньшее место означает лучший результат"
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
    if not trends.empty:
        if "first_to_last_delta" in trends.columns:
            trends["first_to_last_delta"] = pd.to_numeric(trends["first_to_last_delta"], errors="coerce").round(3)
        else:
            trends["first_to_last_delta"] = None

        def trend_status(row: pd.Series) -> str:
            delta = row.get("first_to_last_delta")
            if pd.isna(delta):
                trend = row.get("trend_over_time")
                if pd.isna(trend):
                    return "stable"
                delta = trend
            delta = float(delta)
            if abs(delta) < 0.001:
                return "stable"
            return "improving" if delta < 0 else "declining"

        trends["trend_status"] = trends.apply(trend_status, axis=1)
        trends["trend_metric_label"] = trend_metric_label
        trends["trend_explanation"] = trend_explanation
        trends["trend_period_from"] = trends["first_date"] if "first_date" in trends.columns else None
        trends["trend_period_to"] = trends["last_date"] if "last_date" in trends.columns else None
        trends["trend_direction_rule"] = trend_direction_rule
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
            "display_mode": "insufficient_data",
            "best_dances": [],
            "worst_dances": [],
            "evaluated_dance": None,
            "tied_dances": [],
            "tied_metric_value": None,
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
    comparison = dance_comparison_payload(program_summary)
    return {
        "metrics": df_records(program_summary),
        **comparison,
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


def dance_comparison_record(row: pd.Series) -> dict[str, Any]:
    value = row.get("final_avg_place")
    return {
        "dance": clean_value(row.get("dance")),
        "dance_code": clean_value(normalize_dance_code(row.get("dance"))),
        "metric_value": clean_value(round(float(value), 3)) if pd.notna(value) else None,
        "metric_type": "final_avg_place",
        "metric_label": "среднее место",
        "final_avg_place": clean_value(round(float(value), 3)) if pd.notna(value) else None,
    }


def dance_comparison_payload(summary: pd.DataFrame, value_column: str = "final_avg_place") -> dict[str, Any]:
    if summary.empty or value_column not in summary.columns:
        return {
            "display_mode": "insufficient_data",
            "best_dances": [],
            "worst_dances": [],
            "evaluated_dance": None,
            "tied_dances": [],
            "tied_metric_value": None,
            "best_dance": None,
            "worst_dance": None,
        }
    metrics = summary.dropna(subset=[value_column]).copy()
    if metrics.empty:
        return {
            "display_mode": "insufficient_data",
            "best_dances": [],
            "worst_dances": [],
            "evaluated_dance": None,
            "tied_dances": [],
            "tied_metric_value": None,
            "best_dance": None,
            "worst_dance": None,
        }
    metrics["dance_code"] = metrics["dance"].map(normalize_dance_code)
    metrics["_dance_order"] = metrics["dance_code"].map(lambda code: DANCE_CODE_ORDER.get(code, 999))
    metrics = metrics.sort_values(["_dance_order", "dance_code"])
    values = [float(item) for item in metrics[value_column].tolist()]

    if len(metrics) == 1:
        record = dance_comparison_record(metrics.iloc[0])
        return {
            "display_mode": "single_dance",
            "best_dances": [],
            "worst_dances": [],
            "evaluated_dance": record,
            "tied_dances": [],
            "tied_metric_value": None,
            "best_dance": record,
            "worst_dance": None,
        }

    best_value = min(values)
    worst_value = max(values)
    if abs(best_value - worst_value) < 0.001:
        records = [dance_comparison_record(row) for _, row in metrics.iterrows()]
        return {
            "display_mode": "all_equal",
            "best_dances": [],
            "worst_dances": [],
            "evaluated_dance": None,
            "tied_dances": records,
            "tied_metric_value": clean_value(round(float(best_value), 3)),
            "best_dance": None,
            "worst_dance": None,
        }

    best_rows = metrics[(metrics[value_column] - best_value).abs() < 0.001]
    worst_rows = metrics[(metrics[value_column] - worst_value).abs() < 0.001]
    best_records = [dance_comparison_record(row) for _, row in best_rows.iterrows()]
    worst_records = [dance_comparison_record(row) for _, row in worst_rows.iterrows()]
    return {
        "display_mode": "best_worst",
        "best_dances": best_records,
        "worst_dances": worst_records,
        "evaluated_dance": None,
        "tied_dances": [],
        "tied_metric_value": None,
        "best_dance": best_records[0] if best_records else None,
        "worst_dance": worst_records[0] if worst_records else None,
    }


def marks_derived_dance_metrics(mark_rows: pd.DataFrame) -> list[dict[str, Any]]:
    if mark_rows.empty or "dance" not in mark_rows.columns:
        return []
    rows = mark_rows.dropna(subset=["dance"]).copy()
    if rows.empty:
        return []
    grouped = (
        rows.groupby(["program", "dance"], dropna=False, as_index=False)
        .agg(
            marks_count=("mark_id", "count"),
            protocols_count=("protocol_id", "nunique"),
            tournaments_count=("tournament_id", "nunique"),
            cross_marks=("mark_type", lambda items: int(sum(str(item) == "cross" for item in items))),
            numeric_marks=("mark_type", lambda items: int(sum(str(item) == "numeric_place" for item in items))),
        )
    )
    grouped["dance_code"] = grouped["dance"].map(normalize_dance_code)
    grouped["_dance_order"] = grouped["dance_code"].map(lambda code: DANCE_CODE_ORDER.get(code, 999))
    grouped = grouped.sort_values(["_dance_order", "dance_code"]).drop(columns=["_dance_order"])
    return df_records(grouped)


def category_chip_visibility(
    marks_count: int,
    results_count: int,
    dance_metrics_count: int,
    tournaments_count: int,
    trainer_summaries_count: int,
    trend_items_count: int,
    marks_derived_metrics_count: int,
) -> tuple[bool, str, str]:
    has_participation = marks_count > 0 or results_count > 0
    if not has_participation:
        return False, "hidden", "hidden: no selected dancer marks or results"
    if dance_metrics_count > 0:
        return True, "primary", "primary: dance metrics available"
    if results_count > 0:
        return True, "primary", "primary: dance results available"
    if trend_items_count > 0:
        return True, "primary", "primary: trend items available"
    if marks_derived_metrics_count > 0:
        if marks_count >= VISIBLE_CATEGORY_MIN_MARKS:
            return True, "primary", "primary: marks-derived dance analysis available"
        return True, "limited", f"limited: {marks_count} marks, marks-derived reference only"
    return True, "limited", "limited: participation is confirmed but analytical blocks are sparse"


def build_category_slices(numeric: pd.DataFrame, dance_results: pd.DataFrame, marks: pd.DataFrame) -> dict[str, dict[str, Any]]:
    numeric_with_slices = with_category_slice(numeric)
    results_with_slices = with_category_slice(dance_results)
    marks_with_slices = with_category_slice(marks)
    payload: dict[str, dict[str, Any]] = {"standard": {}, "latin": {}}

    def program_subset(df: pd.DataFrame, program: str) -> pd.DataFrame:
        if df.empty or "program" not in df.columns:
            return pd.DataFrame()
        return df[df["program"] == program].copy()

    def program_slice_subset(df: pd.DataFrame, program: str, slice_key: str) -> pd.DataFrame:
        subset = program_subset(df, program)
        if subset.empty or "category_slice" not in subset.columns:
            return pd.DataFrame()
        return subset[subset["category_slice"] == slice_key].copy()

    for program in ["standard", "latin"]:
        present_slice_keys = {
            str(item)
            for item in pd.concat(
                [
                    program_subset(numeric_with_slices, program).get("category_slice", pd.Series(dtype="object")),
                    program_subset(results_with_slices, program).get("category_slice", pd.Series(dtype="object")),
                    program_subset(marks_with_slices, program).get("category_slice", pd.Series(dtype="object")),
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
                slice_numeric = program_subset(numeric_with_slices, program)
                slice_results = program_subset(results_with_slices, program)
                slice_marks = program_subset(marks_with_slices, program)
            else:
                slice_numeric = program_slice_subset(numeric_with_slices, program, slice_key)
                slice_results = program_slice_subset(results_with_slices, program, slice_key)
                slice_marks = program_slice_subset(marks_with_slices, program, slice_key)
                if slice_marks.empty and slice_results.empty:
                    continue

            if slice_results.empty:
                slice_summary = pd.DataFrame()
                slice_dynamics = pd.DataFrame()
                slice_trends = pd.DataFrame()
                program_data = program_payload(program, slice_summary, slice_trends)
            else:
                slice_summary = dance_summary_from_results(slice_numeric, slice_results)
                slice_dynamics = dynamics_by_date(slice_results)
                slice_trends = safe_trend_ranking(slice_dynamics)
                program_data = program_payload(program, slice_summary, slice_trends)
            slice_tournament_results = build_tournament_dance_results(slice_results)
            marks_derived_metrics = marks_derived_dance_metrics(slice_marks)
            marks_count = int(len(slice_marks))
            results_count = int(len(slice_results))
            dance_metrics_count = int(len(program_data.get("metrics") or []))
            protocols_count = int(
                len(
                    set(
                        [
                            *([] if slice_marks.empty else slice_marks["protocol_id"].dropna().tolist()),
                            *([] if slice_results.empty else slice_results["protocol_id"].dropna().tolist()),
                        ]
                    )
                )
            )
            tournaments_count = int(
                len(
                    set(
                        [
                            *([] if slice_marks.empty else slice_marks["tournament_id"].dropna().tolist()),
                            *([] if slice_results.empty else slice_results["tournament_id"].dropna().tolist()),
                        ]
                    )
                )
            )
            trainer_summaries_count = (
                int(
                    slice_marks.dropna(subset=["dance"])
                    .groupby(["event_date", "tournament_id", "tournament_title"], dropna=False)
                    .size()
                    .shape[0]
                )
                if not slice_marks.empty
                else 0
            )
            trend_items_count = int(len(slice_trends))
            is_visible_chip, visibility_status, visibility_reason = category_chip_visibility(
                marks_count,
                results_count,
                dance_metrics_count,
                tournaments_count,
                trainer_summaries_count,
                trend_items_count,
                len(marks_derived_metrics),
            )

            program_data.update(
                {
                    "key": slice_key,
                    "label": label,
                    "source": "selected_dancer_marks_and_results",
                    "class_group": None if slice_key == "all" else label,
                    "age_groups": sorted(set(str(item) for item in slice_marks["age_group"].dropna())) if not slice_marks.empty and "age_group" in slice_marks.columns else [],
                    "source_categories": sorted(set(str(item) for item in slice_marks["category"].dropna())) if not slice_marks.empty else [],
                    "protocol_count": protocols_count,
                    "tournament_count": tournaments_count,
                    "evidence": {
                        "tournaments": int(slice_marks["tournament_id"].nunique()) if not slice_marks.empty else 0,
                        "protocols": int(slice_marks["protocol_id"].nunique()) if not slice_marks.empty else 0,
                        "marks": marks_count,
                        "results": results_count,
                    },
                    "visibility": {
                        "marks_count": marks_count,
                        "results_count": results_count,
                        "dance_metrics_count": dance_metrics_count,
                        "protocols_count": protocols_count,
                        "tournaments_count": tournaments_count,
                        "trainer_summaries_count": trainer_summaries_count,
                        "trend_items_count": trend_items_count,
                        "marks_derived_metrics_count": len(marks_derived_metrics),
                    },
                    "marks_derived_dance_metrics": marks_derived_metrics,
                    "is_visible_chip": is_visible_chip,
                    "visibility_status": visibility_status,
                    "visibility_reason": visibility_reason,
                    "dynamics_by_date": df_records(slice_dynamics),
                    "trends": df_records(slice_trends),
                    "tournament_dance_results": df_records(slice_tournament_results),
                }
            )
            payload[program][slice_key] = program_data
    return payload


def build_parent_category_groups(numeric: pd.DataFrame, dance_results: pd.DataFrame, marks: pd.DataFrame) -> dict[str, dict[str, Any]]:
    numeric_with_slices = with_category_slice(numeric)
    results_with_slices = with_category_slice(dance_results)
    marks_with_slices = with_category_slice(marks)
    payload: dict[str, dict[str, Any]] = {"standard": {}, "latin": {}}

    def program_subset(df: pd.DataFrame, program: str) -> pd.DataFrame:
        if df.empty or "program" not in df.columns:
            return pd.DataFrame()
        return df[df["program"] == program].copy()

    def group_subset(df: pd.DataFrame, program: str, group_keys: list[str]) -> pd.DataFrame:
        subset = program_subset(df, program)
        if subset.empty or "category_slice" not in subset.columns:
            return pd.DataFrame()
        return subset[subset["category_slice"].isin(group_keys)].copy()

    def present_labels(*frames: pd.DataFrame) -> list[str]:
        present: set[str] = set()
        for frame in frames:
            if not frame.empty and "class_group" in frame.columns:
                present.update(str(item) for item in frame["class_group"].dropna().tolist() if str(item))
        ordered = []
        for label in CLASS_GROUP_ORDER:
            if label in present:
                ordered.append(label)
        return ordered

    def build_payload_for_group(program: str, group_key: str, label: str, group_keys: list[str]) -> dict[str, Any] | None:
        if group_key == "all":
            group_numeric = program_subset(numeric_with_slices, program)
            group_results = program_subset(results_with_slices, program)
            group_marks = program_subset(marks_with_slices, program)
        else:
            group_numeric = group_subset(numeric_with_slices, program, group_keys)
            group_results = group_subset(results_with_slices, program, group_keys)
            group_marks = group_subset(marks_with_slices, program, group_keys)
        if group_marks.empty and group_results.empty:
            return None

        if group_results.empty:
            group_summary = pd.DataFrame()
            group_dynamics = pd.DataFrame()
            group_trends = pd.DataFrame()
            group_data = program_payload(program, group_summary, group_trends)
        else:
            group_summary = dance_summary_from_results(group_numeric, group_results)
            group_dynamics = dynamics_by_date(group_results)
            group_trends = safe_trend_ranking(group_dynamics)
            group_data = program_payload(program, group_summary, group_trends)

        marks_count = int(len(group_marks))
        results_count = int(len(group_results))
        protocol_ids = set()
        tournament_ids = set()
        for frame in [group_marks, group_results]:
            if not frame.empty:
                protocol_ids.update(frame["protocol_id"].dropna().tolist())
                tournament_ids.update(frame["tournament_id"].dropna().tolist())
        included_categories = present_labels(group_marks, group_results)
        source_categories = sorted(
            {
                str(item)
                for frame in [group_marks, group_results]
                if not frame.empty and "category" in frame.columns
                for item in frame["category"].dropna().tolist()
            }
        )
        group_data.update(
            {
                "key": group_key,
                "label": label,
                "source": "parent_category_aggregation",
                "included_categories": included_categories,
                "source_categories": source_categories,
                "protocol_count": int(len(protocol_ids)),
                "tournament_count": int(len(tournament_ids)),
                "evidence": {
                    "tournaments": int(len(tournament_ids)),
                    "protocols": int(len(protocol_ids)),
                    "marks": marks_count,
                    "results": results_count,
                },
                "dynamics_by_date": df_records(group_dynamics),
                "trends": df_records(group_trends),
            }
        )
        return group_data

    for program in ["standard", "latin"]:
        all_payload = build_payload_for_group(program, "all", "Все категории", [])
        if all_payload:
            payload[program]["all"] = all_payload
        for group_key, group_keys in PARENT_CATEGORY_GROUPS.items():
            group_payload = build_payload_for_group(program, group_key, PARENT_CATEGORY_GROUP_LABELS[group_key], group_keys)
            if group_payload:
                payload[program][group_key] = group_payload
    return payload


def build_tournament_dance_results(
    dance_results: pd.DataFrame,
    marks: pd.DataFrame | None = None,
    protocol_results: pd.DataFrame | None = None,
) -> pd.DataFrame:
    if marks is not None and not marks.empty and protocol_results is not None and not protocol_results.empty:
        mark_columns = [
            "protocol_db_id",
            "protocol_id",
            "tournament_id",
            "tournament_title",
            "event_date",
            "tournament_city",
            "category",
            "program",
            "dance",
        ]
        mark_dances = (
            marks.dropna(subset=["dance"])[mark_columns]
            .drop_duplicates(["protocol_db_id", "program", "dance"])
            .copy()
        )
        mark_counts = (
            marks.dropna(subset=["dance"])
            .groupby(["protocol_db_id", "program", "dance"], dropna=False)
            .size()
            .reset_index(name="judge_marks")
        )
        result_columns = [
            "protocol_db_id",
            "protocol_place",
            "protocol_place_value",
            "protocol_place_label",
        ]
        result = mark_dances.merge(protocol_results[result_columns], on="protocol_db_id", how="left")
        result = result.merge(mark_counts, on=["protocol_db_id", "program", "dance"], how="left")
        result = result.dropna(subset=["protocol_place_value"]).copy()
        if result.empty:
            return pd.DataFrame()
        result = result.rename(columns={"tournament_city": "city"})
        result["final_place"] = result["protocol_place_value"]
        result["best_place"] = result["protocol_place_value"]
        result["worst_place"] = result["protocol_place_value"]
        result["n_results"] = 1
        result["judge_marks"] = result["judge_marks"].fillna(0).astype(int)
        result["dance_code"] = result["dance"].map(normalize_dance_code)
        result["result_source"] = "protocol_place"
        result["result_place"] = result["protocol_place"]
        result["result_place_label"] = result["protocol_place_label"]
        result["_dance_order"] = result["dance_code"].map(lambda code: DANCE_CODE_ORDER.get(code, 999))
        result = result.sort_values(["event_date", "protocol_id", "program", "_dance_order", "dance_code"]).drop(columns=["_dance_order"])
        result["date"] = result["event_date"]
        for column in ["final_place", "best_place", "worst_place"]:
            result[column] = result[column].round(3)
        return result

    if dance_results.empty:
        return pd.DataFrame()
    group_columns = [
        "event_date",
        "tournament_id",
        "tournament_title",
        "protocol_id",
        "category",
        "program",
        "dance",
    ]
    if "city" in dance_results.columns:
        group_columns.insert(3, "city")
    result = (
        dance_results.groupby(
            group_columns,
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
    if "result_place_label" in dance_results.columns:
        labels = (
            dance_results.groupby(group_columns, dropna=False, as_index=False)
            .agg(result_place_label=("result_place_label", lambda items: next((str(item) for item in items if pd.notna(item) and str(item).strip()), None)))
        )
        result = result.merge(labels, on=group_columns, how="left")
    for column in ["final_place", "best_place", "worst_place"]:
        result[column] = result[column].round(3)
    result["dance_code"] = result["dance"].map(normalize_dance_code)
    result["result_source"] = "dance_final_place"
    result["result_place"] = result["final_place"]
    if "result_place_label" not in result.columns:
        result["result_place_label"] = result["final_place"].map(place_label)
    result["_dance_order"] = result["dance_code"].map(lambda code: DANCE_CODE_ORDER.get(code, 999))
    result = result.sort_values(["event_date", "protocol_id", "program", "_dance_order", "dance_code"]).drop(columns=["_dance_order"])
    result["date"] = result["event_date"]
    return result


def tournament_metric_record(row: pd.Series, metric_type: str, role: str | None = None) -> dict[str, Any]:
    value_key = "final_place"
    value = row[value_key]
    record: dict[str, Any] = {
        "dance": clean_value(row["dance"]),
        "dance_code": clean_value(row["dance_code"]),
        "metric_value": clean_value(round(float(value), 3)),
        "metric_type": "final_place",
        "metric_label": "итоговое место",
        "category": clean_value(row["category"]),
        "protocol_id": clean_value(row["protocol_id"]),
        "result_place_label": clean_value(row.get("result_place_label")),
    }
    record["final_place"] = clean_value(round(float(value), 3))
    record["interpretation"] = "по итоговым местам"
    return record


def apply_tournament_display_mode(summary: dict[str, Any], metrics: pd.DataFrame, metric_type: str) -> dict[str, Any]:
    value_column = "final_place"
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
        summary["tied_metric_value"] = clean_value(round(float(value), 3))
        summary["tied_metric_label"] = place_label(value)
        return summary

    summary["data_sufficient"] = True
    summary["display_mode"] = "best_worst"
    best_value = min(values)
    worst_value = max(values)
    best_rows = metrics[metrics[value_column].round(6) == best_value].sort_values(["_dance_order", "dance_code"])
    worst_rows = metrics[metrics[value_column].round(6) == worst_value].sort_values(["_dance_order", "dance_code"])
    best_codes = [clean_value(row["dance_code"]) for _, row in best_rows.iterrows()]
    worst_codes = [clean_value(row["dance_code"]) for _, row in worst_rows.iterrows()]
    if set(best_codes) == set(worst_codes):
        value = best_value
        summary["display_mode"] = "tied"
        summary["tied_dances"] = clean_value(metrics["dance_code"].tolist())
        summary["tied_metric_value"] = clean_value(round(float(value), 3))
        summary["tied_metric_label"] = place_label(value)
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
            "result_places": sorted(set(str(item) for item in scored.get("result_place_label", pd.Series(dtype="object")).dropna())) if not scored.empty and "result_place_label" in scored.columns else [],
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
                result_place_label=("result_place_label", lambda items: next((str(item) for item in items if pd.notna(item) and str(item).strip()), None))
                if "result_place_label" in scored.columns
                else ("final_place", lambda items: place_label(next(iter(items), None))),
                protocol_id=("protocol_id", "first"),
                category=("category", "first"),
            )
        )
        final_metrics["final_place"] = final_metrics["final_place"].round(3)
        return apply_tournament_display_mode(summary, final_metrics, "final_place")

    summary["message"] = "итоговое место участника не найдено"
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
                "date": clean_value(event_date),
                "tournament_id": clean_value(tournament_id),
                "tournament_title": clean_value(tournament_title),
                "city": clean_value(next((item for item in tournament_marks["tournament_city"].dropna().tolist() if str(item).strip()), "")) if "tournament_city" in tournament_marks.columns else "",
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
    required_columns = {"n_marks", "strictness", "softness"}
    if stats.empty or not required_columns.issubset(stats.columns):
        return {
            "strictest": [],
            "softest": [],
            "low_confidence": [],
            "by_program": {
                "standard": {"strictest": [], "softest": []},
                "latin": {"strictest": [], "softest": []},
            },
        }
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
        if by_program.empty or not required_columns.union({"program"}).issubset(by_program.columns):
            payload["by_program"][program] = {"strictest": [], "softest": []}
            continue
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


def build_judge_panel_reaction(marks: pd.DataFrame) -> dict[str, Any]:
    if marks.empty:
        return {}
    required_columns = {
        "mark_type",
        "numeric_mark",
        "tournament_id",
        "tournament_title",
        "event_date",
        "tournament_city",
        "protocol_id",
        "round",
        "dance",
        "program",
        "judge_id",
    }
    if not required_columns.issubset(set(marks.columns)):
        return {}

    numeric = marks[(marks["mark_type"] == "numeric_place") & marks["numeric_mark"].notna()].copy()
    if numeric.empty:
        return {}
    numeric["event_date_dt"] = pd.to_datetime(numeric["event_date"], errors="coerce")

    group_columns = [
        "tournament_id",
        "tournament_title",
        "event_date",
        "event_date_dt",
        "tournament_city",
        "protocol_id",
        "round",
        "dance",
        "program",
    ]
    protocol_dance_spread = (
        numeric.groupby(group_columns, dropna=False)
        .agg(
            judge_marks=("numeric_mark", "count"),
            judge_count=("judge_id", "nunique"),
            panel_spread=("numeric_mark", lambda values: float(values.std(ddof=0)) if len(values.dropna()) >= 2 else None),
            panel_mean=("numeric_mark", "mean"),
        )
        .reset_index()
    )
    protocol_dance_spread = protocol_dance_spread[protocol_dance_spread["panel_spread"].notna()].copy()
    if protocol_dance_spread.empty:
        return {}

    by_tournament = (
        protocol_dance_spread.groupby(
            ["tournament_id", "tournament_title", "event_date", "event_date_dt", "tournament_city"],
            dropna=False,
        )
        .agg(
            panel_spread=("panel_spread", "mean"),
            protocol_count=("protocol_id", "nunique"),
            protocol_dance_round_groups=("panel_spread", "count"),
            numeric_marks=("judge_marks", "sum"),
            dances=("dance", lambda items: sort_dance_codes([normalize_dance_code(item) for item in items if pd.notna(item)])),
            programs=("program", lambda items: sorted(set(str(item) for item in items if pd.notna(item)))),
        )
        .reset_index()
        .sort_values(["event_date_dt", "tournament_id"])
    )
    tournament_count = len(by_tournament)
    if tournament_count == 0:
        return {}

    window_size = max(1, round(tournament_count * 0.4))
    first = float(by_tournament.head(window_size)["panel_spread"].mean())
    last = float(by_tournament.tail(window_size)["panel_spread"].mean())
    delta = last - first
    if abs(delta) < PANEL_REACTION_SPREAD_THRESHOLD:
        trend = "stable"
    elif delta < 0:
        trend = "more_consistent"
    else:
        trend = "less_consistent"

    rows = []
    for _, row in by_tournament.iterrows():
        rows.append(
            {
                "tournament_id": clean_value(row["tournament_id"]),
                "tournament_title": clean_value(row["tournament_title"]),
                "event_date": clean_value(row["event_date"]),
                "city": clean_value(row["tournament_city"]),
                "panel_spread": clean_value(float(row["panel_spread"])),
                "protocol_count": clean_value(row["protocol_count"]),
                "protocol_dance_round_groups": clean_value(row["protocol_dance_round_groups"]),
                "numeric_marks": clean_value(row["numeric_marks"]),
                "dances": clean_value(row["dances"]),
                "programs": clean_value(row["programs"]),
            }
        )

    return {
        "panel_spread_by_tournament": rows,
        "panel_spread_first": first,
        "panel_spread_last": last,
        "panel_spread_delta": delta,
        "panel_spread_trend": trend,
        "early_late_window_tournaments": window_size,
        "tournament_count": tournament_count,
        "threshold": PANEL_REACTION_SPREAD_THRESHOLD,
        "method": "numeric_place marks only; spread by protocol+round+dance; tournament mean; first 40% vs last 40%",
    }


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
    raw_dance_results = selected_dance_results_by_internal_id(conn, identity.internal_dancer_id)
    raw_dance_results = add_tournament_city_to_results(raw_dance_results, marks)
    protocol_results = selected_protocol_results(conn, identity.internal_dancer_id)
    tournament_dance_results = build_tournament_dance_results(raw_dance_results, marks, protocol_results)
    if tournament_dance_results.empty:
        tournament_dance_results = build_tournament_dance_results(raw_dance_results)
    summary = dance_summary_from_results(numeric, tournament_dance_results)
    dynamics = dynamics_by_date(tournament_dance_results)
    trends = safe_trend_ranking(dynamics)

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
        "judge_panel_reaction": build_judge_panel_reaction(marks),
        "dances": {
            "metrics": df_records(summary),
            "trends": df_records(trends),
            "dynamics_by_date": df_records(dynamics),
        },
        "category_slices": build_category_slices(numeric, tournament_dance_results, marks),
        "parent_category_groups": build_parent_category_groups(numeric, tournament_dance_results, marks),
        "tournaments": {
            **build_tournament_payload(marks, tournament_dance_results, protocol_results),
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
