#!/usr/bin/env python3
"""Analyze normalized marks for one selected dancer."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import re
import sqlite3
import sys
from pathlib import Path

import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB_PATH = PROJECT_ROOT / "database" / "compreg_spb_2025_2026.sqlite"
SPACE_RE = re.compile(r"\s+")
STANDARD_DANCES = {"W", "T", "V", "F", "Q"}
LATIN_DANCES = {"S", "C", "R", "P", "J"}


@dataclass(frozen=True)
class DancerIdentity:
    internal_dancer_id: int
    compreg_idd: str
    name: str
    club: str | None
    city: str | None


def normalize_person_name(value: str | None) -> str:
    if not value:
        return ""
    value = value.lower().replace("ё", "е")
    value = re.sub(r"[^a-zа-я0-9]+", " ", value)
    return SPACE_RE.sub(" ", value).strip()


def ensure_analytics_view(conn: sqlite3.Connection) -> None:
    conn.create_function("normalize_person_name", 1, normalize_person_name)
    ensure_mark_type_column(conn)
    conn.execute("DROP VIEW IF EXISTS marks_enriched")
    conn.execute(
        """
        CREATE VIEW marks_enriched AS
        SELECT
            m.id AS mark_id,
            p.id AS protocol_db_id,
            p.protocol_id AS protocol_id,
            p.tournament_id AS tournament_id,
            p.tournament_title AS tournament_title,
            p.event_date AS event_date,
            p.city AS tournament_city,
            p.category AS category,
            m.round_name AS round,
            m.dance_name AS dance,
            j.id AS judge_id,
            j.name AS judge,
            m.judge_index AS judge_index,
            m.judge_position AS judge_position,
            d.id AS dancer_id,
            d.external_ref AS compreg_idd,
            d.name AS dancer,
            normalize_person_name(d.name) AS dancer_normalized,
            d.club AS dancer_club,
            d.city AS dancer_city,
            m.competitor_number AS competitor_number,
            m.mark_value AS mark,
            m.mark_type AS mark_type,
            CASE WHEN m.mark_value GLOB '[0-9]*' THEN CAST(m.mark_value AS REAL) ELSE NULL END AS numeric_mark,
            CASE
                WHEN lower(m.round_name) LIKE '%финал%'
                 AND lower(m.round_name) NOT LIKE '%1-2 финал%'
                 AND lower(m.round_name) NOT LIKE '%1-4 финал%'
                 AND lower(m.round_name) NOT LIKE '%1-8 финал%'
                 AND lower(m.round_name) NOT LIKE '%1\\2 финал%'
                 AND lower(m.round_name) NOT LIKE '%1\\4 финал%'
                 AND lower(m.round_name) NOT LIKE '%1\\8 финал%'
                THEN 1 ELSE 0
            END AS is_final_round,
            m.place_value AS place,
            m.raw_mark_string AS raw_mark_string
        FROM marks m
        JOIN protocols p ON p.id = m.protocol_id
        JOIN dancers d ON d.id = m.dancer_id
        JOIN judges j ON j.id = m.judge_id;
        """
    )


def ensure_mark_type_column(conn: sqlite3.Connection) -> None:
    columns = {row[1] for row in conn.execute("PRAGMA table_info(marks)")}
    if "mark_type" not in columns:
        conn.execute("ALTER TABLE marks ADD COLUMN mark_type TEXT")
    conn.execute(
        """
        UPDATE marks
        SET mark_type = CASE
            WHEN mark_value IS NULL OR TRIM(mark_value) = '' THEN 'empty'
            WHEN mark_value GLOB '[0-9]*' THEN 'numeric_place'
            WHEN LENGTH(mark_value) = 1 AND mark_value GLOB '[A-ZА-Я]' THEN 'cross'
            ELSE 'unknown'
        END
        WHERE mark_type IS NULL OR mark_type = '';
        """
    )


def resolve_dancer_by_idd(conn: sqlite3.Connection, compreg_idd: str | int) -> DancerIdentity:
    ensure_analytics_view(conn)
    idd = str(compreg_idd).strip()
    rows = conn.execute(
        """
        SELECT id, external_ref, name, club, city
        FROM dancers
        WHERE external_ref = ?
        ORDER BY id;
        """,
        (idd,),
    ).fetchall()
    if not rows:
        raise ValueError(f"Танцор с Compreg IDD {idd} не найден в базе.")
    if len(rows) > 1:
        matches = ", ".join(f"{row[0]}:{row[2]}" for row in rows[:10])
        raise ValueError(f"Compreg IDD {idd} неоднозначен в базе: {matches}")
    row = rows[0]
    return DancerIdentity(
        internal_dancer_id=int(row[0]),
        compreg_idd=str(row[1]),
        name=str(row[2]),
        club=row[3],
        city=row[4],
    )


def selected_marks_by_internal_id(conn: sqlite3.Connection, internal_dancer_id: int) -> pd.DataFrame:
    ensure_analytics_view(conn)
    return pd.read_sql_query(
        """
        SELECT *
        FROM marks_enriched
        WHERE dancer_id = ?
        ORDER BY event_date, protocol_id, mark_id;
        """,
        conn,
        params=(internal_dancer_id,),
    )


def infer_program(dance: str | None, category: str | None) -> str:
    dance_value = (dance or "").strip().upper()
    category_value = f" {category or ''} ".lower()
    if dance_value in STANDARD_DANCES or re.search(r"\bst\b", category_value):
        return "standard"
    if dance_value in LATIN_DANCES or re.search(r"\bla\b", category_value):
        return "latin"
    return "unknown"


def infer_entry_type(category: str | None) -> str:
    category_value = (category or "").lower()
    if "соло" in category_value or "solo" in category_value:
        return "solo"
    if category_value.strip():
        return "pair_or_unknown"
    return "unknown"


def add_derived_columns(marks: pd.DataFrame) -> pd.DataFrame:
    if marks.empty:
        return marks
    marks = marks.copy()
    marks["program"] = marks.apply(lambda row: infer_program(row.get("dance"), row.get("category")), axis=1)
    marks["entry_type"] = marks["category"].map(infer_entry_type)
    return marks


def print_table(title: str, df: pd.DataFrame, columns: list[str] | None = None, max_rows: int | None = None) -> None:
    print(f"\n{title}")
    if df.empty:
        print("  Нет данных")
        return
    view = df[columns] if columns else df
    if max_rows:
        view = view.head(max_rows)
    print(view.to_string(index=False))


def confidence_label(count: int, threshold: int = 3) -> str:
    return "low confidence" if count < threshold else "ok"


def format_float(value: float | int | None, digits: int = 3) -> str:
    if pd.isna(value):
        return "-"
    return f"{float(value):.{digits}f}"


def print_summary(marks: pd.DataFrame, numeric: pd.DataFrame, crosses: pd.DataFrame) -> None:
    protocol_count = marks["protocol_id"].nunique()
    tournament_count = marks["tournament_id"].nunique()
    print("\nA. Общая сводка")
    print(f"  Количество протоколов: {protocol_count}")
    print(f"  Количество турниров: {tournament_count}")
    print(f"  Записей numeric_place: {len(numeric)}")
    print(f"  Записей cross: {len(crosses)}")


def final_dance_stats(numeric: pd.DataFrame) -> pd.DataFrame:
    if numeric.empty:
        return pd.DataFrame()
    stats = (
        numeric.groupby("dance", as_index=False)
        .agg(
            avg_place=("numeric_mark", "mean"),
            median_place=("numeric_mark", "median"),
            std_place=("numeric_mark", "std"),
            marks=("numeric_mark", "count"),
            protocols=("protocol_id", "nunique"),
        )
        .sort_values("dance")
    )
    stats["std_place"] = stats["std_place"].fillna(0)
    stats["confidence"] = stats["marks"].map(confidence_label)
    for column in ["avg_place", "median_place", "std_place"]:
        stats[column] = stats[column].round(3)
    return stats


def final_dance_dynamics(numeric: pd.DataFrame) -> pd.DataFrame:
    if numeric.empty:
        return pd.DataFrame()
    dynamics = (
        numeric.groupby(["event_date", "dance"], as_index=False)
        .agg(avg_place=("numeric_mark", "mean"), median_place=("numeric_mark", "median"), marks=("numeric_mark", "count"))
        .sort_values(["event_date", "dance"])
    )
    dynamics["confidence"] = dynamics["marks"].map(confidence_label)
    for column in ["avg_place", "median_place"]:
        dynamics[column] = dynamics[column].round(3)
    return dynamics


def cross_dance_stats(crosses: pd.DataFrame) -> pd.DataFrame:
    if crosses.empty:
        return pd.DataFrame()
    stats = (
        crosses.groupby("dance", as_index=False)
        .agg(crosses=("mark", "count"), protocols=("protocol_id", "nunique"), rounds=("round", "nunique"))
        .sort_values("dance")
    )
    stats["confidence"] = stats["crosses"].map(confidence_label)
    return stats


def cross_dance_dynamics(crosses: pd.DataFrame) -> pd.DataFrame:
    if crosses.empty:
        return pd.DataFrame()
    dynamics = (
        crosses.groupby(["event_date", "dance"], as_index=False)
        .agg(crosses=("mark", "count"), protocols=("protocol_id", "nunique"))
        .sort_values(["event_date", "dance"])
    )
    dynamics["confidence"] = dynamics["crosses"].map(confidence_label)
    return dynamics


def final_judge_stats(numeric: pd.DataFrame) -> pd.DataFrame:
    if numeric.empty:
        return pd.DataFrame()
    panel = numeric.groupby(["protocol_id", "round", "dance"], as_index=False).agg(panel_mean=("numeric_mark", "mean"))
    with_panel = numeric.merge(panel, on=["protocol_id", "round", "dance"], how="left")
    # Formula: deviation = judge_mark - panel_mean. Bigger places are worse, so positive deviation is stricter.
    with_panel["deviation"] = with_panel["numeric_mark"] - with_panel["panel_mean"]
    stats = (
        with_panel.groupby("judge", as_index=False)
        .agg(
            n_marks=("numeric_mark", "count"),
            protocols=("protocol_id", "nunique"),
            avg_judge_mark=("numeric_mark", "mean"),
            avg_panel_mean=("panel_mean", "mean"),
            avg_deviation=("deviation", "mean"),
        )
        .sort_values(["avg_deviation", "n_marks"], ascending=[False, False])
    )
    stats["strictness"] = stats["avg_deviation"]
    stats["softness"] = -stats["avg_deviation"]
    stats["confidence"] = stats["n_marks"].map(lambda count: confidence_label(count, threshold=12))
    for column in ["avg_judge_mark", "avg_panel_mean", "avg_deviation", "strictness", "softness"]:
        stats[column] = stats[column].round(3)
    return stats


def judge_comparison_table(numeric: pd.DataFrame) -> pd.DataFrame:
    stats = final_judge_stats(numeric)
    if stats.empty:
        return stats
    return stats[["judge", "n_marks", "avg_judge_mark", "avg_panel_mean", "avg_deviation", "confidence"]]


def judge_stats_by_program(numeric: pd.DataFrame) -> pd.DataFrame:
    rows: list[pd.DataFrame] = []
    for program, subset in [("all", numeric), ("standard", numeric[numeric["program"] == "standard"]), ("latin", numeric[numeric["program"] == "latin"] )]:
        stats = final_judge_stats(subset)
        if stats.empty:
            continue
        stats.insert(1, "program", program)
        rows.append(stats)
    if not rows:
        return pd.DataFrame()
    return pd.concat(rows, ignore_index=True)


def print_judge_rankings_by_program(title: str, numeric: pd.DataFrame, threshold: int = 12) -> None:
    print(f"\n{title}")
    stats = judge_stats_by_program(numeric)
    if stats.empty:
        print("  Нет данных")
        return
    comparison = stats[["judge", "program", "n_marks", "avg_judge_mark", "avg_panel_mean", "avg_deviation", "confidence"]]
    print_table("Comparison table", comparison.sort_values(["program", "avg_deviation", "n_marks"], ascending=[True, False, False]), max_rows=180)
    for program in ["all", "standard", "latin"]:
        subset = stats[(stats["program"] == program) & (stats["n_marks"] >= threshold)].copy()
        low = stats[(stats["program"] == program) & (stats["n_marks"] < threshold)].copy()
        print_table(
            f"Strictest {program}: n_marks >= {threshold}",
            subset.sort_values(["strictness", "n_marks"], ascending=[False, False]).head(10),
        )
        print_table(
            f"Softest {program}: n_marks >= {threshold}",
            subset.sort_values(["softness", "n_marks"], ascending=[False, False]).head(10),
        )
        print_table(
            f"Low confidence {program}: n_marks < {threshold}",
            low.sort_values(["strictness", "n_marks"], ascending=[False, False]).head(15),
        )


def final_judge_audit_rows(numeric: pd.DataFrame, judges: list[str]) -> pd.DataFrame:
    if numeric.empty or not judges:
        return pd.DataFrame()
    panel = numeric.groupby(["protocol_id", "round", "dance"], as_index=False).agg(panel_mean=("numeric_mark", "mean"))
    audit = numeric.merge(panel, on=["protocol_id", "round", "dance"], how="left")
    audit = audit[audit["judge"].isin(judges)].copy()
    audit["judge_mark"] = audit["numeric_mark"]
    audit["deviation"] = audit["judge_mark"] - audit["panel_mean"]
    for column in ["judge_mark", "panel_mean", "deviation"]:
        audit[column] = audit[column].round(3)
    columns = [
        "event_date",
        "protocol_id",
        "tournament_title",
        "program",
        "category",
        "round",
        "dance",
        "judge",
        "judge_mark",
        "panel_mean",
        "deviation",
    ]
    return audit[columns].sort_values(["judge", "event_date", "protocol_id", "dance"])


def data_scope_audit(marks: pd.DataFrame) -> None:
    print("\nAudit scope: выбранный танцор и смешения")
    dancer_scope = (
        marks.groupby(["dancer_id", "dancer", "dancer_club", "dancer_city"], dropna=False)
        .size()
        .reset_index(name="marks")
        .sort_values("marks", ascending=False)
    )
    print_table("Dancer rows included", dancer_scope)
    print_table(
        "Category/program/entry_type mix",
        marks[["category", "program", "entry_type", "protocol_id"]]
        .drop_duplicates()
        .groupby(["program", "entry_type", "category"], as_index=False)
        .agg(protocols=("protocol_id", "nunique"))
        .sort_values(["program", "entry_type", "category"]),
        max_rows=120,
    )
    print_table(
        "Mark types included",
        marks.groupby("mark_type", as_index=False).size().rename(columns={"size": "rows"}).sort_values("mark_type"),
    )


def final_judge_sanity_check(numeric: pd.DataFrame, rows: int = 5) -> pd.DataFrame:
    if numeric.empty:
        return pd.DataFrame()
    panel = numeric.groupby(["protocol_id", "round", "dance"], as_index=False).agg(panel_mean=("numeric_mark", "mean"))
    sample = numeric.merge(panel, on=["protocol_id", "round", "dance"], how="left")
    sample["deviation"] = sample["numeric_mark"] - sample["panel_mean"]
    sample = sample[["protocol_id", "dance", "judge", "numeric_mark", "panel_mean", "deviation"]].rename(
        columns={"numeric_mark": "judge_mark"}
    )
    for column in ["judge_mark", "panel_mean", "deviation"]:
        sample[column] = sample[column].round(3)
    return sample.sort_values(["protocol_id", "dance", "judge"]).head(rows)


def cross_judge_stats(crosses: pd.DataFrame) -> pd.DataFrame:
    if crosses.empty:
        return pd.DataFrame()
    panel = crosses.groupby(["protocol_id", "round", "dance"], as_index=False).agg(panel_crosses=("mark", "count"))
    with_panel = crosses.merge(panel, on=["protocol_id", "round", "dance"], how="left")
    with_panel["panel_share"] = 1 / with_panel["panel_crosses"].where(with_panel["panel_crosses"] > 0)
    stats = (
        with_panel.groupby("judge", as_index=False)
        .agg(
            crosses=("mark", "count"),
            protocols=("protocol_id", "nunique"),
            dances=("dance", "nunique"),
            avg_panel_share=("panel_share", "mean"),
        )
        .sort_values(["crosses", "protocols"], ascending=[False, False])
    )
    stats["confidence"] = stats["crosses"].map(confidence_label)
    stats["avg_panel_share"] = stats["avg_panel_share"].round(3)
    return stats


def final_dance_extremes(numeric: pd.DataFrame) -> pd.DataFrame:
    stats = final_dance_stats(numeric)
    if stats.empty:
        return pd.DataFrame()

    rows: list[dict[str, object]] = []
    eligible = stats[stats["marks"] >= 3]
    if not eligible.empty:
        stable = eligible.sort_values(["std_place", "marks"], ascending=[True, False]).iloc[0]
        strongest = eligible.sort_values(["avg_place", "marks"], ascending=[True, False]).iloc[0]
        weakest = eligible.sort_values(["avg_place", "marks"], ascending=[False, False]).iloc[0]
        rows.extend(
            [
                {"metric": "most stable dance", "dance": stable["dance"], "value": format_float(stable["std_place"]), "basis": f"std by {int(stable['marks'])} marks", "confidence": stable["confidence"]},
                {"metric": "strongest dance", "dance": strongest["dance"], "value": format_float(strongest["avg_place"]), "basis": f"avg place by {int(strongest['marks'])} marks", "confidence": strongest["confidence"]},
                {"metric": "weakest dance", "dance": weakest["dance"], "value": format_float(weakest["avg_place"]), "basis": f"avg place by {int(weakest['marks'])} marks", "confidence": weakest["confidence"]},
            ]
        )

    improvements: list[dict[str, object]] = []
    for dance, group in numeric.groupby("dance"):
        by_date = group.groupby("event_date", as_index=False).agg(avg_place=("numeric_mark", "mean"), marks=("numeric_mark", "count"))
        by_date = by_date.sort_values("event_date")
        if len(by_date) < 2:
            continue
        first = by_date.iloc[0]
        last = by_date.iloc[-1]
        improvement = float(first["avg_place"] - last["avg_place"])
        improvements.append(
            {
                "dance": dance,
                "improvement": improvement,
                "basis": f"{first['event_date']} {format_float(first['avg_place'])} -> {last['event_date']} {format_float(last['avg_place'])}; {int(group['numeric_mark'].count())} marks",
                "confidence": confidence_label(int(group["numeric_mark"].count())),
            }
        )
    if improvements:
        improved = sorted(improvements, key=lambda item: item["improvement"], reverse=True)[0]
        rows.append({"metric": "most improved dance", "dance": improved["dance"], "value": format_float(improved["improvement"]), "basis": improved["basis"], "confidence": improved["confidence"]})
    return pd.DataFrame(rows)


def print_debug_diagnostics(conn: sqlite3.Connection, marks: pd.DataFrame) -> None:
    print("\nDEBUG: PRAGMA table_info(marks)")
    for row in conn.execute("PRAGMA table_info(marks)"):
        print(row)

    print("\nDEBUG: SELECT * FROM marks LIMIT 20")
    cur = conn.execute("SELECT * FROM marks LIMIT 20")
    print([item[0] for item in cur.description])
    for row in cur.fetchall():
        print(row)

    debug_queries = [
        ("DEBUG: distinct dances", "SELECT DISTINCT dance_name AS dance FROM marks ORDER BY dance_name"),
        ("DEBUG: distinct rounds", "SELECT DISTINCT round_name AS round FROM marks ORDER BY round_name"),
        ("DEBUG: counts by round", "SELECT round_name AS round, COUNT(*) FROM marks GROUP BY round_name ORDER BY COUNT(*) DESC"),
        ("DEBUG: counts by dance", "SELECT dance_name AS dance, COUNT(*) FROM marks GROUP BY dance_name ORDER BY dance_name"),
        ("DEBUG: counts by protocol", "SELECT protocol_id, COUNT(*) FROM marks GROUP BY protocol_id ORDER BY protocol_id"),
        ("DEBUG: counts by mark_type", "SELECT mark_type, COUNT(*) FROM marks GROUP BY mark_type ORDER BY mark_type"),
    ]
    for title, sql in debug_queries:
        print("\n" + title)
        rows = conn.execute(sql).fetchall()
        for row in rows[:200]:
            print(row)
        if len(rows) > 200:
            print(f"... {len(rows) - 200} rows omitted")

    print("\nDEBUG: selected dancer mark_type counts")
    if marks.empty:
        print("No selected dancer marks.")
    else:
        print(marks.groupby("mark_type").size().reset_index(name="rows").to_string(index=False))


def analyze(conn: sqlite3.Connection, compreg_idd: str | int, debug: bool = False) -> int:
    ensure_analytics_view(conn)
    try:
        identity = resolve_dancer_by_idd(conn, compreg_idd)
    except ValueError as exc:
        print(str(exc))
        return 1

    marks = add_derived_columns(selected_marks_by_internal_id(conn, identity.internal_dancer_id))
    if debug:
        print_debug_diagnostics(conn, marks)
    if marks.empty:
        print(f"Данные по танцору с Compreg IDD {identity.compreg_idd} не найдены.")
        return 1

    numeric_all = marks[marks["mark_type"] == "numeric_place"].dropna(subset=["numeric_mark"]).copy()
    numeric = numeric_all[numeric_all["is_final_round"] == 1].copy()
    crosses = marks[marks["mark_type"] == "cross"].copy()
    dancer_label = marks["dancer"].iloc[0]
    print(f"Аналитика танцора: {dancer_label}")
    print(f"Compreg IDD: {identity.compreg_idd}; internal_dancer_id: {identity.internal_dancer_id}")
    print(f"Клуб/город: {marks['dancer_club'].iloc[0] or '-'} / {marks['dancer_city'].iloc[0] or '-'}")
    print_summary(marks, numeric, crosses)
    data_scope_audit(marks)

    tournaments = (
        marks[["event_date", "tournament_id", "tournament_title", "protocol_id", "category"]]
        .drop_duplicates()
        .sort_values(["event_date", "protocol_id"])
    )
    print_table("Список турниров и протоколов", tournaments, max_rows=120)

    print("\nB. Анализ финальных числовых мест (только mark_type = numeric_place)")
    print_table("Статистика по танцам", final_dance_stats(numeric))
    print_table("Динамика по танцам по датам", final_dance_dynamics(numeric), max_rows=160)

    judge_stats = final_judge_stats(numeric)
    print("\nC. Статистика по судьям (финальные места)")
    print("Формула: avg_deviation = avg(judge_mark - panel_mean)")
    print("strictness = avg_deviation; positive strictness means judge placed dancer worse than panel average")
    print("softness = -avg_deviation; positive softness means judge placed dancer better than panel average")
    print_table("Sanity check: judge_mark - panel_mean", final_judge_sanity_check(numeric), max_rows=5)
    print_table("Comparison table: финалы, все программы", judge_comparison_table(numeric), max_rows=120)
    strict_top = judge_stats[judge_stats["n_marks"] >= 12].sort_values(["strictness", "n_marks"], ascending=[False, False]).head(10)
    strict_low = judge_stats[judge_stats["n_marks"] < 12].sort_values(["strictness", "n_marks"], ascending=[False, False]).head(15)
    print_table("D. Самые строгие судьи: final only, all programs, n_marks >= 12", strict_top)
    print_table("D-low. Строгие судьи с низкой уверенностью: final only, n_marks < 12", strict_low)
    print_table("E. Самые мягкие судьи: final only, all programs, n_marks >= 12", judge_stats[judge_stats["n_marks"] >= 12].sort_values(["softness", "n_marks"], ascending=[False, False]).head(10))

    audit_judges = strict_top["judge"].tolist()[:5]
    if len(audit_judges) < 5:
        audit_judges.extend(strict_low[~strict_low["judge"].isin(audit_judges)]["judge"].tolist()[: 5 - len(audit_judges)])
    print_table("Audit rows for top strict judges: final numeric places", final_judge_audit_rows(numeric, audit_judges), max_rows=300)

    print_judge_rankings_by_program("Разделение strictness по программам: final only", numeric, threshold=12)
    print_judge_rankings_by_program("Разделение strictness по программам: all numeric places", numeric_all, threshold=12)

    print("\nF. Стабильность танцев (финальные места)")
    print_table("Итоги", final_dance_extremes(numeric))

    print("\nАнализ проходов/крестов (только mark_type = cross, отдельно от numeric_place)")
    print("Для crosses в базе сохранены только записанные кресты; отсутствующие кресты как отрицательные решения пока не нормализованы, поэтому это не та же strictness-метрика, что numeric places.")
    print_table("Кресты по танцам", cross_dance_stats(crosses))
    print_table("Динамика крестов по датам", cross_dance_dynamics(crosses), max_rows=160)
    print_table("Судьи по крестам", cross_judge_stats(crosses), max_rows=120)
    print_table("Semifinal/qualification crosses only: judges by recorded crosses", cross_judge_stats(crosses), max_rows=120)
    return 0


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Analyze marks for one dancer only.")
    parser.add_argument("--idd", required=True, help="External Compreg dancer IDD.")
    parser.add_argument("--db-path", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--debug", action="store_true", help="Print SQLite diagnostics for marks quality checks.")
    if not argv:
        parser.print_help(sys.stderr)
        raise SystemExit(2)
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    with sqlite3.connect(args.db_path) as conn:
        return analyze(conn, args.idd, debug=args.debug)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
