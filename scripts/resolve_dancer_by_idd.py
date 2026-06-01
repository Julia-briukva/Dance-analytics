#!/usr/bin/env python3
"""Prototype resolver from Compreg IDD to local dancers.id."""

from __future__ import annotations

import argparse
import re
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path

import requests
from bs4 import BeautifulSoup

from compreg_encoding import read_compreg_html_file, write_compreg_html_file


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB_PATH = PROJECT_ROOT / "database" / "compreg_spb_2025_2026.sqlite"
CACHE_DIR = PROJECT_ROOT / "data" / "cache" / "dancer_profiles"
CARD_URL = "https://compreg.ru/danceinfo.php"
REQUEST_TIMEOUT = (6, 15)
USER_AGENT = "DanceAnalyticsResolverPrototype/1.0"
SPACE_RE = re.compile(r"\s+")


@dataclass(frozen=True)
class DancerCard:
    idd: str
    name: str | None
    club: str | None
    city: str | None
    source: str
    cache_path: Path | None


@dataclass(frozen=True)
class Candidate:
    internal_dancer_id: int
    name: str
    club: str | None
    city: str | None
    external_ref: str | None
    confidence: int
    reason: str


def normalize_text(value: str | None) -> str:
    if not value:
        return ""
    value = value.lower().replace("ё", "е")
    value = re.sub(r"[^0-9a-zа-я]+", " ", value)
    return SPACE_RE.sub(" ", value).strip()


def clean_text(value: str | None) -> str | None:
    if not value:
        return None
    cleaned = SPACE_RE.sub(" ", value.replace("\xa0", " ")).strip()
    return cleaned or None


def cache_path_for(idd: str) -> Path:
    return CACHE_DIR / f"{idd}_danceinfo_post_ci_{idd}.html"


