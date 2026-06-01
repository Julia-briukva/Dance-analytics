#!/usr/bin/env python3
"""Export dancer candidates for club Triumph from the local SQLite database."""

from __future__ import annotations

import argparse
import csv
import json
import re
import sqlite3
import sys
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB_PATH = PROJECT_ROOT / "database" / "compreg_spb_2025_2026.sqlite"
DEFAULT_OUTPUT_PATH = PROJECT_ROOT / "reports" / "club_triumph_dancers_candidates.csv"
SEARCH_TERMS = ("Триумф", "триумф")

CSV_FIELDS = [
    "idd",
    "surname",
    "name",
    "full_name",
    "club",
    "city",
    "source_table",
    "internal_dancer_id",
    "protocols_count",
    "raw_row",
]


def clean_text(value: Any) -> str:
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()


def contains_triumph(value: Any) -> bool:
    text = clean_text(value)
    return any(term in text for term in SEARCH_TERMS)


def split_name(full_name: str) -> tuple[str, str]:
    parts = clean_text(full_name).split()
    if not parts:
        return "", ""
    surname = parts[0]
    name = parts[1] if len(parts) > 1 else ""
    return surname, name


def json_row(row: dict[str, Any]) -> str:
    return json.dumps(row, ensure_ascii=False, sort_keys=True)


