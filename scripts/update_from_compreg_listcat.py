#!/usr/bin/env python3
"""Import one Compreg tournament from a listcat URL and parse its protocols."""

from __future__ import annotations

import argparse
import re
import sqlite3
import subprocess
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from build_spb_database import (
    Candidate,
    DEFAULT_DB_PATH,
    DEFAULT_WORKERS,
    PROJECT_ROOT,
    init_db,
    load_listcat_pages,
    load_protocol_pages,
    print_database_summary,
    save_metadata,
    setup_logging as setup_crawler_logging,
)
from parse_protocols import ProtocolRecord, init_schema, mark_status, parse_protocol, setup_logging as setup_parser_logging


LISTCAT_URL_RE = re.compile(
    r"^https?://compreg\.ru/resultsdata/(?P<year>\d{4})/(?P<month>\d{2})/(?P<day_suffix>\d{4})/listcat\.php$"
)


@dataclass(frozen=True)
class ParsedListcatUrl:
    url: str
    event_date: date
    suffix: int
    tournament_id: int
    relative_key: str


def parse_listcat_url(url: str) -> ParsedListcatUrl:
    match = LISTCAT_URL_RE.match(url.strip())
    if not match:
        raise ValueError("Expected URL like https://compreg.ru/resultsdata/YYYY/MM/DDXX/listcat.php")
    year = int(match.group("year"))
    month = int(match.group("month"))
    day_suffix = match.group("day_suffix")
    day = int(day_suffix[:2])
    suffix = int(day_suffix[2:])
    event_date = date(year, month, day)
    tournament_id = int(f"{year:04d}{month:02d}{day_suffix}")
    relative_key = f"{year:04d}/{month:02d}/{day_suffix}/listcat.php"
    return ParsedListcatUrl(
        url=url.strip(),
        event_date=event_date,
        suffix=suffix,
        tournament_id=tournament_id,
        relative_key=relative_key,
    )


def protocol_ids_for_tournament(conn: sqlite3.Connection, tournament_id: int) -> list[int]:
    return [
        int(row[0])
        for row in conn.execute(
            """
            SELECT id
            FROM protocols
            WHERE tournament_id = ?
              AND protocol_cache_path IS NOT NULL
            ORDER BY protocol_id
            """,
            (tournament_id,),
        ).fetchall()
    ]


def protocol_ids_to_parse(conn: sqlite3.Connection, tournament_id: int, reparse_tournament: bool) -> list[int]:
    if reparse_tournament:
        return protocol_ids_for_tournament(conn, tournament_id)
    return [
        int(row[0])
        for row in conn.execute(
            """
            SELECT p.id
            FROM protocols p
            LEFT JOIN protocol_parse_status s ON s.protocol_id = p.id
            WHERE p.tournament_id = ?
              AND p.protocol_cache_path IS NOT NULL
              AND COALESCE(s.status, '') NOT IN ('parsed', 'partial')
            ORDER BY p.protocol_id
            """,
            (tournament_id,),
        ).fetchall()
    ]


def parse_selected_protocols(conn: sqlite3.Connection, protocol_db_ids: list[int]) -> dict[str, int]:
    counts = {"parsed": 0, "partial": 0, "skipped": 0, "error": 0}
    setup_parser_logging(False)
    for db_id in protocol_db_ids:
        row = conn.execute(
            """
            SELECT id, tournament_id, protocol_id, protocol_cache_path
            FROM protocols
            WHERE id = ?
            """,
            (db_id,),
        ).fetchone()
        if not row or not row[3]:
            counts["skipped"] += 1
            continue
        record = ProtocolRecord(int(row[0]), int(row[1]), int(row[2]), PROJECT_ROOT / row[3])
        try:
            status, _marks_count = parse_protocol(conn, record)
        except Exception as exc:  # noqa: BLE001 - keep the batch import alive
            mark_status(conn, record.db_id, "error", str(exc))
            status = "error"
        counts[status if status in counts else "skipped"] += 1
    return counts


