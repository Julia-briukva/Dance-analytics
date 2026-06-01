#!/usr/bin/env python3
"""Analyze dance-level metrics for one selected dancer."""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

import pandas as pd
from bs4 import BeautifulSoup, Tag

from analyze_dancer import (
    DEFAULT_DB_PATH,
    LATIN_DANCES,
    STANDARD_DANCES,
    add_derived_columns,
    ensure_analytics_view,
    resolve_dancer_by_idd,
    selected_marks_by_internal_id,
)
from compreg_encoding import read_compreg_html_file
from parse_protocols import clean_empty, normalize_text, parse_float


MIN_MARKS_FOR_RANKING = 6
MIN_DATES_FOR_TREND = 2
PREFERRED_DATES_FOR_TREND = 3
ROLLING_WINDOW = 3


def print_table(title: str, df: pd.DataFrame, max_rows: int | None = None) -> None:
    print(f"\n{title}")
    if df.empty:
        print("  Нет данных")
        return
    view = df.head(max_rows) if max_rows else df
    print(view.to_string(index=False))


def confidence_for_dance(n_marks: int, n_dates: int) -> str:
    if n_marks < MIN_MARKS_FOR_RANKING or n_dates < MIN_DATES_FOR_TREND:
        return "low confidence"
    return "ok"


def confidence_for_trend(n_dates: int) -> str:
    if n_dates < MIN_DATES_FOR_TREND:
        return "insufficient trend"
    if n_dates < PREFERRED_DATES_FOR_TREND:
        return "low confidence trend"
    return "ok"


def linear_slope(values: pd.Series) -> float | None:
    clean = values.dropna().reset_index(drop=True)
    n = len(clean)
    if n < MIN_DATES_FOR_TREND:
        return None
    x = pd.Series(range(n), dtype="float64")
    y = clean.astype("float64")
    x_mean = x.mean()
    y_mean = y.mean()
    denominator = ((x - x_mean) ** 2).sum()
    if denominator == 0:
        return None
    return float(((x - x_mean) * (y - y_mean)).sum() / denominator)


def selected_numeric_marks_by_internal_id(conn: sqlite3.Connection, internal_dancer_id: int) -> pd.DataFrame:
    ensure_analytics_view(conn)
    marks = add_derived_columns(selected_marks_by_internal_id(conn, internal_dancer_id))
    if marks.empty:
        return marks
    numeric = marks[(marks["mark_type"] == "numeric_place") & (marks["is_final_round"] == 1)].dropna(subset=["numeric_mark"]).copy()
    numeric["event_date"] = pd.to_datetime(numeric["event_date"], errors="coerce")
    return numeric.sort_values(["event_date", "protocol_id", "dance", "mark_id"])


def protocol_cache_paths(conn: sqlite3.Connection) -> dict[int, str]:
    return {int(row[0]): row[1] for row in conn.execute("SELECT id, protocol_cache_path FROM protocols")}


def extract_ordinary_dance_result(canvas: Tag, competitor_number: str, round_name: str, dance_name: str, raw_mark: str) -> float | None:
    caption_node = canvas.select_one(".prot-subcaption")
    parsed_round = normalize_text(caption_node.get_text(" ", strip=True) if caption_node else "")
    if parsed_round != round_name:
        return None
    rows = canvas.find_all("div", class_="round-data-box", recursive=False)
    if not rows:
        rows = canvas.select(".round-DT-caption > .round-data-box, .prot-table-canvas > .round-data-box")
    for row in rows:
        number_node = row.select_one(".round-data-num-all")
        number = clean_empty(number_node.get_text(" ", strip=True).replace("№", "") if number_node else None)
        if number != str(competitor_number):
            continue
        dance_box = row.select_one(".round-data-dance")
        if not dance_box:
            continue
        children = [child for child in dance_box.find_all("div", recursive=False) if isinstance(child, Tag)]
        idx = 0
        while idx < len(children):
            dance = mark = result_text = None
            if "visible-ph-al" in children[idx].get("class", []):
                dance = clean_empty(children[idx].get_text(" ", strip=True))
                idx += 1
            if idx < len(children) and "round-data-marks-mt" in children[idx].get("class", []):
                mark = clean_empty(children[idx].get_text(" ", strip=True))
                idx += 1
            if idx < len(children) and "round-data-sum" in children[idx].get("class", []):
                result_text = clean_empty(children[idx].get_text(" ", strip=True))
                idx += 1
            if dance == dance_name and mark == raw_mark:
                return parse_float(result_text)
            if not dance or not mark:
                idx += 1
    return None


