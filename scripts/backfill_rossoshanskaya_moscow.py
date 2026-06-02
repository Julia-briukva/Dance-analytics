#!/usr/bin/env python3
"""Retry Moscow protocol search for Rossoshanskaya Darya from saved candidates."""

from __future__ import annotations

import csv
import json
import re
import sqlite3
import subprocess
import sys
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

import requests
from bs4 import BeautifulSoup

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parents[0]
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from backfill_club_dancers import ProtocolLink, parse_new_protocols, protocol_present, upsert_protocol  # noqa: E402
from compreg_encoding import read_compreg_html_file, write_compreg_html_file  # noqa: E402


DEFAULT_CSV = PROJECT_ROOT / "reports" / "rossoshanskaya_moscow_backfill.csv"
DEFAULT_LOG = PROJECT_ROOT / "reports" / "rossoshanskaya_moscow_search.log"
DEFAULT_DB = PROJECT_ROOT / "database" / "compreg_spb_2025_2026.sqlite"
REPORT_JSON = PROJECT_ROOT / "reports" / "dancer_2020091_report.json"
TARGET_IDD = "2020091"
TARGET_DANCER_ID = 26512
TARGET_TERMS = [
    "Россошанская Дарья",
    "Росошанская Дарья",
    "Россошанская",
    "Росошанская",
    "Darya",
    "Daria",
]
USER_AGENT = "DanceAnalyticsRossoshanskayaMoscowBackfill/1.0"


def clean_text(value: str | None) -> str:
    return re.sub(r"\s+", " ", (value or "").replace("\xa0", " ")).strip()


def protocol_cache_path(url: str) -> Path:
    match = re.search(r"/resultsdata/(\d{4})/(\d{2})/(\d{4})/(\d+)\.php", url)
    if not match:
        raise ValueError(f"Unsupported protocol URL: {url}")
    year, month, day_suffix, protocol_id = match.groups()
    return PROJECT_ROOT / "data" / "cache" / "protocols" / year / month / day_suffix / f"{protocol_id}.php"


def protocol_link(row: dict[str, str]) -> ProtocolLink:
    url = row["protocol_url"]
    match = re.search(r"/resultsdata/(\d{4})/(\d{2})/(\d{4})/(\d+)\.php", url)
    if not match:
        raise ValueError(f"Unsupported protocol URL: {url}")
    year, month, day_suffix, protocol_id = match.groups()
    return ProtocolLink(
        url=url,
        tournament_id=int(f"{year}{month}{day_suffix}"),
        protocol_id=int(protocol_id),
        cache_path=protocol_cache_path(url),
    )


def matched_terms(html: str) -> list[str]:
    return [term for term in TARGET_TERMS if re.search(re.escape(term), html, flags=re.IGNORECASE)]


def extract_context(html: str, term: str) -> str:
    match = re.search(re.escape(term), html, flags=re.IGNORECASE)
    if not match:
        return ""
    snippet = html[max(0, match.start() - 500) : match.end() + 900]
    return clean_text(BeautifulSoup(snippet, "html.parser").get_text(" ", strip=True))[:1200]


def protocol_metadata(html: str) -> dict[str, str]:
    soup = BeautifulSoup(html, "html.parser")
    captions = [clean_text(node.get_text(" ", strip=True)) for node in soup.select(".prot-caption")]
    headers = [clean_text(node.get_text(" ", strip=True)) for node in soup.select(".prot-header-text")]
    title = captions[0] if captions else ""
    category = captions[-1] if captions else ""
    date = ""
    for text in headers:
        match = re.search(r"(\d{2}\.\d{2}\.\d{2,4})", text)
        if not match:
            continue
        day, month, year = match.group(1).split(".")
        if len(year) == 2:
            year = f"20{year}"
        date = f"{year}-{month}-{day}"
        break
    return {"date": date, "title": title, "category": category, "city": "Москва"}


def fetch_or_read(session: requests.Session, row: dict[str, str]) -> tuple[str, str, str, str]:
    """Return html, downloaded, http_status, error."""
    path = protocol_cache_path(row["protocol_url"])
    if path.exists():
        return read_compreg_html_file(path), "no", "", ""

    try:
        response = session.get(row["protocol_url"], timeout=(8, 25))
        status = str(response.status_code)
        if response.status_code == 200 and response.content.strip():
            html = write_compreg_html_file(path, response.content, declared_encoding=response.encoding)
            return html, "yes", status, ""
        return "", "no", status, f"http_{response.status_code}"
    except Exception as exc:  # noqa: BLE001 - persisted diagnostic script.
        return "", "no", "", f"{type(exc).__name__}: {exc}"


def import_matches(rows: list[dict[str, str]]) -> list[tuple[str, int, str]]:
    matched_rows = [row for row in rows if row.get("status") == "match_found"]
    if not matched_rows:
        return []
    imported: list[tuple[str, int, str]] = []
    with sqlite3.connect(DEFAULT_DB) as conn:
        db_ids_to_parse: list[int] = []
        for row in matched_rows:
            link = protocol_link(row)
            existing = protocol_present(conn, link)
            if existing is None:
                db_id = upsert_protocol(conn, link, int(row.get("http_status") or 200))
                status = "inserted"
            else:
                db_id = existing
                status = "already_present"
            db_ids_to_parse.append(db_id)
            imported.append((str(link.protocol_id), db_id, status))
        conn.commit()
        parse_new_protocols(conn, db_ids_to_parse)
        conn.commit()
    return imported