def run_command(command: list[str]) -> None:
    completed = subprocess.run(command, cwd=PROJECT_ROOT, check=False)
    if completed.returncode != 0:
        raise RuntimeError(f"Command failed with exit code {completed.returncode}: {' '.join(command)}")


def import_listcat(args: argparse.Namespace) -> int:
    parsed_url = parse_listcat_url(args.url)
    candidate = Candidate(
        event_date=parsed_url.event_date,
        suffix=parsed_url.suffix,
        url=parsed_url.url,
        relative_key=parsed_url.relative_key,
    )

    setup_crawler_logging(args.debug_city)
    init_db(args.db_path)

    pages, rejected_pages, check_records, stats = load_listcat_pages(
        [candidate],
        workers=args.workers,
        refresh=args.refresh,
        city_protocol_sample=args.city_protocol_sample,
        checked_urls=set(),
    )
    protocol_results = load_protocol_pages(pages, workers=args.workers, refresh=args.refresh)
    save_metadata(
        args.db_path,
        pages,
        rejected_pages,
        check_records,
        protocol_results,
        prune_existing=False,
    )

    print_database_summary(args.db_path)
    print(
        "\nImport stats:\n"
        f"listcats_found={stats.listcats_found}\n"
        f"accepted_spb_pages={stats.spb_found}\n"
        f"rejected_pages={stats.rejected}\n"
        f"protocol_pages={len(protocol_results)}"
    )

    if not pages:
        if rejected_pages:
            rejected = rejected_pages[0]
            print(
                "\nTournament was found but rejected by city filter:\n"
                f"tournament_id={rejected.tournament_id}\n"
                f"city={rejected.raw_detected_city or 'unknown'}\n"
                f"reason={rejected.reject_reason}"
            )
        else:
            print("\nNo tournament protocols were imported for this URL.")
        return 1

    with sqlite3.connect(args.db_path) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        init_schema(conn)
        all_protocols = protocol_ids_for_tournament(conn, parsed_url.tournament_id)
        to_parse = [] if args.skip_parse else protocol_ids_to_parse(conn, parsed_url.tournament_id, args.reparse_tournament)
        parse_counts = parse_selected_protocols(conn, to_parse)
        conn.commit()

    print(
        "\nParse stats:\n"
        f"tournament_id={parsed_url.tournament_id}\n"
        f"protocols_in_tournament={len(all_protocols)}\n"
        f"protocols_selected_for_parse={len(to_parse)}\n"
        f"parsed={parse_counts['parsed']}\n"
        f"partial={parse_counts['partial']}\n"
        f"skipped={parse_counts['skipped']}\n"
        f"errors={parse_counts['error']}"
    )

    if args.build_reports:
        run_command([sys.executable, str(PROJECT_ROOT / "scripts" / "build_club_reports.py")])
    if args.deploy_pages:
        run_command([sys.executable, str(PROJECT_ROOT / "scripts" / "deploy_pages_reports.py"), "--all", "--index"])

    return 0 if parse_counts["error"] == 0 else 1


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Import and parse one Compreg tournament from a listcat URL.")
    parser.add_argument("url", help="Compreg listcat URL, for example https://compreg.ru/resultsdata/2026/09/2700/listcat.php")
    parser.add_argument("--db-path", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
    parser.add_argument("--city-protocol-sample", type=int, default=5)
    parser.add_argument("--refresh", action="store_true", help="Download listcat and protocols again, ignoring local cache.")
    parser.add_argument("--skip-parse", action="store_true", help="Only download/update protocol metadata, do not parse protocols.")
    parser.add_argument("--reparse-tournament", action="store_true", help="Parse every protocol in this tournament again.")
    parser.add_argument("--build-reports", action="store_true", help="Rebuild all club reports after import.")
    parser.add_argument("--deploy-pages", action="store_true", help="Copy generated reports and index to the GitHub Pages root.")
    parser.add_argument("--debug-city", action="store_true", help="Also print city detection debug logs.")
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    return import_listcat(args)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