def quote_identifier(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def identity_key(candidate: dict[str, str]) -> tuple[str, ...]:
    idd = clean_text(candidate.get("idd"))
    internal_id = clean_text(candidate.get("internal_dancer_id"))
    full_name = clean_text(candidate.get("full_name")).casefold()
    club = clean_text(candidate.get("club")).casefold()
    city = clean_text(candidate.get("city")).casefold()

    if internal_id:
        return ("internal", internal_id)
    if idd:
        return ("idd", idd)
    return ("name_club_city", full_name, club, city)


def make_candidate(
    *,
    idd: Any = "",
    full_name: Any = "",
    club: Any = "",
    city: Any = "",
    source_table: str,
    internal_dancer_id: Any = "",
    protocols_count: Any = "",
    raw_row: dict[str, Any] | None = None,
) -> dict[str, str]:
    full_name_text = clean_text(full_name)
    surname, name = split_name(full_name_text)
    return {
        "idd": clean_text(idd),
        "surname": surname,
        "name": name,
        "full_name": full_name_text,
        "club": clean_text(club),
        "city": clean_text(city),
        "source_table": source_table,
        "internal_dancer_id": clean_text(internal_dancer_id),
        "protocols_count": clean_text(protocols_count),
        "raw_row": json_row(raw_row or {}),
    }


def merge_candidates(candidates: list[dict[str, str]]) -> list[dict[str, str]]:
    merged: dict[tuple[str, ...], dict[str, str]] = {}

    for candidate in candidates:
        key = identity_key(candidate)
        if key not in merged:
            merged[key] = dict(candidate)
            continue

        existing = merged[key]
        for field in ("idd", "surname", "name", "full_name", "club", "city", "internal_dancer_id"):
            if not existing.get(field) and candidate.get(field):
                existing[field] = candidate[field]

        sources = set(filter(None, existing.get("source_table", "").split(";")))
        sources.update(filter(None, candidate.get("source_table", "").split(";")))
        existing["source_table"] = ";".join(sorted(sources))

        try:
            existing_count = int(existing.get("protocols_count") or 0)
            candidate_count = int(candidate.get("protocols_count") or 0)
            existing["protocols_count"] = str(max(existing_count, candidate_count))
        except ValueError:
            if not existing.get("protocols_count"):
                existing["protocols_count"] = candidate.get("protocols_count", "")

        if not existing.get("raw_row") and candidate.get("raw_row"):
            existing["raw_row"] = candidate["raw_row"]

    return sorted(
        merged.values(),
        key=lambda item: (
            clean_text(item.get("full_name")).casefold(),
            clean_text(item.get("idd")),
            clean_text(item.get("club")).casefold(),
        ),
    )


def fetch_protocol_dancer_candidates(conn: sqlite3.Connection) -> list[dict[str, str]]:
    params = [f"%{term}%" for term in SEARCH_TERMS]
    rows = conn.execute(
        """
        SELECT
            pd.idd,
            d.external_ref,
            pd.dancer_name,
            d.name AS normalized_name,
            pd.club,
            pd.city,
            pd.dancer_id,
            COUNT(DISTINCT pd.protocol_id) AS protocols_count,
            MIN(pd.id) AS sample_protocol_dancer_row_id
        FROM protocol_dancers pd
        LEFT JOIN dancers d ON d.id = pd.dancer_id
        WHERE
            pd.club LIKE ? OR pd.club LIKE ?
            OR pd.dancer_name LIKE ? OR pd.dancer_name LIKE ?
            OR pd.city LIKE ? OR pd.city LIKE ?
        GROUP BY
            pd.idd,
            d.external_ref,
            pd.dancer_name,
            d.name,
            pd.club,
            pd.city,
            pd.dancer_id
        ORDER BY pd.dancer_name, pd.idd
        """,
        params * 3,
    ).fetchall()

    candidates = []
    for row in rows:
        row_dict = dict(row)
        idd = row_dict.get("idd") or row_dict.get("external_ref")
        full_name = row_dict.get("dancer_name") or row_dict.get("normalized_name")
        candidates.append(
            make_candidate(
                idd=idd,
                full_name=full_name,
                club=row_dict.get("club"),
                city=row_dict.get("city"),
                source_table="protocol_dancers",
                internal_dancer_id=row_dict.get("dancer_id"),
                protocols_count=row_dict.get("protocols_count"),
                raw_row=row_dict,
            )
        )
    return candidates


def fetch_dancer_candidates(conn: sqlite3.Connection) -> list[dict[str, str]]:
    params = [f"%{term}%" for term in SEARCH_TERMS]
    rows = conn.execute(
        """
        SELECT id, external_ref, name, club, city
        FROM dancers
        WHERE
            club LIKE ? OR club LIKE ?
            OR name LIKE ? OR name LIKE ?
            OR city LIKE ? OR city LIKE ?
        ORDER BY name
        """,
        params * 3,
    ).fetchall()

    return [
        make_candidate(
            idd=row["external_ref"],
            full_name=row["name"],
            club=row["club"],
            city=row["city"],
            source_table="dancers",
            internal_dancer_id=row["id"],
            raw_row=dict(row),
        )
        for row in rows
    ]


def table_names(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        """
        SELECT name
        FROM sqlite_master
        WHERE type = 'table'
            AND name NOT LIKE 'sqlite_%'
        ORDER BY name
        """
    ).fetchall()
    return [row["name"] for row in rows]


def text_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    columns = conn.execute(f"PRAGMA table_info({quote_identifier(table)})").fetchall()
    result = []
    for column in columns:
        column_type = clean_text(column["type"]).upper()
        if "TEXT" in column_type or column_type == "":
            result.append(column["name"])
    return result


def extract_idd_from_row(row: dict[str, Any]) -> str:
    for key in ("idd", "external_ref", "compreg_idd"):
        value = clean_text(row.get(key))
        if value:
            return value
    raw_text = " ".join(clean_text(value) for value in row.values())
    match = re.search(r"\b20\d{5}\b", raw_text)
    return match.group(0) if match else ""


def fetch_universal_candidates(conn: sqlite3.Connection) -> list[dict[str, str]]:
    candidates = []

    for table in table_names(conn):
        columns = text_columns(conn, table)
        if not columns:
            continue

        rows = conn.execute(f"SELECT * FROM {quote_identifier(table)}").fetchall()
        for row in rows:
            row_dict = dict(row)
            if not any(contains_triumph(row_dict.get(column)) for column in columns):
                continue

            full_name = row_dict.get("dancer_name") or row_dict.get("dancer") or row_dict.get("name") or ""
            club = row_dict.get("club") or row_dict.get("dancer_club") or ""
            city = row_dict.get("city") or row_dict.get("dancer_city") or ""
            internal_dancer_id = row_dict.get("dancer_id") or (row_dict.get("id") if table == "dancers" else "")

            if not any((full_name, club, city, internal_dancer_id, extract_idd_from_row(row_dict))):
                continue

            candidates.append(
                make_candidate(
                    idd=extract_idd_from_row(row_dict),
                    full_name=full_name,
                    club=club,
                    city=city,
                    source_table=table,
                    internal_dancer_id=internal_dancer_id,
                    raw_row=row_dict,
                )
            )

    return candidates


def export_candidates(db_path: Path, output_path: Path) -> list[dict[str, str]]:
    if not db_path.exists():
        raise FileNotFoundError(f"SQLite database not found: {db_path}")

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        raw_candidates = []
        raw_candidates.extend(fetch_protocol_dancer_candidates(conn))
        raw_candidates.extend(fetch_dancer_candidates(conn))
        raw_candidates.extend(fetch_universal_candidates(conn))
        candidates = merge_candidates(raw_candidates)
    finally:
        conn.close()

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", newline="", encoding="utf-8") as file_obj:
        writer = csv.DictWriter(file_obj, fieldnames=CSV_FIELDS)
        writer.writeheader()
        writer.writerows(candidates)

    return candidates


def print_preview(candidates: list[dict[str, str]], output_path: Path) -> None:
    unique_idds = {candidate["idd"] for candidate in candidates if candidate.get("idd")}
    print(f"Rows found: {len(candidates)}")
    print(f"Unique IDD: {len(unique_idds)}")
    print(f"CSV path: {output_path}")
    print()
    print("First 20 rows:")

    preview_fields = ["idd", "surname", "name", "full_name", "club", "city", "source_table"]
    writer = csv.DictWriter(
        sys.stdout,
        fieldnames=preview_fields,
        extrasaction="ignore",
    )
    writer.writeheader()
    writer.writerows(candidates[:20])


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Export unique dancer candidates whose local database rows mention club Triumph."
    )
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH, help="Path to SQLite database.")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT_PATH, help="Path to output CSV.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    candidates = export_candidates(args.db, args.output)
    print_preview(candidates, args.output)


if __name__ == "__main__":
    main()