def rebuild_report() -> None:
    subprocess.run([sys.executable, str(SCRIPT_DIR / "build_dancer_report.py"), "--idd", TARGET_IDD], cwd=PROJECT_ROOT, check=True)
    subprocess.run([sys.executable, str(SCRIPT_DIR / "render_html_report.py"), "--idd", TARGET_IDD], cwd=PROJECT_ROOT, check=True)


def report_cities() -> list[str]:
    if not REPORT_JSON.exists():
        return []
    data = json.loads(REPORT_JSON.read_text(encoding="utf-8"))
    return sorted({item.get("city") for item in data.get("tournaments", {}).get("items", []) if item.get("city")})


def main() -> int:
    started = datetime.now().isoformat(timespec="seconds")
    if not DEFAULT_CSV.exists():
        print(f"Missing CSV: {DEFAULT_CSV.relative_to(PROJECT_ROOT)}")
        return 1

    with DEFAULT_CSV.open(encoding="utf-8", newline="") as file_obj:
        reader = csv.DictReader(file_obj)
        fieldnames = list(reader.fieldnames or [])
        rows = list(reader)

    if not rows or "status" not in fieldnames:
        print(f"CSV has no status rows: {DEFAULT_CSV.relative_to(PROJECT_ROOT)}")
        return 1

    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }
    )

    checked_no_match = sum(1 for row in rows if row.get("status") == "checked_no_match")
    retry_indexes = [index for index, row in enumerate(rows) if row.get("status") == "download_error"]
    downloaded = 0
    match_found = 0
    log_lines = [
        f"started={started} retry_download_error={len(retry_indexes)} checked_no_match_preserved={checked_no_match}",
    ]

    for count, index in enumerate(retry_indexes, start=1):
        row = rows[index]
        cache_before = protocol_cache_path(row["protocol_url"]).exists()
        html, downloaded_flag, http_status, error = fetch_or_read(session, row)
        terms = matched_terms(html) if html else []
        metadata = protocol_metadata(html) if html else {}
        now = datetime.now().isoformat(timespec="seconds")

        row["checked_at"] = now
        row["cache_before"] = "yes" if cache_before else "no"
        row["downloaded"] = downloaded_flag
        row["http_status"] = http_status
        row["error"] = error
        row["local_cache_path"] = str(protocol_cache_path(row["protocol_url"]).relative_to(PROJECT_ROOT))
        if metadata.get("date"):
            row["date"] = metadata["date"]
        if metadata.get("title"):
            row["tournament_name"] = metadata["title"]
        if metadata.get("category"):
            row["category"] = metadata["category"]
        row["city"] = "Москва"

        if terms:
            row["status"] = "match_found"
            row["matched_terms"] = "; ".join(terms)
            row["context"] = extract_context(html, terms[0])
            match_found += 1
        elif html:
            row["status"] = "checked_no_match"
            row["matched_terms"] = ""
            row["context"] = ""
        else:
            row["status"] = "download_error"
            row["matched_terms"] = ""
            row["context"] = ""

        if downloaded_flag == "yes":
            downloaded += 1
            time.sleep(0.05)
        log_lines.append(
            f"{count:03d}/{len(retry_indexes)} {row['status']} downloaded={downloaded_flag} "
            f"protocol={row.get('protocol_id')} {row.get('category')} err={error}"
        )

    imported = import_matches(rows)
    import_by_protocol = {protocol_id: f"{status}:db_id={db_id}" for protocol_id, db_id, status in imported}
    for row in rows:
        if row.get("protocol_id") in import_by_protocol:
            row["sqlite_import_status"] = import_by_protocol[row["protocol_id"]]

    if imported:
        rebuild_report()

    with DEFAULT_CSV.open("w", encoding="utf-8", newline="") as file_obj:
        writer = csv.DictWriter(file_obj, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    status_counts = Counter(row.get("status") for row in rows)
    still_download_error = status_counts.get("download_error", 0)
    cities = report_cities()

    summary = (
        f"finished={datetime.now().isoformat(timespec='seconds')} "
        f"checked_no_match={status_counts.get('checked_no_match', 0)} "
        f"retried_download_error={len(retry_indexes)} downloaded={downloaded} "
        f"match_found={status_counts.get('match_found', 0)} "
        f"still_download_error={still_download_error} imported={imported} cities={cities}"
    )
    log_lines.insert(1, summary)
    DEFAULT_LOG.write_text("\n".join(log_lines) + "\n", encoding="utf-8")

    print(f"checked_no_match: {status_counts.get('checked_no_match', 0)}")
    print(f"retried_download_error: {len(retry_indexes)}")
    print(f"downloaded: {downloaded}")
    print(f"match_found: {status_counts.get('match_found', 0)}")
    print(f"still_download_error: {still_download_error}")
    print(f"cities in Rossoshanskaya report: {', '.join(cities) if cities else '—'}")
    print(f"CSV: {DEFAULT_CSV.relative_to(PROJECT_ROOT)}")
    print(f"Log: {DEFAULT_LOG.relative_to(PROJECT_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