def extract_fkt_dance_result(canvas: Tag, competitor_number: str, round_name: str, dance_name: str, raw_mark: str) -> float | None:
    round_node = canvas.select_one(":scope > .prot-subcaption")
    parsed_round = normalize_text(round_node.get_text(" ", strip=True) if round_node else "")
    if parsed_round != round_name:
        return None
    for dance_box in canvas.find_all("div", class_="round-data-box-fkt", recursive=False):
        dance_node = dance_box.find("div", class_="prot-subcaption", recursive=False)
        dance = clean_empty(dance_node.get_text(" ", strip=True) if dance_node else None)
        if dance != dance_name:
            continue
        for row in dance_box.find_all("div", class_="round-data-cappel-box-fkt", recursive=False):
            number_node = row.find("div", class_="round-data-num", recursive=False)
            number = clean_empty(number_node.get_text(" ", strip=True).replace("№", "") if number_node else None)
            raw_node = row.find("div", class_="round-data-marks-mt-fkt", recursive=False)
            mark = clean_empty(raw_node.get_text(" ", strip=True) if raw_node else None)
            if number == str(competitor_number) and mark == raw_mark:
                place_node = row.find("div", class_="round-data-place", recursive=False)
                place_text = clean_empty(place_node.get_text(" ", strip=True).replace("Место в туре -", "") if place_node else None)
                return parse_float(place_text)
    return None


def selected_dance_results_by_internal_id(conn: sqlite3.Connection, internal_dancer_id: int) -> pd.DataFrame:
    numeric = selected_numeric_marks_by_internal_id(conn, internal_dancer_id)
    if numeric.empty:
        return pd.DataFrame()

    paths = protocol_cache_paths(conn)
    soup_cache: dict[str, BeautifulSoup] = {}
    rows: list[dict[str, object]] = []
    keys = [
        "protocol_db_id",
        "protocol_id",
        "tournament_id",
        "tournament_title",
        "event_date",
        "category",
        "round",
        "dance",
        "competitor_number",
        "raw_mark_string",
        "program",
    ]
    for key_values, group in numeric.groupby(keys, dropna=False):
        item = dict(zip(keys, key_values))
        path = paths.get(int(item["protocol_db_id"]))
        if not path:
            continue
        if path not in soup_cache:
            cache_path = Path(path)
            if not cache_path.is_absolute():
                cache_path = DEFAULT_DB_PATH.parents[1] / cache_path
            soup_cache[path] = BeautifulSoup(read_compreg_html_file(cache_path), "html.parser")
        final_place = None
        for canvas in soup_cache[path].select(".prot-table-canvas"):
            final_place = extract_ordinary_dance_result(
                canvas,
                str(item["competitor_number"]),
                str(item["round"]),
                str(item["dance"]),
                str(item["raw_mark_string"]),
            )
            if final_place is not None:
                break
            final_place = extract_fkt_dance_result(
                canvas,
                str(item["competitor_number"]),
                str(item["round"]),
                str(item["dance"]),
                str(item["raw_mark_string"]),
            )
            if final_place is not None:
                break
        if final_place is None:
            continue
        item["final_place"] = final_place
        item["judge_marks"] = int(group["numeric_mark"].count())
        rows.append(item)

    result = pd.DataFrame(rows)
    if not result.empty:
        result["event_date"] = pd.to_datetime(result["event_date"], errors="coerce")
    return result.sort_values(["event_date", "protocol_id", "dance"]) if not result.empty else result


