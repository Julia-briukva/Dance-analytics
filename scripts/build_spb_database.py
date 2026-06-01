#!/usr/bin/env python3
"""Build a local SQLite data layer for Compreg Saint Petersburg tournaments."""

from __future__ import annotations

import argparse
import hashlib
import logging
import re
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterable

import pandas as pd
import requests
from bs4 import BeautifulSoup


BASE_URL = "https://compreg.ru"
DEFAULT_START_DATE = "2025-09-01"
DEFAULT_END_DATE = "2026-08-31"
DEFAULT_MIN_SUFFIX = 0
DEFAULT_MAX_SUFFIX = 50
DEFAULT_WORKERS = 8
REQUEST_TIMEOUT = (4, 8)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = PROJECT_ROOT / "data"
CACHE_DIR = DATA_DIR / "cache"
LISTCAT_CACHE_DIR = CACHE_DIR / "listcat"
PROTOCOL_CACHE_DIR = CACHE_DIR / "protocols"
MISSING_CACHE_DIR = CACHE_DIR / "missing"
DATABASE_DIR = PROJECT_ROOT / "database"
DEFAULT_DB_PATH = DATABASE_DIR / "compreg_spb_2025_2026.sqlite"
DEBUG_LOG_PATH = PROJECT_ROOT / "reports" / "city_detection_debug.log"

LOAD_CATEGORY_RE = re.compile(r"LoadCategoryRes\(\s*(\d+)\s*,\s*(\d+)\s*\)", re.I)
PARENTHESIS_RE = re.compile(r"\(([^()]{2,120})\)")
CITY_ALIASES = {
    "spb": [
        "санкт-петербург",
        "санкт петербург",
        "с.-петербург",
        "с петербург",
        "спб",
        "spb",
        "saint petersburg",
        "st. petersburg",
        "st petersburg",
        "sankt petersburg",
        "ленинградская область",
        "ленобласть",
        "leningrad oblast",
    ],
}
CITY_LABELS = {
    "spb": "Санкт-Петербург",
}


def normalize_city_name(value: str | None) -> str:
    """Normalize city aliases so future city dictionaries can reuse one matcher."""
    if not value:
        return ""
    value = value.lower().replace("ё", "е")
    value = re.sub(r"[^0-9a-zа-я]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


NORMALIZED_CITY_ALIASES = {
    city_key: {alias for alias in (normalize_city_name(item) for item in aliases) if alias}
    for city_key, aliases in CITY_ALIASES.items()
}


@dataclass(frozen=True)
class FetchResult:
    url: str
    local_path: Path | None
    status_code: int | None
    from_cache: bool
    error: str | None = None


@dataclass(frozen=True)
class Candidate:
    event_date: date
    suffix: int
    url: str
    relative_key: str


@dataclass(frozen=True)
class TournamentPage:
    event_date: date
    suffix: int
    listcat_url: str
    local_path: Path
    tournament_id: int | None
    title: str | None
    location_text: str | None
    raw_detected_city: str | None
    normalized_city: str | None
    city_detection_source: str | None
    protocol_ids: tuple[int, ...]


@dataclass(frozen=True)
class RejectedTournamentPage:
    event_date: date
    suffix: int
    listcat_url: str
    local_path: Path
    tournament_id: int | None
    title: str | None
    raw_detected_city: str | None
    normalized_city: str | None
    city_detection_source: str | None
    reject_reason: str
    protocol_ids: tuple[int, ...]


@dataclass(frozen=True)
class CityDetection:
    matched_city_key: str | None
    raw_detected_city: str | None
    normalized_city: str | None
    source: str | None
    reason: str


@dataclass(frozen=True)
class ListcatCheckRecord:
    candidate: Candidate
    exists: bool
    from_cache: bool
    status_code: int | None
    city_detected: str | None


@dataclass(frozen=True)
class CrawlStats:
    candidates: int
    fetched: int
    cached: int
    skipped_checked_missing: int
    listcats_found: int
    spb_found: int
    rejected: int


def ensure_directories() -> None:
    for path in (
        DATA_DIR,
        LISTCAT_CACHE_DIR,
        PROTOCOL_CACHE_DIR,
        MISSING_CACHE_DIR,
        DATABASE_DIR,
        PROJECT_ROOT / "reports",
    ):
        path.mkdir(parents=True, exist_ok=True)


def setup_logging(debug_city: bool) -> None:
    ensure_directories()
    handlers: list[logging.Handler] = [logging.FileHandler(DEBUG_LOG_PATH, mode="w", encoding="utf-8")]
    if debug_city:
        handlers.append(logging.StreamHandler(sys.stdout))
    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=handlers,
        force=True,
    )


