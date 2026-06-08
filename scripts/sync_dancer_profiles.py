#!/usr/bin/env python3
"""Sync Compreg dancer profile cards used by report headers."""

from __future__ import annotations

import argparse
import csv
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TextIO

import requests

from compreg_encoding import read_compreg_html_file, write_compreg_html_file
from compreg_profile import clean_text, parse_profile_html


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CLUB_CSV = PROJECT_ROOT / "data" / "club_triumph_spb_dancers.csv"
DEFAULT_AUDIT_CSV = PROJECT_ROOT / "reports" / "dancer_header_profile_sync.csv"
CACHE_DIR = PROJECT_ROOT / "data" / "cache" / "dancer_profiles"
CARD_URL = "https://compreg.ru/danceinfo.php"
REQUEST_TIMEOUT = (6, 15)
USER_AGENT = "DanceAnalyticsProfileSync/1.0"
AUDIT_FIELDS = [
    "idd",
    "name",
    "compreg_url",
    "cache_path",
    "card_status",
    "http_status",
    "compreg_has_header_data",
    "club",
    "city",
    "coaches_st",
    "coaches_la",
    "class_st",
    "class_la",
    "skr_class_st",
    "skr_class_la",
    "norm_status_st",
    "norm_status_la",
    "reason",
]


@dataclass(frozen=True)
class ProfileSyncResult:
    idd: str
    name: str
    compreg_url: str
    cache_path: Path
    card_status: str
    http_status: str
    profile: dict[str, str]
    reason: str


def profile_cache_path(idd: str) -> Path:
    return CACHE_DIR / f"{idd}_danceinfo_post_ci_{idd}.html"


def read_rows(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as file_obj:
        return [row for row in csv.DictReader(file_obj) if clean_text(row.get("idd"))]


def has_header_data(profile: dict[str, str]) -> bool:
    return any(
        profile.get(key)
        for key in [
            "club",
            "city",
            "coaches_st",
            "coaches_la",
            "class_st",
            "class_la",
            "skr_class_st",
            "skr_class_la",
            "norm_status_st",
            "norm_status_la",
        ]
    )


def fetch_or_read_profile(
    session: requests.Session,
    row: dict[str, str],
    *,
    refresh: bool = False,
) -> ProfileSyncResult:
    idd = clean_text(row.get("idd"))
    name = clean_text(row.get("name"))
    cache_path = profile_cache_path(idd)
    compreg_url = CARD_URL

    if cache_path.exists() and not refresh:
        html = read_compreg_html_file(cache_path)
        profile = parse_profile_html(html)
        return ProfileSyncResult(
            idd=idd,
            name=name,
            compreg_url=compreg_url,
            cache_path=cache_path,
            card_status="cache",
            http_status="",
            profile=profile,
            reason="cache hit" if has_header_data(profile) else "cache exists but header data not found",
        )

    try:
        response = session.post(CARD_URL, data={"ci": idd}, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
    except requests.RequestException as exc:
        if cache_path.exists():
            html = read_compreg_html_file(cache_path)
            profile = parse_profile_html(html)
            return ProfileSyncResult(
                idd=idd,
                name=name,
                compreg_url=compreg_url,
                cache_path=cache_path,
                card_status="cache_fallback",
                http_status="",
                profile=profile,
                reason=f"network error, used existing cache: {exc}",
            )
        return ProfileSyncResult(
            idd=idd,
            name=name,
            compreg_url=compreg_url,
            cache_path=cache_path,
            card_status="unavailable",
            http_status="",
            profile={},
            reason=str(exc),
        )

    html = write_compreg_html_file(cache_path, response.content, declared_encoding=response.encoding)
    profile = parse_profile_html(html)
    return ProfileSyncResult(
        idd=idd,
        name=name,
        compreg_url=compreg_url,
        cache_path=cache_path,
        card_status="fetched",
        http_status=str(response.status_code),
        profile=profile,
        reason="fetched and parsed" if has_header_data(profile) else "fetched but header data not found",
    )


def result_to_row(result: ProfileSyncResult) -> dict[str, str]:
    profile = result.profile
    return {
        "idd": result.idd,
        "name": result.name,
        "compreg_url": result.compreg_url,
        "cache_path": str(result.cache_path.relative_to(PROJECT_ROOT)),
        "card_status": result.card_status,
        "http_status": result.http_status,
        "compreg_has_header_data": "yes" if has_header_data(profile) else "no",
        "club": profile.get("club", ""),
        "city": profile.get("city", ""),
        "coaches_st": profile.get("coaches_st", ""),
        "coaches_la": profile.get("coaches_la", ""),
        "class_st": profile.get("class_st", ""),
        "class_la": profile.get("class_la", ""),
        "skr_class_st": profile.get("skr_class_st", ""),
        "skr_class_la": profile.get("skr_class_la", ""),
        "norm_status_st": profile.get("norm_status_st", ""),
        "norm_status_la": profile.get("norm_status_la", ""),
        "reason": result.reason,
    }


def sync_profiles(
    rows: list[dict[str, str]],
    *,
    audit_csv: Path = DEFAULT_AUDIT_CSV,
    refresh: bool = False,
    log: TextIO | None = None,
) -> list[dict[str, str]]:
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})
    audit_rows: list[dict[str, str]] = []
    for index, row in enumerate(rows, start=1):
        result = fetch_or_read_profile(session, row, refresh=refresh)
        audit_row = result_to_row(result)
        audit_rows.append(audit_row)
        if log:
            log.write(
                f"profile [{index}/{len(rows)}] idd={result.idd} status={result.card_status} "
                f"header={audit_row['compreg_has_header_data']} reason={result.reason}\n"
            )
    audit_csv.parent.mkdir(parents=True, exist_ok=True)
    with audit_csv.open("w", encoding="utf-8", newline="") as file_obj:
        writer = csv.DictWriter(file_obj, fieldnames=AUDIT_FIELDS)
        writer.writeheader()
        writer.writerows(audit_rows)
    return audit_rows


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Sync Compreg dancer profile cards for report headers.")
    parser.add_argument("--club-csv", type=Path, default=DEFAULT_CLUB_CSV)
    parser.add_argument("--audit-csv", type=Path, default=DEFAULT_AUDIT_CSV)
    parser.add_argument("--refresh", action="store_true", help="Re-fetch cards even if cache already exists.")
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    rows = read_rows(args.club_csv)
    audit_rows = sync_profiles(rows, audit_csv=args.audit_csv, refresh=args.refresh)
    counts = {
        "checked": len(audit_rows),
        "with_header_data": sum(row["compreg_has_header_data"] == "yes" for row in audit_rows),
        "unavailable": sum(row["card_status"] == "unavailable" for row in audit_rows),
        "fetched": sum(row["card_status"] == "fetched" for row in audit_rows),
        "cache": sum(row["card_status"] == "cache" for row in audit_rows),
    }
    print(f"Checked: {counts['checked']}")
    print(f"With header data: {counts['with_header_data']}")
    print(f"Fetched: {counts['fetched']}")
    print(f"Cache: {counts['cache']}")
    print(f"Unavailable: {counts['unavailable']}")
    print(f"Audit CSV: {args.audit_csv.relative_to(PROJECT_ROOT)}")
    return 0 if counts["unavailable"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