def dance_summary(numeric: pd.DataFrame, dance_results: pd.DataFrame | None = None) -> pd.DataFrame:
    if numeric.empty:
        return pd.DataFrame()
    if dance_results is None or dance_results.empty:
        dance_results = (
            numeric.groupby(["program", "dance", "protocol_id", "round"], as_index=False)
            .agg(
                final_place=("place", "first"),
                event_date=("event_date", "first"),
                category=("category", "first"),
            )
            .dropna(subset=["final_place"])
        )
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
    judge_stats = (
        numeric.groupby(["program", "dance"], as_index=False)
        .agg(
            judge_avg_place=("numeric_mark", "mean"),
            judge_std_deviation=("numeric_mark", "std"),
            judge_variance=("numeric_mark", "var"),
            judge_marks=("numeric_mark", "count"),
        )
    )
    stats = performance.merge(judge_stats, on=["program", "dance"], how="left")
    stats["avg_place"] = stats["final_avg_place"]
    stats["final_median_place"] = stats["median_place"]
    stats["final_std_deviation"] = stats["std_deviation"]
    stats["std_deviation"] = stats["std_deviation"].fillna(0)
    stats["variance"] = stats["variance"].fillna(0)
    stats["judge_std_deviation"] = stats["judge_std_deviation"].fillna(0)
    stats["judge_variance"] = stats["judge_variance"].fillna(0)
    stats["consistency_score"] = 1 / (1 + stats["std_deviation"])
    stats["volatility_score"] = stats["std_deviation"]
    stats["confidence"] = stats.apply(lambda row: confidence_for_dance(int(row["n_marks"]), int(row["n_dates"])), axis=1)
    for column in [
        "final_avg_place",
        "avg_place",
        "judge_avg_place",
        "median_place",
        "final_median_place",
        "std_deviation",
        "final_std_deviation",
        "variance",
        "judge_std_deviation",
        "judge_variance",
        "consistency_score",
        "volatility_score",
        "best_place",
        "worst_place",
    ]:
        stats[column] = stats[column].round(3)
    return stats


def dynamics_by_date(dance_results: pd.DataFrame) -> pd.DataFrame:
    if dance_results.empty:
        return pd.DataFrame()
    dynamics = (
        dance_results.groupby(["program", "dance", "event_date"], as_index=False)
        .agg(
            avg_place=("final_place", "mean"),
            final_avg_place=("final_place", "mean"),
            median_place=("final_place", "median"),
            n_marks=("final_place", "count"),
            n_protocols=("protocol_id", "nunique"),
        )
        .sort_values(["program", "dance", "event_date"])
    )
    dynamics["rolling_average"] = (
        dynamics.groupby(["program", "dance"])["avg_place"]
        .transform(lambda values: values.rolling(ROLLING_WINDOW, min_periods=1).mean())
    )
    dynamics["tournament_to_tournament_delta"] = dynamics.groupby(["program", "dance"])["avg_place"].diff()
    dynamics["event_date"] = dynamics["event_date"].dt.strftime("%Y-%m-%d")
    for column in ["avg_place", "final_avg_place", "median_place", "rolling_average", "tournament_to_tournament_delta"]:
        dynamics[column] = dynamics[column].round(3)
    return dynamics


def trend_ranking(dynamics: pd.DataFrame) -> pd.DataFrame:
    if dynamics.empty:
        return pd.DataFrame()
    rows: list[dict[str, object]] = []
    for (program, dance), group in dynamics.groupby(["program", "dance"]):
        ordered = group.sort_values("event_date")
        slope = linear_slope(ordered["avg_place"])
        first = ordered.iloc[0]
        last = ordered.iloc[-1]
        n_dates = len(ordered)
        rows.append(
            {
                "program": program,
                "dance": dance,
                "trend_over_time": slope,
                "first_date": first["event_date"],
                "first_avg_place": first["avg_place"],
                "last_date": last["event_date"],
                "last_avg_place": last["avg_place"],
                "first_to_last_delta": round(float(last["avg_place"] - first["avg_place"]), 3),
                "n_dates": n_dates,
                "confidence": confidence_for_trend(n_dates),
            }
        )
    result = pd.DataFrame(rows)
    result["trend_over_time"] = result["trend_over_time"].round(3)
    return result.sort_values(["program", "trend_over_time"])