def parse_date(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


def iter_dates(start: date, end: date) -> Iterable[date]:
    current = start
    while current <= end:
        yield current
        current += timedelta(days=1)


def build_candidates(start: date, end: date, min_suffix: int, max_suffix: int) -> list[Candidate]:
    candidates: list[Candidate] = []
    for event_date in iter_dates(start, end):
        year = event_date.strftime("%Y")
        month = event_date.strftime("%m")
        day = event_date.strftime("%d")
        for suffix in range(min_suffix, max_suffix + 1):
            day_suffix = f"{day}{suffix:02d}"
            relative_key = f"{year}/{month}/{day_suffix}/listcat.php"
            url = f"{BASE_URL}/resultsdata/{relative_key}"
            candidates.append(Candidate(event_date, suffix, url, relative_key))
    return candidates


def listcat_cache_path(candidate: Candidate) -> Path:
    year, month, day_suffix, _ = candidate.relative_key.split("/")
    return LISTCAT_CACHE_DIR / year / month / day_suffix / "listcat.php"


def protocol_cache_path(tournament_id: int, protocol_id: int) -> Path:
    tid = str(tournament_id)
    year = tid[:4]
    month = tid[4:6]
    day_suffix = tid[6:]
    return PROTOCOL_CACHE_DIR / year / month / day_suffix / f"{protocol_id}.php"


def missing_marker_path(url: str) -> Path:
    digest = hashlib.sha1(url.encode("utf-8")).hexdigest()
    return MISSING_CACHE_DIR / f"{digest}.missing"


def fetch_html(
    session: requests.Session,
    url: str,
    cache_path: Path,
    refresh: bool = False,
) -> FetchResult:
    marker_path = missing_marker_path(url)
    if cache_path.exists() and not refresh:
        return FetchResult(url=url, local_path=cache_path, status_code=200, from_cache=True)
    if marker_path.exists() and not refresh:
        return FetchResult(url=url, local_path=None, status_code=404, from_cache=True)

    try:
        response = session.get(url, timeout=REQUEST_TIMEOUT)
    except requests.RequestException as exc:
        return FetchResult(url=url, local_path=None, status_code=None, from_cache=False, error=str(exc))

    if response.status_code == 200 and response.text.strip():
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(response.text, encoding=response.encoding or "utf-8", errors="replace")
        if marker_path.exists():
            marker_path.unlink()
        return FetchResult(url=url, local_path=cache_path, status_code=response.status_code, from_cache=False)

    if response.status_code != 200:
        marker_path.parent.mkdir(parents=True, exist_ok=True)
        marker_path.write_text(f"{response.status_code} {url}\n", encoding="utf-8")

    return FetchResult(url=url, local_path=None, status_code=response.status_code, from_cache=False)


def make_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/125.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }
    )
    return session


def normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def alias_matches_city(normalized_text: str, city_key: str) -> bool:
    padded_text = f" {normalized_text} "
    return any(f" {alias} " in padded_text for alias in NORMALIZED_CITY_ALIASES[city_key])


def extract_city_candidates(text: str | None) -> list[str]:
    if not text:
        return []
    cleaned = normalize_text(text)
    candidates = [match.strip() for match in PARENTHESIS_RE.findall(cleaned) if match.strip()]
    candidates.append(cleaned)
    return list(dict.fromkeys(candidates))


