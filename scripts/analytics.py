#!/usr/bin/env python3
"""Small dancer-scoped analytics helpers for the local Dance database."""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

import pandas as pd

from analyze_dancer import DEFAULT_DB_PATH, resolve_dancer_by_idd, selected_marks_by_internal_id

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def get_marks_for_dancer(conn: sqlite3.Connection, compreg_idd: str | int) -> pd.DataFrame:
    """Return normalized marks for a single external Compreg IDD."""
    identity = resolve_dancer_by_idd(conn, compreg_idd)
    return selected_marks_by_internal_id(conn, identity.internal_dancer_id)


def print_dancer_summary(conn: sqlite3.Connection, compreg_idd: str | int) -> None:
    identity = resolve_dancer_by_idd(conn, compreg_idd)
    marks = selected_marks_by_internal_id(conn, identity.internal_dancer_id)
    print(f"Marks for {identity.name} ({identity.compreg_idd}): {len(marks)}")
    if marks.empty:
        return

    protocol_ids = marks["protocol_id"].drop_duplicates().tolist()
    print("\nProtocol IDs:")
    print(", ".join(str(item) for item in protocol_ids))

    print("\nSample rows:")
    columns = ["protocol_id", "event_date", "category", "round", "dance", "judge", "dancer", "mark", "place"]
    print(marks[columns].head(20).to_string(index=False))

    numeric_marks = marks.dropna(subset=["numeric_mark"])
    print("\nAverage numeric marks by dance:")
    if numeric_marks.empty:
        print("No numeric marks found for this dancer.")
    else:
        summary = (
            numeric_marks.groupby("dance", as_index=False)
            .agg(avg_mark=("numeric_mark", "mean"), marks=("numeric_mark", "count"))
            .sort_values("dance")
        )
        summary["avg_mark"] = summary["avg_mark"].round(3)
        print(summary.to_string(index=False))


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run dancer-scoped analytics queries.")
    parser.add_argument("--idd", required=True, help="External Compreg dancer IDD.")
    parser.add_argument("--db-path", type=Path, default=DEFAULT_DB_PATH)
    if not argv:
        parser.print_help(sys.stderr)
        raise SystemExit(2)
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    with sqlite3.connect(args.db_path) as conn:
        print_dancer_summary(conn, args.idd)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