def missing_dances(numeric: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, str]] = []
    present_standard = set(numeric.loc[numeric["program"] == "standard", "dance"].dropna().str.upper())
    present_latin = set(numeric.loc[numeric["program"] == "latin", "dance"].dropna().str.upper())
    for dance in sorted(STANDARD_DANCES):
        if dance not in present_standard:
            rows.append({"program": "standard", "dance": dance, "status": "missing numeric final marks"})
    for dance in sorted(LATIN_DANCES):
        if dance not in present_latin:
            rows.append({"program": "latin", "dance": dance, "status": "missing numeric final marks"})
    return pd.DataFrame(rows)


def incomplete_protocol_warnings(numeric: pd.DataFrame) -> pd.DataFrame:
    if numeric.empty:
        return pd.DataFrame()
    rows: list[dict[str, object]] = []
    for (protocol_id, program), group in numeric.groupby(["protocol_id", "program"]):
        category = str(group.iloc[0]["category"] or "").lower()
        if "d+c" not in category:
            continue
        expected = STANDARD_DANCES if program == "standard" else LATIN_DANCES if program == "latin" else set()
        if not expected:
            continue
        present = set(group["dance"].dropna().str.upper())
        missing = sorted(expected - present)
        if missing:
            first = group.iloc[0]
            rows.append(
                {
                    "event_date": first["event_date"].strftime("%Y-%m-%d") if pd.notna(first["event_date"]) else "",
                    "protocol_id": protocol_id,
                    "program": program,
                    "category": first["category"],
                    "present_dances": ",".join(sorted(present)),
                    "missing_dances": ",".join(missing),
                }
            )
    return pd.DataFrame(rows).sort_values(["event_date", "protocol_id", "program"]) if rows else pd.DataFrame()


def ranking_tables(summary: pd.DataFrame, trends: pd.DataFrame) -> dict[str, pd.DataFrame]:
    eligible = summary[(summary["n_marks"] >= MIN_MARKS_FOR_RANKING) & (summary["n_dates"] >= MIN_DATES_FOR_TREND)].copy()
    trend_eligible = trends[trends["n_dates"] >= MIN_DATES_FOR_TREND].copy()
    return {
        "best_by_final_average": eligible.sort_values(["final_avg_place", "n_marks"], ascending=[True, False]),
        "best_by_median": eligible.sort_values(["final_median_place", "n_marks"], ascending=[True, False]),
        "most_stable": eligible.sort_values(["final_std_deviation", "n_marks"], ascending=[True, False]),
        "best_peak": eligible.sort_values(["best_place", "n_marks"], ascending=[True, False]),
        "worst_by_final_average": eligible.sort_values(["final_avg_place", "n_marks"], ascending=[False, False]),
        "judge_level_best": eligible.sort_values(["judge_avg_place", "judge_marks"], ascending=[True, False]),
        "strongest": eligible.sort_values(["final_avg_place", "n_marks"], ascending=[True, False]),
        "weakest": eligible.sort_values(["final_avg_place", "n_marks"], ascending=[False, False]),
        "stability": eligible.sort_values(["final_std_deviation", "n_marks"], ascending=[True, False]),
        "least_stable": eligible.sort_values(["final_std_deviation", "n_marks"], ascending=[False, False]),
        "improvement": trend_eligible.sort_values(["trend_over_time", "n_dates"], ascending=[True, False]),
        "regression": trend_eligible.sort_values(["trend_over_time", "n_dates"], ascending=[False, False]),
    }