def detect_city_from_sources(sources: list[tuple[str, str | None]]) -> CityDetection:
    first_raw_candidate: tuple[str, str] | None = None
    for source_name, text in sources:
        for raw_candidate in extract_city_candidates(text):
            normalized = normalize_city_name(raw_candidate)
            if not normalized:
                continue
            if first_raw_candidate is None:
                first_raw_candidate = (source_name, raw_candidate)
            for city_key in NORMALIZED_CITY_ALIASES:
                if alias_matches_city(normalized, city_key):
                    return CityDetection(
                        matched_city_key=city_key,
                        raw_detected_city=raw_candidate,
                        normalized_city=CITY_LABELS[city_key],
                        source=source_name,
                        reason="matched_alias",
                    )

    if first_raw_candidate:
        source_name, raw_candidate = first_raw_candidate
        return CityDetection(
            matched_city_key=None,
            raw_detected_city=raw_candidate,
            normalized_city=None,
            source=source_name,
            reason="no_alias_match",
        )

    return CityDetection(
        matched_city_key=None,
        raw_detected_city=None,
        normalized_city=None,
        source=None,
        reason="no_city_candidate",
    )


def extract_title_and_city_sources(soup: BeautifulSoup) -> tuple[str | None, list[tuple[str, str]]]:
    title_parts: list[str] = []
    if soup.title and soup.title.get_text(strip=True):
        title_parts.append(soup.title.get_text(" ", strip=True))

    city_sources: list[tuple[str, str]] = []
    for selector in ("h1", "h2", ".title", ".zag", "caption", ".prot-caption", ".prot-header-text"):
        for node in soup.select(selector):
            text = normalize_text(node.get_text(" ", strip=True))
            if text and text not in title_parts:
                title_parts.append(text)
            if text:
                city_sources.append((f"selector:{selector}", text))

    title = " | ".join(title_parts[:3]) if title_parts else None
    if title:
        city_sources.insert(0, ("title", title))
    city_sources.append(("html_text", normalize_text(soup.get_text(" ", strip=True))))
    return title, city_sources


def extract_protocol_city_sources(local_path: Path) -> list[tuple[str, str]]:
    html = local_path.read_text(encoding="utf-8", errors="replace")
    soup = BeautifulSoup(html, "html.parser")
    _, sources = extract_title_and_city_sources(soup)
    metadata_sources = [(source, text) for source, text in sources if source != "html_text"]
    return [(f"protocol:{local_path.name}:{source}", text) for source, text in metadata_sources]


def parse_tournament_page(
    candidate: Candidate,
    local_path: Path,
    city_protocol_sample: int,
    refresh: bool,
) -> TournamentPage | RejectedTournamentPage | None:
    html = local_path.read_text(encoding="utf-8", errors="replace")
    soup = BeautifulSoup(html, "html.parser")

    matches = LOAD_CATEGORY_RE.findall(html)
    if not matches:
        return None

    tournament_ids = [int(tid) for tid, _ in matches]
    tournament_id = tournament_ids[0] if tournament_ids else None
    protocol_ids = tuple(dict.fromkeys(int(pid) for _, pid in matches))
    title, city_sources = extract_title_and_city_sources(soup)
    city_detection = detect_city_from_sources(city_sources)

    if city_detection.matched_city_key is None and tournament_id and city_protocol_sample > 0:
        session = make_session()
        for protocol_id in protocol_ids[:city_protocol_sample]:
            url = protocol_url(tournament_id, protocol_id)
            result = fetch_html(session, url, protocol_cache_path(tournament_id, protocol_id), refresh=refresh)
            if result.local_path:
                city_sources.extend(extract_protocol_city_sources(result.local_path))
        city_detection = detect_city_from_sources(city_sources)

    location = city_detection.raw_detected_city

    if city_detection.matched_city_key != "spb":
        reason = "city_alias_not_matched" if city_detection.raw_detected_city else "city_not_detected"
        logging.debug(
            "REJECTED tournament_id=%s url=%s title=%r raw_city=%r source=%r reason=%s",
            tournament_id,
            candidate.url,
            title,
            city_detection.raw_detected_city,
            city_detection.source,
            reason,
        )
        return RejectedTournamentPage(
            event_date=candidate.event_date,
            suffix=candidate.suffix,
            listcat_url=candidate.url,
            local_path=local_path,
            tournament_id=tournament_id,
            title=title,
            raw_detected_city=city_detection.raw_detected_city,
            normalized_city=city_detection.normalized_city,
            city_detection_source=city_detection.source,
            reject_reason=reason,
            protocol_ids=protocol_ids,
        )

    logging.debug(
        "ACCEPTED tournament_id=%s url=%s title=%r raw_city=%r source=%r",
        tournament_id,
        candidate.url,
        title,
        city_detection.raw_detected_city,
        city_detection.source,
    )

    return TournamentPage(
        event_date=candidate.event_date,
        suffix=candidate.suffix,
        listcat_url=candidate.url,
        local_path=local_path,
        tournament_id=tournament_id,
        title=title,
        location_text=location,
        raw_detected_city=city_detection.raw_detected_city,
        normalized_city=city_detection.normalized_city,
        city_detection_source=city_detection.source,
        protocol_ids=protocol_ids,
    )


