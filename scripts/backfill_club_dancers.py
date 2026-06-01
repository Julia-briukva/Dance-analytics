#!/usr/bin/env python3
"""Backfill local protocol data for dancers from a club CSV."""

from __future__ import annotations

import argparse
import csv
import re
import sqlite3
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from compreg_encoding import write_compreg_html_file
from parse_protocols import DEFAULT_DB_PATH, PROJECT_ROOT, ProtocolRecord, init_schema, parse_protocol, setup_logging


DEFAULT_CLUB_CSV = PROJECT_ROOT / "data" / "club_triumph_spb_dancers.csv"
DEFAULT_STATUS_CSV = PROJECT_ROOT / "reports" / "club_backfill_status.csv"
DEFAULT_BUILD_STATUS_CSV = PROJECT_ROOT / "reports" / "club_report_build_status.csv"
CACHE_DIR = PROJECT_ROOT / "data" / "cache"
DANCER_PROFILE_CACHE_DIR = CACHE_DIR / "dancer_profiles"
PROTOCOL_CACHE_DIR = CACHE_DIR / "protocols"
REQUEST_TIMEOUT = (8, 25)
USER_AGENT = "DanceAnalyticsClubBackfill/1.0"
STATUS_FIELDS = [
    "idd",
    "name",
    "protocols_found",
    "protocols_new",
    "protocols_already_present",
    "status",
]

PROFILE_REQUESTS = [
    ("POST", "https://compreg.ru/danceinfo.php", {"ci": "{idd}"}, "danceinfo_post_ci"),
    ("POST", "https://compreg.ru/dancer_resultsp.php", {"ci": "{idd}"}, "dancer_resultsp_post_ci"),
    ("POST", "https://compreg.ru/dancer_resultsp.php", {"ci": "{idd}", "tab": "stp", "query": ""}, "dancer_resultsp_post_ci_tab_stp"),
    ("GET", "https://compreg.ru/danceinfo.php?idd={idd}", {}, "danceinfo_get_idd"),
    ("GET", "https://compreg.ru/dancer_resultsp.php?idd={idd}", {}, "dancer_resultsp_get_idd"),
]


@dataclass(frozen=True)
class ProtocolLink:
    url: str
    tournament_id: int
    protocol_id: int
    cache_path: Path


def clean_text(value: str | None) -> str:
    return re.sub(r"\s+", " ", (value or "").replace("\xa0", " ")).strip()