def print_program_summaries(summary: pd.DataFrame, trends: pd.DataFrame) -> None:
    for program in ["standard", "latin"]:
        print(f"\n=== {program.upper()} ===")
        program_summary = summary[summary["program"] == program]
        program_trends = trends[trends["program"] == program]
        print_table("Dance metrics", program_summary)
        tables = ranking_tables(program_summary, program_trends)
        print_table("Best by final average", tables["best_by_final_average"], max_rows=10)
        print_table("Best by median", tables["best_by_median"], max_rows=10)
        print_table("Most stable", tables["most_stable"], max_rows=10)
        print_table("Best peak", tables["best_peak"], max_rows=10)
        print_table("Worst by final average", tables["worst_by_final_average"], max_rows=10)
        print_table("Judge-level best", tables["judge_level_best"], max_rows=10)
        print_table("Least stable ranking", tables["least_stable"], max_rows=10)
        print_table("Improvement ranking", tables["improvement"], max_rows=10)
        print_table("Regression ranking", tables["regression"], max_rows=10)


def analyze(conn: sqlite3.Connection, compreg_idd: str | int) -> int:
    try:
        identity = resolve_dancer_by_idd(conn, compreg_idd)
    except ValueError as exc:
        print(str(exc))
        return 1

    numeric = selected_numeric_marks_by_internal_id(conn, identity.internal_dancer_id)
    if numeric.empty:
        print(f"Данные по танцору с Compreg IDD {identity.compreg_idd} не найдены.")
        return 1

    dancer_label = numeric["dancer"].iloc[0]
    print(f"Dance analytics: {dancer_label}")
    print(f"Compreg IDD: {identity.compreg_idd}; internal_dancer_id: {identity.internal_dancer_id}")
    print(f"Клуб/город: {numeric['dancer_club'].iloc[0] or '-'} / {numeric['dancer_city'].iloc[0] or '-'}")
    dance_results = selected_dance_results_by_internal_id(conn, identity.internal_dancer_id)
    print(f"Numeric final marks: {len(numeric)}")
    print(f"Dance result rows: {len(dance_results)}")
    print(f"Protocols: {numeric['protocol_id'].nunique()}")
    print(f"Tournaments: {numeric['tournament_id'].nunique()}")
    print(f"Confidence: dance rankings require n_marks >= {MIN_MARKS_FOR_RANKING}; trends require at least {MIN_DATES_FOR_TREND} dates")

    summary = dance_summary(numeric, dance_results)
    dynamics = dynamics_by_date(dance_results)
    trends = trend_ranking(dynamics)

    print_table("All dance metrics", summary)
    tables = ranking_tables(summary, trends)
    print_table("1. Best by final average: lowest final_avg_place", tables["best_by_final_average"], max_rows=10)
    print_table("2. Best by median: lowest final_median_place", tables["best_by_median"], max_rows=10)
    print_table("3. Most stable: lowest final_std_deviation", tables["most_stable"], max_rows=10)
    print_table("4. Best peak: lowest single final_place", tables["best_peak"], max_rows=10)
    print_table("5. Worst by final average: highest final_avg_place", tables["worst_by_final_average"], max_rows=10)
    print_table("6. Judge-level best: lowest judge_avg_place", tables["judge_level_best"], max_rows=10)
    print_table("7. Improvement ranking: strongest negative trend_over_time", tables["improvement"], max_rows=10)
    print_table("8. Regression ranking: strongest positive trend_over_time", tables["regression"], max_rows=10)

    print_program_summaries(summary, trends)
    print_table("Dynamics by date", dynamics, max_rows=200)
    print_table("Missing dances", missing_dances(numeric))
    print_table("Incomplete protocol warnings", incomplete_protocol_warnings(numeric), max_rows=120)
    low_confidence = summary[summary["confidence"] != "ok"].copy()
    print_table("Confidence warnings", low_confidence)
    return 0


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Analyze dance metrics for one selected dancer.")
    parser.add_argument("--idd", required=True, help="External Compreg dancer IDD.")
    parser.add_argument("--db-path", type=Path, default=DEFAULT_DB_PATH)
    if not argv:
        parser.print_help(sys.stderr)
        raise SystemExit(2)
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    with sqlite3.connect(args.db_path) as conn:
        return analyze(conn, args.idd)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