def protocol_url(tournament_id: int, protocol_id: int) -> str:
    tid = str(tournament_id)
    return f"{BASE_URL}/resultsdata/{tid[:4]}/{tid[4:6]}/{tid[6:]}/{protocol_id}.php"


def init_db(db_path: Path) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            PRAGMA foreign_keys = ON;

            CREATE TABLE IF NOT EXISTS tournaments (
                id INTEGER PRIMARY KEY,
                event_date TEXT NOT NULL,
                suffix INTEGER,
                title TEXT,
                location_text TEXT,
                raw_detected_city TEXT,
                normalized_city TEXT,
                city_detection_source TEXT,
                listcat_url TEXT NOT NULL UNIQUE,
                listcat_cache_path TEXT NOT NULL,
                source TEXT NOT NULL DEFAULT 'compreg',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS protocols (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tournament_id INTEGER NOT NULL,
                protocol_id INTEGER NOT NULL,
                protocol_url TEXT NOT NULL UNIQUE,
                protocol_cache_path TEXT,
                status_code INTEGER,
                fetched_at TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (tournament_id) REFERENCES tournaments(id)
            );

            CREATE UNIQUE INDEX IF NOT EXISTS idx_protocols_tournament_protocol
                ON protocols(tournament_id, protocol_id);

            CREATE TABLE IF NOT EXISTS checked_listcats (
                url TEXT PRIMARY KEY,
                "exists" INTEGER NOT NULL,
                checked_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                city_detected TEXT,
                status_code INTEGER,
                cache_path TEXT,
                from_cache INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS rejected_tournaments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tournament_id INTEGER,
                event_date TEXT NOT NULL,
                suffix INTEGER,
                title TEXT,
                raw_detected_city TEXT,
                normalized_city TEXT,
                city_detection_source TEXT,
                reject_reason TEXT NOT NULL,
                listcat_url TEXT NOT NULL UNIQUE,
                listcat_cache_path TEXT NOT NULL,
                protocol_count INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS dancers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                club TEXT,
                city TEXT,
                external_ref TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(name, club, city)
            );

            CREATE TABLE IF NOT EXISTS judges (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                city TEXT,
                external_ref TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(name, city)
            );

            CREATE TABLE IF NOT EXISTS marks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                protocol_row_id INTEGER,
                protocol_id INTEGER NOT NULL,
                dancer_id INTEGER,
                judge_id INTEGER,
                round_name TEXT,
                dance_name TEXT,
                mark_value TEXT,
                place_value REAL,
                raw_text TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (protocol_id) REFERENCES protocols(id),
                FOREIGN KEY (dancer_id) REFERENCES dancers(id),
                FOREIGN KEY (judge_id) REFERENCES judges(id)
            );
            """
        )
        ensure_column(conn, "tournaments", "raw_detected_city", "TEXT")
        ensure_column(conn, "tournaments", "normalized_city", "TEXT")
        ensure_column(conn, "tournaments", "city_detection_source", "TEXT")


def load_checked_listcat_urls(db_path: Path) -> set[str]:
    if not db_path.exists():
        return set()
    with sqlite3.connect(db_path) as conn:
        table_exists = conn.execute(
            """
            SELECT 1
            FROM sqlite_master
            WHERE type = 'table' AND name = 'checked_listcats';
            """
        ).fetchone()
        if not table_exists:
            return set()
        rows = conn.execute("SELECT url FROM checked_listcats").fetchall()
    return {row[0] for row in rows}


def upsert_checked_listcat(conn: sqlite3.Connection, record: ListcatCheckRecord) -> None:
    cache_path = listcat_cache_path(record.candidate)
    conn.execute(
        """
        INSERT INTO checked_listcats (
            url, "exists", city_detected, status_code, cache_path, from_cache, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(url) DO UPDATE SET
            "exists" = excluded."exists",
            city_detected = excluded.city_detected,
            status_code = excluded.status_code,
            cache_path = excluded.cache_path,
            from_cache = excluded.from_cache,
            updated_at = CURRENT_TIMESTAMP;
        """,
        (
            record.candidate.url,
            1 if record.exists else 0,
            record.city_detected,
            record.status_code,
            str(cache_path.relative_to(PROJECT_ROOT)) if record.exists else None,
            1 if record.from_cache else 0,
        ),
    )


def ensure_column(conn: sqlite3.Connection, table_name: str, column_name: str, column_type: str) -> None:
    columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table_name})")}
    if column_name not in columns:
        conn.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_type}")


def upsert_tournament(conn: sqlite3.Connection, page: TournamentPage) -> None:
    if page.tournament_id is None:
        return
    conn.execute(
        """
        INSERT INTO tournaments (
            id, event_date, suffix, title, location_text, raw_detected_city, normalized_city,
            city_detection_source, listcat_url, listcat_cache_path, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(id) DO UPDATE SET
            event_date = excluded.event_date,
            suffix = excluded.suffix,
            title = excluded.title,
            location_text = excluded.location_text,
            raw_detected_city = excluded.raw_detected_city,
            normalized_city = excluded.normalized_city,
            city_detection_source = excluded.city_detection_source,
            listcat_url = excluded.listcat_url,
            listcat_cache_path = excluded.listcat_cache_path,
            updated_at = CURRENT_TIMESTAMP;
        """,
        (
            page.tournament_id,
            page.event_date.isoformat(),
            page.suffix,
            page.title,
            page.location_text,
            page.raw_detected_city,
            page.normalized_city,
            page.city_detection_source,
            page.listcat_url,
            str(page.local_path.relative_to(PROJECT_ROOT)),
        ),
    )


def upsert_rejected_tournament(conn: sqlite3.Connection, page: RejectedTournamentPage) -> None:
    conn.execute(
        """
        INSERT INTO rejected_tournaments (
            tournament_id, event_date, suffix, title, raw_detected_city, normalized_city,
            city_detection_source, reject_reason, listcat_url, listcat_cache_path, protocol_count, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(listcat_url) DO UPDATE SET
            tournament_id = excluded.tournament_id,
            event_date = excluded.event_date,
            suffix = excluded.suffix,
            title = excluded.title,
            raw_detected_city = excluded.raw_detected_city,
            normalized_city = excluded.normalized_city,
            city_detection_source = excluded.city_detection_source,
            reject_reason = excluded.reject_reason,
            listcat_cache_path = excluded.listcat_cache_path,
            protocol_count = excluded.protocol_count,
            updated_at = CURRENT_TIMESTAMP;
        """,
        (
            page.tournament_id,
            page.event_date.isoformat(),
            page.suffix,
            page.title,
            page.raw_detected_city,
            page.normalized_city,
            page.city_detection_source,
            page.reject_reason,
            page.listcat_url,
            str(page.local_path.relative_to(PROJECT_ROOT)),
            len(page.protocol_ids),
        ),
    )


def upsert_protocol(
    conn: sqlite3.Connection,
    tournament_id: int,
    protocol_id: int,
    url: str,
    cache_path: Path | None,
    status_code: int | None,
) -> None:
    conn.execute(
        """
        INSERT INTO protocols (
            tournament_id, protocol_id, protocol_url, protocol_cache_path, status_code, fetched_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
        ON CONFLICT(protocol_url) DO UPDATE SET
            protocol_cache_path = excluded.protocol_cache_path,
            status_code = excluded.status_code,
            fetched_at = excluded.fetched_at,
            updated_at = CURRENT_TIMESTAMP;
        """,
        (
            tournament_id,
            protocol_id,
            url,
            str(cache_path.relative_to(PROJECT_ROOT)) if cache_path else None,
            status_code,
        ),
    )


def fetch_candidate(candidate: Candidate, refresh: bool, checked_urls: set[str]) -> tuple[Candidate, FetchResult]:
    if not refresh and candidate.url in checked_urls:
        cache_path = listcat_cache_path(candidate)
        if cache_path.exists():
            return candidate, FetchResult(candidate.url, cache_path, 200, True)
        return candidate, FetchResult(candidate.url, None, 404, True)

    session = make_session()
    result = fetch_html(session, candidate.url, listcat_cache_path(candidate), refresh=refresh)
    return candidate, result


def fetch_protocol(task: tuple[int, int, bool]) -> tuple[int, int, FetchResult]:
    tournament_id, protocol_id, refresh = task
    session = make_session()
    url = protocol_url(tournament_id, protocol_id)
    result = fetch_html(session, url, protocol_cache_path(tournament_id, protocol_id), refresh=refresh)
    return tournament_id, protocol_id, result


def load_listcat_pages(
    candidates: list[Candidate],
    workers: int,
    refresh: bool,
    city_protocol_sample: int,
    checked_urls: set[str],
) -> tuple[list[TournamentPage], list[RejectedTournamentPage], list[ListcatCheckRecord], CrawlStats]:
    pages: list[TournamentPage] = []
    rejected_pages: list[RejectedTournamentPage] = []
    check_records: list[ListcatCheckRecord] = []
    checked = 0
    found_existing = 0
    fetched = 0
    cached = 0
    skipped_checked_missing = 0

    with ThreadPoolExecutor(max_workers=workers) as executor:
        future_map = {executor.submit(fetch_candidate, candidate, refresh, checked_urls): candidate for candidate in candidates}
        for future in as_completed(future_map):
            candidate, result = future.result()
            checked += 1
            if result.from_cache:
                cached += 1
                if not result.local_path and candidate.url in checked_urls:
                    skipped_checked_missing += 1
            else:
                fetched += 1

            city_detected = None
            if result.local_path:
                found_existing += 1
                page = parse_tournament_page(candidate, result.local_path, city_protocol_sample, refresh)
                if isinstance(page, TournamentPage):
                    pages.append(page)
                    city_detected = page.raw_detected_city
                elif isinstance(page, RejectedTournamentPage):
                    rejected_pages.append(page)
                    city_detected = page.raw_detected_city

            check_records.append(
                ListcatCheckRecord(
                    candidate=candidate,
                    exists=result.local_path is not None,
                    from_cache=result.from_cache,
                    status_code=result.status_code,
                    city_detected=city_detected,
                )
            )
            if checked % 500 == 0:
                print(
                    f"Checked listcat pages: {checked}/{len(candidates)}; "
                    f"found: {found_existing}; SPb: {len(pages)}; rejected: {len(rejected_pages)}; "
                    f"cached/skipped: {cached}",
                    flush=True,
                )

    print(
        f"Checked listcat pages: {checked}/{len(candidates)}; listcats found: {found_existing}; "
        f"SPb pages: {len(pages)}; rejected: {len(rejected_pages)}; cached/skipped: {cached}; fetched: {fetched}",
        flush=True,
    )
    stats = CrawlStats(
        candidates=len(candidates),
        fetched=fetched,
        cached=cached,
        skipped_checked_missing=skipped_checked_missing,
        listcats_found=found_existing,
        spb_found=len(pages),
        rejected=len(rejected_pages),
    )
    return (
        sorted(pages, key=lambda page: (page.event_date, page.suffix)),
        sorted(rejected_pages, key=lambda page: (page.event_date, page.suffix)),
        check_records,
        stats,
    )


def load_protocol_pages(pages: list[TournamentPage], workers: int, refresh: bool) -> list[tuple[int, int, FetchResult]]:
    tasks = []
    for page in pages:
        if page.tournament_id is None:
            continue
        for protocol_id in page.protocol_ids:
            tasks.append((page.tournament_id, protocol_id, refresh))

    results: list[tuple[int, int, FetchResult]] = []
    with ThreadPoolExecutor(max_workers=workers) as executor:
        future_map = {executor.submit(fetch_protocol, task): task for task in tasks}
        for index, future in enumerate(as_completed(future_map), start=1):
            results.append(future.result())
            if index % 200 == 0:
                print(f"Fetched protocol pages: {index}/{len(tasks)}", flush=True)

    print(f"Fetched protocol pages: {len(results)}/{len(tasks)}", flush=True)
    return results


def save_metadata(
    db_path: Path,
    pages: list[TournamentPage],
    rejected_pages: list[RejectedTournamentPage],
    check_records: list[ListcatCheckRecord],
    protocol_results: list[tuple[int, int, FetchResult]],
    prune_existing: bool = True,
) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        if prune_existing:
            conn.execute("DELETE FROM protocols")
            conn.execute("DELETE FROM tournaments")
            conn.execute("DELETE FROM rejected_tournaments")
        for record in check_records:
            upsert_checked_listcat(conn, record)
        for page in pages:
            upsert_tournament(conn, page)
        for page in rejected_pages:
            upsert_rejected_tournament(conn, page)
        for tournament_id, protocol_id, result in protocol_results:
            upsert_protocol(
                conn,
                tournament_id=tournament_id,
                protocol_id=protocol_id,
                url=result.url,
                cache_path=result.local_path,
                status_code=result.status_code,
            )
        conn.commit()


def month_sequence(start: date, end: date) -> list[str]:
    months = []
    current = date(start.year, start.month, 1)
    end_month = date(end.year, end.month, 1)
    while current <= end_month:
        months.append(current.strftime("%Y-%m"))
        if current.month == 12:
            current = date(current.year + 1, 1, 1)
        else:
            current = date(current.year, current.month + 1, 1)
    return months


def print_coverage(db_path: Path, start: date, end: date) -> None:
    with sqlite3.connect(db_path) as conn:
        coverage_rows = conn.execute(
            """
            SELECT substr(replace(replace(url, 'https://compreg.ru/resultsdata/', ''), '/listcat.php', ''), 1, 7) AS ym,
                   COUNT(*) AS checked,
                   SUM("exists") AS listcats_found,
                   SUM(CASE WHEN city_detected IS NOT NULL THEN 1 ELSE 0 END) AS city_detected_count
            FROM checked_listcats
            GROUP BY ym
            ORDER BY ym;
            """
        ).fetchall()
        spb_rows = conn.execute(
            """
            SELECT substr(event_date, 1, 7) AS ym, COUNT(*)
            FROM tournaments
            GROUP BY ym;
            """
        ).fetchall()

    coverage = {row[0].replace("/", "-"): row[1:] for row in coverage_rows if row[0]}
    spb_by_month = {row[0]: row[1] for row in spb_rows}
    missing_months = []
    print("\nCoverage by month:", flush=True)
    for ym in month_sequence(start, end):
        checked, listcats_found, city_detected_count = coverage.get(ym, (0, 0, 0))
        spb_count = spb_by_month.get(ym, 0)
        if checked == 0:
            missing_months.append(ym)
        print(
            f"{ym}: checked={checked}, listcats_found={listcats_found or 0}, "
            f"city_detected={city_detected_count or 0}, spb={spb_count}",
            flush=True,
        )

    print("\nMissing months:", flush=True)
    if missing_months:
        print(", ".join(missing_months), flush=True)
    else:
        print("No missing months in checked_listcats coverage.", flush=True)


def latest_tournament_date(db_path: Path) -> date | None:
    if not db_path.exists():
        return None
    with sqlite3.connect(db_path) as conn:
        table_exists = conn.execute(
            """
            SELECT 1
            FROM sqlite_master
            WHERE type = 'table' AND name = 'tournaments';
            """
        ).fetchone()
        if not table_exists:
            return None
        value = conn.execute("SELECT MAX(event_date) FROM tournaments").fetchone()[0]
    return parse_date(value) if value else None


def print_database_summary(db_path: Path) -> None:
    with sqlite3.connect(db_path) as conn:
        tables = pd.read_sql_query(
            """
            SELECT name
            FROM sqlite_master
            WHERE type = 'table'
            ORDER BY name;
            """,
            conn,
        )
        counts = []
        for table_name in tables["name"].tolist():
            count = conn.execute(f"SELECT COUNT(*) FROM {table_name}").fetchone()[0]
            counts.append({"table": table_name, "rows": count})
        counts_df = pd.DataFrame(counts)

        sample = pd.read_sql_query(
            """
            SELECT id, event_date, title, raw_detected_city, normalized_city, city_detection_source, listcat_url
            FROM tournaments
            ORDER BY event_date, id
            LIMIT 10;
            """,
            conn,
        )
        rejected_sample = pd.read_sql_query(
            """
            SELECT tournament_id, event_date, title, raw_detected_city, city_detection_source, reject_reason, listcat_url
            FROM rejected_tournaments
            ORDER BY event_date, tournament_id
            LIMIT 20;
            """,
            conn,
        )
        detected_cities = pd.read_sql_query(
            """
            SELECT raw_detected_city, normalized_city, COUNT(*) AS tournaments
            FROM tournaments
            GROUP BY raw_detected_city, normalized_city
            UNION ALL
            SELECT raw_detected_city, normalized_city, COUNT(*) AS tournaments
            FROM rejected_tournaments
            GROUP BY raw_detected_city, normalized_city
            ORDER BY tournaments DESC, raw_detected_city;
            """,
            conn,
        )

    print("\nDatabase tables:", flush=True)
    print(counts_df.to_string(index=False), flush=True)
    print("\nSample tournaments:", flush=True)
    if sample.empty:
        print("No tournaments found yet.", flush=True)
    else:
        print(sample.to_string(index=False), flush=True)

    print("\nRejected tournaments:", flush=True)
    if rejected_sample.empty:
        print("No rejected tournaments.", flush=True)
    else:
        print(rejected_sample.to_string(index=False), flush=True)

    print("\nDetected city values:", flush=True)
    if detected_cities.empty:
        print("No city values detected.", flush=True)
    else:
        print(detected_cities.to_string(index=False), flush=True)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build local SQLite database from Compreg results.")
    parser.add_argument("--start-date", default=DEFAULT_START_DATE, help="Season start date, YYYY-MM-DD.")
    parser.add_argument("--end-date", default=DEFAULT_END_DATE, help="Season end date, YYYY-MM-DD.")
    parser.add_argument("--min-suffix", type=int, default=DEFAULT_MIN_SUFFIX, help="Min DDXX daily suffix to probe.")
    parser.add_argument("--max-suffix", type=int, default=DEFAULT_MAX_SUFFIX, help="Max DDXX daily suffix to probe.")
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS, help="ThreadPoolExecutor workers.")
    parser.add_argument("--db-path", type=Path, default=DEFAULT_DB_PATH, help="SQLite database path.")
    parser.add_argument("--refresh", action="store_true", help="Ignore local cache and download again.")
    parser.add_argument("--incremental", action="store_true", help="Only check dates after the latest tournament date in DB.")
    parser.add_argument(
        "--city-protocol-sample",
        type=int,
        default=3,
        help="How many protocol pages to inspect when listcat city aliases are not enough.",
    )
    parser.add_argument("--debug-city", action="store_true", help="Also print city detection debug logs to stdout.")
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    start_time = time.time()
    start_date = parse_date(args.start_date)
    end_date = parse_date(args.end_date)
    if args.incremental:
        latest_date = latest_tournament_date(args.db_path)
        if latest_date:
            start_date = max(start_date, latest_date + timedelta(days=1))
    if end_date < start_date:
        print("No new dates to check.", flush=True)
        return 0

    ensure_directories()
    setup_logging(args.debug_city)
    init_db(args.db_path)

    checked_urls = load_checked_listcat_urls(args.db_path)
    candidates = build_candidates(start_date, end_date, args.min_suffix, args.max_suffix)
    print(f"Built candidates: {len(candidates)}", flush=True)

    pages, rejected_pages, check_records, stats = load_listcat_pages(
        candidates,
        workers=args.workers,
        refresh=args.refresh,
        city_protocol_sample=args.city_protocol_sample,
        checked_urls=checked_urls,
    )
    protocol_results = load_protocol_pages(pages, workers=args.workers, refresh=args.refresh)
    save_metadata(
        args.db_path,
        pages,
        rejected_pages,
        check_records,
        protocol_results,
        prune_existing=not args.incremental,
    )
    print_database_summary(args.db_path)
    print(
        "\nCrawler stats:\n"
        f"candidates={stats.candidates}\n"
        f"listcats_found={stats.listcats_found}\n"
        f"rejected={stats.rejected}\n"
        f"cached_or_skipped={stats.cached}\n"
        f"skipped_checked_missing={stats.skipped_checked_missing}\n"
        f"http_fetched={stats.fetched}",
        flush=True,
    )
    print_coverage(args.db_path, start_date, end_date)
    print(f"\nDone in {time.time() - start_time:.1f}s", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