def read_club_rows(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as file_obj:
        return [row for row in csv.DictReader(file_obj) if clean_text(row.get("idd"))]


def profile_cache_path(idd: str, label: str) -> Path:
    return DANCER_PROFILE_CACHE_DIR / f"{idd}_{label}.html"


def fetch_html(session: requests.Session, method: str, url: str, payload: dict[str, str] | None, cache_path: Path) -> str:
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    response = session.request(
        method,
        url,
        params=payload if method == "GET" else None,
        data=payload if method == "POST" else None,
        timeout=REQUEST_TIMEOUT,
    )
    response.raise_for_status()
    return write_compreg_html_file(cache_path, response.content, declared_encoding=response.encoding)


def fetch_dancer_pages(session: requests.Session, idd: str) -> list[tuple[str, str]]:
    pages: list[tuple[str, str]] = []
    for method, url_template, payload_template, label in PROFILE_REQUESTS:
        url = url_template.format(idd=idd)
        payload = {key: value.format(idd=idd) for key, value in payload_template.items()} if payload_template else None
        cache_path = profile_cache_path(idd, label)
        try:
            html = fetch_html(session, method, url, payload, cache_path)
        except requests.RequestException:
            if cache_path.exists():
                html = cache_path.read_text(encoding="utf-8", errors="replace")
            else:
                continue
        pages.append((html, url))
    return pages


def protocol_link_from_url(url: str) -> ProtocolLink | None:
    match = re.search(r"/resultsdata/(\d{4})/(\d{2})/(\d{4})/(\d+)\.php", url)
    if not match:
        return None
    year, month, day_suffix, protocol_id = match.groups()
    tournament_id = int(f"{year}{month}{day_suffix}")
    protocol_id_int = int(protocol_id)
    cache_path = PROTOCOL_CACHE_DIR / year / month / day_suffix / f"{protocol_id}.php"
    return ProtocolLink(url=url, tournament_id=tournament_id, protocol_id=protocol_id_int, cache_path=cache_path)


def extract_protocol_links(html: str, base_url: str) -> list[ProtocolLink]:
    soup = BeautifulSoup(html, "html.parser")
    urls: set[str] = set()
    for link in soup.find_all("a", href=True):
        href = urljoin(base_url, clean_text(link.get("href")))
        if "/resultsdata/" in href and href.endswith(".php"):
            urls.add(href)
    for match in re.finditer(r"https?://[^'\"\s<>]+/resultsdata/\d{4}/\d{2}/\d{4}/\d+\.php", html):
        urls.add(match.group(0))
    for match in re.finditer(r"(/resultsdata/\d{4}/\d{2}/\d{4}/\d+\.php)", html):
        urls.add(urljoin(base_url, match.group(1)))
    links = [item for item in (protocol_link_from_url(url) for url in sorted(urls)) if item is not None]
    return links


def ensure_tournament(conn: sqlite3.Connection, link: ProtocolLink) -> None:
    year = link.tournament_id // 1000000
    month = (link.tournament_id // 10000) % 100
    day_suffix = link.tournament_id % 10000
    day = day_suffix // 100
    suffix = day_suffix % 100
    event_date = f"{year:04d}-{month:02d}-{day:02d}"
    listcat_url = f"https://compreg.ru/resultsdata/{year:04d}/{month:02d}/{day_suffix:04d}/listcat.php"
    listcat_cache_path = f"data/cache/listcat/{year:04d}/{month:02d}/{day_suffix:04d}/listcat.php"
    conn.execute(
        """
        INSERT OR IGNORE INTO tournaments (
            id, event_date, suffix, title, location_text, listcat_url, listcat_cache_path
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (link.tournament_id, event_date, suffix, None, None, listcat_url, listcat_cache_path),
    )


def protocol_present(conn: sqlite3.Connection, link: ProtocolLink) -> int | None:
    row = conn.execute(
        "SELECT id FROM protocols WHERE protocol_url = ? OR (tournament_id = ? AND protocol_id = ?)",
        (link.url, link.tournament_id, link.protocol_id),
    ).fetchone()
    return int(row[0]) if row else None


def upsert_protocol(conn: sqlite3.Connection, link: ProtocolLink, status_code: int | None) -> int:
    ensure_tournament(conn, link)
    conn.execute(
        """
        INSERT INTO protocols (
            tournament_id, protocol_id, protocol_url, protocol_cache_path, status_code, fetched_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
        ON CONFLICT(protocol_url) DO UPDATE SET
            protocol_cache_path = excluded.protocol_cache_path,
            status_code = excluded.status_code,
            fetched_at = excluded.fetched_at,
            updated_at = CURRENT_TIMESTAMP
        """,
        (
            link.tournament_id,
            link.protocol_id,
            link.url,
            str(link.cache_path.relative_to(PROJECT_ROOT)),
            status_code,
        ),
    )
    return int(conn.execute("SELECT id FROM protocols WHERE protocol_url = ?", (link.url,)).fetchone()[0])


def fetch_protocol(session: requests.Session, link: ProtocolLink) -> int:
    response = session.get(link.url, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    write_compreg_html_file(link.cache_path, response.content, declared_encoding=response.encoding)
    return response.status_code


def safe_link_dancer(conn: sqlite3.Connection, idd: str, name: str, club: str, city: str) -> None:
    if conn.execute("SELECT 1 FROM dancers WHERE external_ref = ?", (idd,)).fetchone():
        return
    rows = conn.execute(
        """
        SELECT id
        FROM dancers
        WHERE name = ?
          AND club = ?
          AND city = ?
          AND (external_ref IS NULL OR TRIM(external_ref) = '')
        """,
        (name, club, city),
    ).fetchall()
    if len(rows) == 1:
        conn.execute("UPDATE dancers SET external_ref = ? WHERE id = ?", (idd, rows[0][0]))


def backfill_one(conn: sqlite3.Connection, session: requests.Session, row: dict[str, str]) -> tuple[dict[str, str], list[int]]:
    idd = clean_text(row.get("idd"))
    name = clean_text(row.get("name"))
    club = clean_text(row.get("club"))
    city = clean_text(row.get("city"))
    pages = fetch_dancer_pages(session, idd)
    links_by_url: dict[str, ProtocolLink] = {}
    for html, base_url in pages:
        for link in extract_protocol_links(html, base_url):
            links_by_url[link.url] = link

    new_protocol_ids: list[int] = []
    already_present = 0
    for link in links_by_url.values():
        existing = protocol_present(conn, link)
        if existing is not None:
            already_present += 1
            continue
        try:
            status_code = fetch_protocol(session, link)
        except requests.RequestException:
            continue
        db_id = upsert_protocol(conn, link, status_code)
        new_protocol_ids.append(db_id)

    status = "success" if links_by_url else "no_protocol_links"
    result = {
        "idd": idd,
        "name": name,
        "protocols_found": str(len(links_by_url)),
        "protocols_new": str(len(new_protocol_ids)),
        "protocols_already_present": str(already_present),
        "status": status,
    }
    safe_link_dancer(conn, idd, name, club, city)
    return result, new_protocol_ids


def parse_new_protocols(conn: sqlite3.Connection, protocol_db_ids: list[int]) -> None:
    if not protocol_db_ids:
        return
    setup_logging(False)
    for db_id in protocol_db_ids:
        row = conn.execute(
            "SELECT id, tournament_id, protocol_id, protocol_cache_path FROM protocols WHERE id = ?",
            (db_id,),
        ).fetchone()
        if not row or not row[3]:
            continue
        parse_protocol(conn, ProtocolRecord(int(row[0]), int(row[1]), int(row[2]), PROJECT_ROOT / row[3]))


def write_status(rows: list[dict[str, str]], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as file_obj:
        writer = csv.DictWriter(file_obj, fieldnames=STATUS_FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def count_success_reports(path: Path) -> int:
    if not path.exists():
        return 0
    with path.open(encoding="utf-8", newline="") as file_obj:
        return sum(1 for row in csv.DictReader(file_obj) if row.get("status") == "success")


def run_build_club_reports() -> int:
    completed = subprocess.run([sys.executable, str(PROJECT_ROOT / "scripts" / "build_club_reports.py")], cwd=PROJECT_ROOT)
    return completed.returncode


def backfill(club_csv: Path, db_path: Path, status_csv: Path, run_reports: bool) -> tuple[list[dict[str, str]], int, int]:
    rows = read_club_rows(club_csv)
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})
    statuses: list[dict[str, str]] = []
    all_new_protocol_ids: list[int] = []
    with sqlite3.connect(db_path) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        init_schema(conn)
        for row in rows:
            try:
                status, new_ids = backfill_one(conn, session, row)
            except Exception as exc:  # noqa: BLE001 - keep processing batch
                status = {
                    "idd": clean_text(row.get("idd")),
                    "name": clean_text(row.get("name")),
                    "protocols_found": "0",
                    "protocols_new": "0",
                    "protocols_already_present": "0",
                    "status": f"error: {exc}",
                }
                new_ids = []
            statuses.append(status)
            all_new_protocol_ids.extend(new_ids)
            conn.commit()
        parse_new_protocols(conn, all_new_protocol_ids)
        for row in rows:
            safe_link_dancer(conn, clean_text(row.get("idd")), clean_text(row.get("name")), clean_text(row.get("club")), clean_text(row.get("city")))
        conn.commit()

    write_status(statuses, status_csv)
    before_reports = count_success_reports(DEFAULT_BUILD_STATUS_CSV)
    if run_reports:
        run_build_club_reports()
    after_reports = count_success_reports(DEFAULT_BUILD_STATUS_CSV)
    return statuses, len(set(all_new_protocol_ids)), max(0, after_reports - before_reports)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Backfill local protocol data for club dancers.")
    parser.add_argument("--club-csv", type=Path, default=DEFAULT_CLUB_CSV)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--status-csv", type=Path, default=PROJECT_ROOT / "reports" / "club_backfill_status.csv")
    parser.add_argument("--skip-build-reports", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    started = datetime.now(timezone.utc).isoformat()
    statuses, new_protocols, new_reports = backfill(args.club_csv, args.db, args.status_csv, not args.skip_build_reports)
    dancers_with_new = sum(int(row.get("protocols_new") or 0) > 0 for row in statuses)
    print(f"Started: {started}")
    print(f"Rows processed: {len(statuses)}")
    print(f"New protocols found: {new_protocols}")
    print(f"Dancers with first/new data: {dancers_with_new}")
    print(f"New reports appeared: {new_reports}")
    if DEFAULT_BUILD_STATUS_CSV.exists():
        reports_total = count_success_reports(DEFAULT_BUILD_STATUS_CSV)
        print(f"Club report coverage: {reports_total} of {len(statuses)} dancers have reports")
    print(f"Backfill status CSV: {args.status_csv.relative_to(PROJECT_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