def fetch_card_html(idd: str) -> tuple[str, str, Path | None]:
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})
    payload = {"ci": idd}
    cache_path = cache_path_for(idd)
    try:
        response = session.post(CARD_URL, data=payload, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
    except requests.RequestException as exc:
        if cache_path.exists():
            return read_compreg_html_file(cache_path), f"cache fallback after network error: {exc}", cache_path
        raise RuntimeError(f"Failed to fetch Compreg card and no cache fallback exists: {exc}") from exc

    html = write_compreg_html_file(cache_path, response.content, declared_encoding=response.encoding)
    return html, f"live POST {CARD_URL} body={{'ci': '{idd}'}}", cache_path


def label_value_pairs(info: BeautifulSoup) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    for prop in info.select(".res-dancer-info-prop"):
        label = clean_text(prop.select_one(".res-dancer-info-label").get_text(" ", strip=True) if prop.select_one(".res-dancer-info-label") else None)
        value = clean_text(prop.select_one(".res-dancer-info").get_text(" ", strip=True) if prop.select_one(".res-dancer-info") else None)
        if label:
            pairs.append((label, value or ""))
    return pairs


def parse_card(html: str, idd: str, source: str, cache_path: Path | None) -> DancerCard:
    soup = BeautifulSoup(html, "html.parser")
    info = soup.select_one(".res-dancer-info-table") or soup

    name = clean_text(info.select_one(".res-dancer-is").get_text(" ", strip=True) if info.select_one(".res-dancer-is") else None)
    pairs = label_value_pairs(info)

    found_idd = None
    club = None
    city = None
    for label, value in pairs:
        label_norm = normalize_text(label)
        if label_norm == "idd" and value:
            found_idd = value
        if not value and "," in label and "тренер" not in label_norm and label_norm not in {"класс", "пол", "статус"}:
            parts = [clean_text(part) for part in label.split(",")]
            parts = [part for part in parts if part]
            if len(parts) >= 2:
                club, city = parts[0], parts[1]
                break

    if not found_idd:
        text = clean_text(info.get_text(" ", strip=True)) or ""
        match = re.search(r"\b(\d{4,12})\b", text)
        found_idd = match.group(1) if match else idd

    return DancerCard(idd=found_idd, name=name, club=club, city=city, source=source, cache_path=cache_path)


def score_candidate(card: DancerCard, row: sqlite3.Row) -> Candidate:
    reasons: list[str] = []
    score = 0

    row_name = row["name"]
    row_club = row["club"] or None
    row_city = row["city"] or None
    row_ref = row["external_ref"] or None

    if normalize_text(card.name) and normalize_text(card.name) == normalize_text(row_name):
        score += 60
        reasons.append("name exact")
    elif normalize_text(card.name) and normalize_text(card.name) in normalize_text(row_name):
        score += 35
        reasons.append("name partial")

    if normalize_text(card.club) and normalize_text(card.club) == normalize_text(row_club):
        score += 25
        reasons.append("club exact")
    elif normalize_text(card.club) and normalize_text(card.club) in normalize_text(row_club):
        score += 15
        reasons.append("club partial")

    if normalize_text(card.city) and normalize_text(card.city) == normalize_text(row_city):
        score += 15
        reasons.append("city exact")
    elif normalize_text(card.city) and normalize_text(card.city) in normalize_text(row_city):
        score += 8
        reasons.append("city partial")

    if row_ref and row_ref == card.idd:
        reasons.append("external_ref already matches")

    return Candidate(
        internal_dancer_id=int(row["id"]),
        name=row_name,
        club=row_club,
        city=row_city,
        external_ref=row_ref,
        confidence=min(score, 100),
        reason=", ".join(reasons) if reasons else "no strong match",
    )


def find_candidates(conn: sqlite3.Connection, card: DancerCard) -> list[Candidate]:
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT id, name, club, city, external_ref
        FROM dancers
        ORDER BY id;
        """
    ).fetchall()
    candidates = [score_candidate(card, row) for row in rows]
    candidates = [candidate for candidate in candidates if candidate.confidence > 0]
    candidates.sort(key=lambda item: (item.confidence, item.internal_dancer_id), reverse=True)
    return candidates[:10]


def idd_links(conn: sqlite3.Connection, idd: str) -> list[sqlite3.Row]:
    conn.row_factory = sqlite3.Row
    return conn.execute(
        """
        SELECT id, name, club, city, external_ref
        FROM dancers
        WHERE external_ref = ?
        ORDER BY id;
        """,
        (idd,),
    ).fetchall()


def decision(conn: sqlite3.Connection, card: DancerCard, candidates: list[Candidate]) -> tuple[str, str, Candidate | None]:
    if not candidates:
        return "MANUAL REVIEW REQUIRED", "no local candidates found", None

    high_confidence = [candidate for candidate in candidates if candidate.confidence >= 90]
    if len(high_confidence) != 1:
        return "MANUAL REVIEW REQUIRED", f"high-confidence candidates: {len(high_confidence)}", None

    selected = high_confidence[0]
    if selected.external_ref and selected.external_ref != card.idd:
        return "MANUAL REVIEW REQUIRED", "selected candidate has a different external_ref", None

    linked_rows = idd_links(conn, card.idd)
    other_links = [row for row in linked_rows if int(row["id"]) != selected.internal_dancer_id]
    if other_links:
        linked = ", ".join(f"{row['id']}:{row['name']}" for row in other_links)
        return "MANUAL REVIEW REQUIRED", f"IDD is already linked to another dancer: {linked}", None

    return "AUTO-LINK SAFE", "single high-confidence match and no conflicting external_ref", selected


def print_result(card: DancerCard, candidates: list[Candidate], status: str, reason: str) -> None:
    print("Compreg card")
    print(f"  source: {card.source}")
    print(f"  cache: {card.cache_path.relative_to(PROJECT_ROOT) if card.cache_path else '-'}")
    print(f"  idd: {card.idd or '-'}")
    print(f"  name: {card.name or '-'}")
    print(f"  club: {card.club or '-'}")
    print(f"  city: {card.city or '-'}")

    print("\nLocal candidates")
    if not candidates:
        print("  no candidates")
    for candidate in candidates:
        print(
            "  "
            f"internal_dancer_id={candidate.internal_dancer_id}; "
            f"name={candidate.name}; "
            f"club={candidate.club or '-'}; "
            f"city={candidate.city or '-'}; "
            f"external_ref={candidate.external_ref or '-'}; "
            f"confidence={candidate.confidence}; "
            f"reason={candidate.reason}"
        )

    print(f"\n{status}")
    print(f"Reason: {reason}")


def apply_link(conn: sqlite3.Connection, card: DancerCard, candidate: Candidate) -> int:
    if candidate.external_ref == card.idd:
        print("\nalready linked")
        print(f"internal_dancer_id: {candidate.internal_dancer_id}")
        print(f"name: {candidate.name}")
        print(f"previous external_ref: {candidate.external_ref}")
        print(f"new external_ref: {card.idd}")
        print("rows updated: 0")
        return 0

    if candidate.external_ref and candidate.external_ref != card.idd:
        raise RuntimeError("Selected candidate has a different external_ref. Refusing to overwrite without --force.")

    try:
        conn.execute("BEGIN")
        linked_rows = idd_links(conn, card.idd)
        other_links = [row for row in linked_rows if int(row["id"]) != candidate.internal_dancer_id]
        if other_links:
            linked = ", ".join(f"{row['id']}:{row['name']}" for row in other_links)
            raise RuntimeError(f"IDD is already linked to another dancer: {linked}")

        before = conn.execute("SELECT external_ref FROM dancers WHERE id = ?", (candidate.internal_dancer_id,)).fetchone()
        previous_ref = before[0] if before else None
        if previous_ref and previous_ref != card.idd:
            raise RuntimeError("Selected candidate has a different external_ref. Refusing to overwrite without --force.")

        cursor = conn.execute(
            """
            UPDATE dancers
            SET external_ref = ?
            WHERE id = ?
              AND (external_ref IS NULL OR TRIM(external_ref) = '' OR external_ref = ?);
            """,
            (card.idd, candidate.internal_dancer_id, card.idd),
        )
        rows_updated = cursor.rowcount
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    print("\nlink applied")
    print(f"internal_dancer_id: {candidate.internal_dancer_id}")
    print(f"name: {candidate.name}")
    print(f"previous external_ref: {previous_ref or '-'}")
    print(f"new external_ref: {card.idd}")
    print(f"rows updated: {rows_updated}")
    return rows_updated


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Resolve Compreg IDD to a local dancer candidate.")
    parser.add_argument("--idd", required=True, help="External Compreg dancer IDD.")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true", help="Inspect candidates without writing SQLite.")
    mode.add_argument("--apply", action="store_true", help="Write dancers.external_ref only when AUTO-LINK SAFE.")
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    idd = str(args.idd).strip()
    html, source, cache_path = fetch_card_html(idd)
    card = parse_card(html, idd, source, cache_path)
    with sqlite3.connect(DEFAULT_DB_PATH) as conn:
        candidates = find_candidates(conn, card)
        status, reason, selected = decision(conn, card, candidates)
        print_result(card, candidates, status, reason)
        if args.apply:
            if status != "AUTO-LINK SAFE" or selected is None:
                raise RuntimeError("Refusing to apply: match is not AUTO-LINK SAFE.")
            apply_link(conn, card, selected)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
