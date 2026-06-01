#!/usr/bin/env python3
"""Parse cached Compreg protocol pages into normalized SQLite tables."""

from __future__ import annotations

import argparse
import logging
import re
import sqlite3
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import pandas as pd
from bs4 import BeautifulSoup, Tag

from compreg_encoding import read_compreg_html_file


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB_PATH = PROJECT_ROOT / "database" / "compreg_spb_2025_2026.sqlite"
LOG_PATH = PROJECT_ROOT / "reports" / "parse_protocols.log"

JUDGE_RE = re.compile(r"^([A-ZА-Я])\.\s*(.+)$")
DATE_RE = re.compile(r"(\d{2}\.\d{2}\.\d{2,4})")
SPACE_RE = re.compile(r"\s+")


@dataclass(frozen=True)
class ProtocolRecord:
    db_id: int
    tournament_id: int
    external_protocol_id: int
    cache_path: Path


@dataclass(frozen=True)
class JudgeRef:
    judge_id: int
    judge_index: str
    judge_name: str
    position: int


@dataclass(frozen=True)
class ParticipantRef:
    dancer_id: int
    competitor_number: str
    name: str
    idd: str | None
    club: str | None
    city: str | None
    place: str | None


def normalize_text(value: str | None) -> str:
    if not value:
        return ""
    return SPACE_RE.sub(" ", value.replace("\xa0", " ")).strip()


def clean_empty(value: str | None) -> str | None:
    value = normalize_text(value)
    if not value or value in {"&nbsp;", "-"}:
        return None
    return value


def parse_float(value: str | None) -> float | None:
    value = clean_empty(value)
    if not value:
        return None
    match = re.search(r"\d+(?:[,.]\d+)?", value)
    return float(match.group(0).replace(",", ".")) if match else None


def setup_logging(verbose: bool) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    handlers: list[logging.Handler] = [logging.FileHandler(LOG_PATH, mode="w", encoding="utf-8")]
    if verbose:
        handlers.append(logging.StreamHandler(sys.stdout))
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=handlers,
        force=True,
    )


def ensure_column(conn: sqlite3.Connection, table_name: str, column_name: str, column_type: str) -> None:
    columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table_name})")}
    if column_name not in columns:
        conn.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_type}")


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        PRAGMA foreign_keys = ON;

        CREATE TABLE IF NOT EXISTS protocol_judges (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            protocol_id INTEGER NOT NULL,
            judge_id INTEGER NOT NULL,
            judge_index TEXT NOT NULL,
            judge_position INTEGER NOT NULL,
            judge_name TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(protocol_id, judge_index, judge_position, judge_name),
            FOREIGN KEY (protocol_id) REFERENCES protocols(id),
            FOREIGN KEY (judge_id) REFERENCES judges(id)
        );

        CREATE TABLE IF NOT EXISTS protocol_dancers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            protocol_id INTEGER NOT NULL,
            dancer_id INTEGER NOT NULL,
            competitor_number TEXT NOT NULL,
            dancer_name TEXT NOT NULL,
            idd TEXT,
            club TEXT,
            city TEXT,
            dancer_class TEXT,
            place TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(protocol_id, competitor_number),
            FOREIGN KEY (protocol_id) REFERENCES protocols(id),
            FOREIGN KEY (dancer_id) REFERENCES dancers(id)
        );

        CREATE TABLE IF NOT EXISTS protocol_parse_status (
            protocol_id INTEGER PRIMARY KEY,
            status TEXT NOT NULL,
            message TEXT,
            parsed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            rounds_count INTEGER NOT NULL DEFAULT 0,
            dancers_count INTEGER NOT NULL DEFAULT 0,
            judges_count INTEGER NOT NULL DEFAULT 0,
            marks_count INTEGER NOT NULL DEFAULT 0,
            FOREIGN KEY (protocol_id) REFERENCES protocols(id)
        );
        """
    )
    for column, column_type in (
        ("tournament_title", "TEXT"),
        ("event_date", "TEXT"),
        ("city", "TEXT"),
        ("category", "TEXT"),
        ("parsed_at", "TEXT"),
    ):
        ensure_column(conn, "protocols", column, column_type)
    for column, column_type in (
        ("judge_index", "TEXT"),
        ("judge_position", "INTEGER"),
        ("competitor_number", "TEXT"),
        ("raw_mark_string", "TEXT"),
        ("mark_type", "TEXT"),
        ("parsed_at", "TEXT"),
    ):
        ensure_column(conn, "marks", column, column_type)


def reset_normalized_tables(conn: sqlite3.Connection) -> None:
    conn.execute("DELETE FROM marks")
    conn.execute("DELETE FROM protocol_dancers")
    conn.execute("DELETE FROM protocol_judges")
    conn.execute("DELETE FROM protocol_parse_status")
    conn.execute("DELETE FROM dancers")
    conn.execute("DELETE FROM judges")


def load_protocol_records(conn: sqlite3.Connection, limit: int | None) -> list[ProtocolRecord]:
    sql = """
        SELECT id, tournament_id, protocol_id, protocol_cache_path
        FROM protocols
        WHERE protocol_cache_path IS NOT NULL
        ORDER BY tournament_id, protocol_id
    """
    if limit:
        sql += f" LIMIT {int(limit)}"
    rows = conn.execute(sql).fetchall()
    return [ProtocolRecord(row[0], row[1], row[2], PROJECT_ROOT / row[3]) for row in rows]


def extract_metadata(soup: BeautifulSoup) -> dict[str, str | None]:
    header = soup.select_one(".prot-header-box")
    if not header:
        return {"tournament_title": None, "event_date": None, "city": None, "category": None}
    captions = [normalize_text(node.get_text(" ", strip=True)) for node in header.select(".prot-caption")]
    header_texts = [normalize_text(node.get_text(" ", strip=True)) for node in header.select(".prot-header-text")]
    title = captions[0] if captions else None
    category = captions[-1] if captions else None
    event_date = None
    for text in header_texts:
        match = DATE_RE.search(text)
        if match:
            raw_date = match.group(1)
            fmt = "%d.%m.%y" if len(raw_date.rsplit(".", 1)[-1]) == 2 else "%d.%m.%Y"
            event_date = datetime.strptime(raw_date, fmt).date().isoformat()
            break
    city = title.rsplit("(", 1)[-1].split(")", 1)[0] if title and "(" in title and ")" in title else None
    return {"tournament_title": title, "event_date": event_date, "city": city, "category": category}


def split_club_city(value: str | None) -> tuple[str | None, str | None]:
    value = clean_empty(value)
    if not value:
        return None, None
    if "," not in value:
        return None, value
    club, city = value.rsplit(",", 1)
    return clean_empty(club), clean_empty(city)


def upsert_dancer(conn: sqlite3.Connection, name: str, club: str | None, city: str | None, idd: str | None) -> int:
    conn.execute(
        "INSERT OR IGNORE INTO dancers (name, club, city, external_ref) VALUES (?, ?, ?, ?)",
        (name, club or "", city or "", idd),
    )
    dancer_id = int(conn.execute("SELECT id FROM dancers WHERE name = ? AND club = ? AND city = ?", (name, club or "", city or "")).fetchone()[0])
    if idd:
        conn.execute(
            """
            UPDATE dancers
            SET external_ref = ?
            WHERE id = ?
              AND (external_ref IS NULL OR TRIM(external_ref) = '');
            """,
            (idd, dancer_id),
        )
    return dancer_id


def upsert_judge(conn: sqlite3.Connection, name: str, city: str | None = None) -> int:
    conn.execute("INSERT OR IGNORE INTO judges (name, city) VALUES (?, ?)", (name, city or ""))
    return int(conn.execute("SELECT id FROM judges WHERE name = ? AND city = ?", (name, city or "")).fetchone()[0])


def parse_participants(conn: sqlite3.Connection, protocol_id: int, soup: BeautifulSoup) -> dict[str, ParticipantRef]:
    participants: dict[str, ParticipantRef] = {}
    protocol_caption = None
    for caption in soup.select(".prot-subcaption"):
        if normalize_text(caption.get_text(" ", strip=True)) == "Протокол":
            protocol_caption = caption
            break
    if not protocol_caption:
        return participants
    canvas = protocol_caption.find_parent(class_="prot-table-canvas")
    if not isinstance(canvas, Tag):
        return participants
    for row in canvas.find_all("div", class_="prot-table-box", recursive=False):
        number_node = row.select_one(".prot-table-num")
        number = clean_empty(number_node.get_text(" ", strip=True) if number_node else None)
        if not number:
            continue
        place_node = row.select_one(".prot-table-place .prot-table-result")
        place = clean_empty(place_node.get_text(" ", strip=True) if place_node else None)
        names = [clean_empty(node.get_text(" ", strip=True)) for node in row.select(".prot-table-name")]
        names = [name for name in names if name and name.lower() != "участник"]
        if not names:
            continue
        idds = [clean_empty(node.get_text(" ", strip=True)) for node in row.select(".prot-table-IDD")]
        classes = [clean_empty(node.get_text(" ", strip=True)) for node in row.select(".prot-table-class")]
        club_nodes = row.select(".prot-table-club")
        club_city_text = clean_empty(club_nodes[0].get_text(" ", strip=True) if club_nodes else None)
        club, city = split_club_city(club_city_text)
        dancer_name = " / ".join(names)
        idd = " / ".join([item for item in idds if item]) or None
        dancer_class = " / ".join([item for item in classes if item]) or None
        dancer_id = upsert_dancer(conn, dancer_name, club, city, idd)
        conn.execute(
            """
            INSERT OR REPLACE INTO protocol_dancers (
                protocol_id, dancer_id, competitor_number, dancer_name, idd, club, city, dancer_class, place
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (protocol_id, dancer_id, number, dancer_name, idd, club, city, dancer_class, place),
        )
        participants[number] = ParticipantRef(dancer_id, number, dancer_name, idd, club, city, place)
    return participants


def parse_round_judges(conn: sqlite3.Connection, protocol_id: int, canvas: Tag) -> list[JudgeRef]:
    refs: list[JudgeRef] = []
    for node in canvas.select(".prot-refery-box-hi .prot-refery-org"):
        text = normalize_text(node.get_text(" ", strip=True))
        match = JUDGE_RE.match(text)
        if not match:
            continue
        judge_index, judge_name = match.group(1), match.group(2)
        position = len(refs) + 1
        judge_id = upsert_judge(conn, judge_name)
        conn.execute(
            """
            INSERT OR IGNORE INTO protocol_judges (protocol_id, judge_id, judge_index, judge_position, judge_name)
            VALUES (?, ?, ?, ?, ?)
            """,
            (protocol_id, judge_id, judge_index, position, judge_name),
        )
        refs.append(JudgeRef(judge_id, judge_index, judge_name, position))
    return refs


def iter_round_canvases(soup: BeautifulSoup) -> list[Tag]:
    canvases: list[Tag] = []
    seen_canvas_ids: set[int] = set()
    for caption in soup.select(".prot-subcaption"):
        text = normalize_text(caption.get_text(" ", strip=True))
        if not text or text == "Протокол":
            continue
        canvas = caption.find_parent(class_="prot-table-canvas")
        if (
            isinstance(canvas, Tag)
            and id(canvas) not in seen_canvas_ids
            and (canvas.select_one(".round-data-box") or canvas.select_one(".round-data-box-fkt"))
        ):
            canvases.append(canvas)
            seen_canvas_ids.add(id(canvas))
    return canvases


def parse_mark_string(raw_mark: str, judges: list[JudgeRef]) -> list[tuple[JudgeRef, str]]:
    raw_mark = normalize_text(raw_mark)
    if not raw_mark or not judges:
        return []
    if classify_mark_value(raw_mark) in {"not_available", "aggregate_place"}:
        return []
    if len(raw_mark) != len(judges):
        return []
    if any(char != "-" and classify_mark_value(char) not in {"numeric_place", "cross"} for char in raw_mark):
        return []
    values: list[tuple[JudgeRef, str]] = []
    for pos, char in enumerate(raw_mark):
        if char == "-":
            continue
        values.append((judges[pos], char))
    return values


def classify_mark_value(value: str | None) -> str:
    value = clean_empty(value)
    if not value:
        return "empty"
    if value.upper() in {"#Н/Д", "Н/Д", "N/A"}:
        return "not_available"
    if re.fullmatch(r"\d+,\d+", value):
        return "aggregate_place"
    if re.fullmatch(r"\d+(?:[,.]\d+)?", value):
        return "numeric_place"
    if re.fullmatch(r"[A-ZА-Я]", value):
        return "cross"
    return "unknown"


def parse_round_marks(conn: sqlite3.Connection, protocol_id: int, canvas: Tag, participants: dict[str, ParticipantRef]) -> int:
    caption_node = canvas.select_one(".prot-subcaption")
    round_name = normalize_text(caption_node.get_text(" ", strip=True) if caption_node else "")
    judges = parse_round_judges(conn, protocol_id, canvas)
    if not judges:
        return 0
    rows = canvas.find_all("div", class_="round-data-box", recursive=False)
    if not rows:
        rows = canvas.select(".round-DT-caption > .round-data-box, .prot-table-canvas > .round-data-box")
    inserted = 0
    for row in rows:
        number_node = row.select_one(".round-data-num-all")
        number = clean_empty(number_node.get_text(" ", strip=True).replace("№", "") if number_node else None)
        if not number or number == "Участники":
            continue
        participant = participants.get(number)
        if not participant:
            continue
        place_node = row.select_one(".round-data-place-all")
        place_text = clean_empty(place_node.get_text(" ", strip=True).replace("Место в туре -", "") if place_node else None)
        place = parse_float(place_text)
        dance_box = row.select_one(".round-data-dance")
        if not dance_box:
            continue
        children = [child for child in dance_box.find_all("div", recursive=False) if isinstance(child, Tag)]
        idx = 0
        while idx < len(children):
            dance = raw_mark = None
            if "visible-ph-al" in children[idx].get("class", []):
                dance = clean_empty(children[idx].get_text(" ", strip=True))
                idx += 1
            if idx < len(children) and "round-data-marks-mt" in children[idx].get("class", []):
                raw_mark = clean_empty(children[idx].get_text(" ", strip=True))
                idx += 1
            if idx < len(children) and "round-data-sum" in children[idx].get("class", []):
                idx += 1
            if not dance or not raw_mark:
                idx += 1
                continue
            for judge, mark in parse_mark_string(raw_mark, judges):
                conn.execute(
                    """
                    INSERT INTO marks (
                        protocol_id, dancer_id, judge_id, round_name, dance_name, mark_value,
                        place_value, competitor_number, judge_index, judge_position, raw_mark_string,
                        mark_type, parsed_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                    """,
                    (
                        protocol_id,
                        participant.dancer_id,
                        judge.judge_id,
                        round_name,
                        dance,
                        mark,
                        place,
                        participant.competitor_number,
                        judge.judge_index,
                        judge.position,
                        raw_mark,
                        classify_mark_value(mark),
                    ),
                )
                inserted += 1
    return inserted


def parse_fkt_round_marks(conn: sqlite3.Connection, protocol_id: int, canvas: Tag, participants: dict[str, ParticipantRef]) -> int:
    round_node = canvas.select_one(":scope > .prot-subcaption")
    round_name = normalize_text(round_node.get_text(" ", strip=True) if round_node else "")
    judges = parse_round_judges(conn, protocol_id, canvas)
    if not judges:
        return 0

    inserted = 0
    for dance_box in canvas.find_all("div", class_="round-data-box-fkt", recursive=False):
        dance_node = dance_box.find("div", class_="prot-subcaption", recursive=False)
        dance = clean_empty(dance_node.get_text(" ", strip=True) if dance_node else None)
        if not dance:
            continue
        for row in dance_box.find_all("div", class_="round-data-cappel-box-fkt", recursive=False):
            number_node = row.find("div", class_="round-data-num", recursive=False)
            number = clean_empty(number_node.get_text(" ", strip=True).replace("№", "") if number_node else None)
            if not number or number == "Участники":
                continue
            participant = participants.get(number)
            if not participant:
                continue
            raw_node = row.find("div", class_="round-data-marks-mt-fkt", recursive=False)
            raw_mark = clean_empty(raw_node.get_text(" ", strip=True) if raw_node else None)
            if not raw_mark:
                continue
            place_node = row.find("div", class_="round-data-place", recursive=False)
            place_text = clean_empty(place_node.get_text(" ", strip=True).replace("Место в туре -", "") if place_node else None)
            place = parse_float(place_text)
            for judge, mark in parse_mark_string(raw_mark, judges):
                conn.execute(
                    """
                    INSERT INTO marks (
                        protocol_id, dancer_id, judge_id, round_name, dance_name, mark_value,
                        place_value, competitor_number, judge_index, judge_position, raw_mark_string,
                        mark_type, parsed_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                    """,
                    (
                        protocol_id,
                        participant.dancer_id,
                        judge.judge_id,
                        round_name,
                        dance,
                        mark,
                        place,
                        participant.competitor_number,
                        judge.judge_index,
                        judge.position,
                        raw_mark,
                        classify_mark_value(mark),
                    ),
                )
                inserted += 1
    return inserted


def mark_status(conn: sqlite3.Connection, protocol_id: int, status: str, message: str | None, rounds_count: int = 0, dancers_count: int = 0, judges_count: int = 0, marks_count: int = 0) -> None:
    conn.execute(
        """
        INSERT OR REPLACE INTO protocol_parse_status (
            protocol_id, status, message, parsed_at, rounds_count, dancers_count, judges_count, marks_count
        ) VALUES (?, ?, ?, CURRENT_TIMESTAMP, ?, ?, ?, ?)
        """,
        (protocol_id, status, message, rounds_count, dancers_count, judges_count, marks_count),
    )


def parse_protocol(conn: sqlite3.Connection, record: ProtocolRecord) -> tuple[str, int]:
    if not record.cache_path.exists():
        mark_status(conn, record.db_id, "skipped", f"missing cache: {record.cache_path}")
        return "skipped", 0
    html = read_compreg_html_file(record.cache_path)
    soup = BeautifulSoup(html, "html.parser")
    metadata = extract_metadata(soup)
    conn.execute(
        """
        UPDATE protocols
        SET tournament_title = ?, event_date = ?, city = ?, category = ?, parsed_at = CURRENT_TIMESTAMP
        WHERE id = ?
        """,
        (metadata["tournament_title"], metadata["event_date"], metadata["city"], metadata["category"], record.db_id),
    )
    participants = parse_participants(conn, record.db_id, soup)
    rounds = iter_round_canvases(soup)
    marks_count = 0
    for canvas in rounds:
        marks_count += parse_round_marks(conn, record.db_id, canvas, participants)
        marks_count += parse_fkt_round_marks(conn, record.db_id, canvas, participants)
    judges_count = conn.execute("SELECT COUNT(*) FROM protocol_judges WHERE protocol_id = ?", (record.db_id,)).fetchone()[0]
    if not participants:
        mark_status(conn, record.db_id, "skipped", "no participants parsed", len(rounds), 0, judges_count, marks_count)
        return "skipped", marks_count
    if not rounds:
        mark_status(conn, record.db_id, "skipped", "no_round_sections_result_summary_only", 0, len(participants), judges_count, marks_count)
        return "skipped", marks_count
    if marks_count == 0:
        message = "unsupported_fkt_round_layout" if soup.select(".round-data-box-fkt") else "no marks parsed"
        mark_status(conn, record.db_id, "partial", message, len(rounds), len(participants), judges_count, marks_count)
        return "partial", marks_count
    mark_status(conn, record.db_id, "parsed", None, len(rounds), len(participants), judges_count, marks_count)
    return "parsed", marks_count


def print_summary(conn: sqlite3.Connection) -> None:
    counts = pd.read_sql_query(
        """
        SELECT status, COUNT(*) AS protocols, SUM(marks_count) AS marks
        FROM protocol_parse_status
        GROUP BY status
        ORDER BY protocols DESC;
        """,
        conn,
    )
    sample_marks = pd.read_sql_query(
        """
        SELECT p.protocol_id, m.round_name AS round, m.dance_name AS dance, j.name AS judge,
               d.name AS dancer, m.mark_value AS mark, m.place_value AS place
        FROM marks m
        JOIN protocols p ON p.id = m.protocol_id
        JOIN judges j ON j.id = m.judge_id
        JOIN dancers d ON d.id = m.dancer_id
        ORDER BY m.id
        LIMIT 12;
        """,
        conn,
    )
    avg_by_judge = pd.read_sql_query(
        """
        SELECT j.name AS judge, ROUND(AVG(CAST(m.mark_value AS REAL)), 3) AS avg_mark, COUNT(*) AS marks
        FROM marks m
        JOIN judges j ON j.id = m.judge_id
        WHERE m.mark_value GLOB '[0-9]*'
        GROUP BY j.id, j.name
        HAVING marks >= 20
        ORDER BY avg_mark ASC, marks DESC
        LIMIT 10;
        """,
        conn,
    )
    avg_by_dance = pd.read_sql_query(
        """
        SELECT dance_name AS dance, ROUND(AVG(CAST(mark_value AS REAL)), 3) AS avg_mark, COUNT(*) AS marks
        FROM marks
        WHERE mark_value GLOB '[0-9]*'
        GROUP BY dance_name
        ORDER BY dance_name;
        """,
        conn,
    )
    print("\nParsing status:")
    print(counts.to_string(index=False) if not counts.empty else "No protocol statuses.")
    print("\nNormalized marks sample:")
    print(sample_marks.to_string(index=False) if not sample_marks.empty else "No marks parsed.")
    print("\nGlobal parser sanity check: average numeric marks by judge")
    print(avg_by_judge.to_string(index=False) if not avg_by_judge.empty else "No numeric marks for judge averages.")
    print("\nGlobal parser sanity check: average numeric marks by dance")
    print(avg_by_dance.to_string(index=False) if not avg_by_dance.empty else "No numeric marks for dance averages.")


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Parse cached Compreg protocol HTML into normalized SQLite tables.")
    parser.add_argument("--db-path", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--keep-existing", action="store_true", help="Do not clear normalized protocol tables before parsing.")
    parser.add_argument("--verbose", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    setup_logging(args.verbose)
    start = time.time()
    with sqlite3.connect(args.db_path) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        init_schema(conn)
        if not args.keep_existing:
            reset_normalized_tables(conn)
        records = load_protocol_records(conn, args.limit)
        logging.info("Protocols to parse: %s", len(records))
        parsed = partial = skipped = total_marks = 0
        for index, record in enumerate(records, start=1):
            try:
                status, marks_count = parse_protocol(conn, record)
            except Exception as exc:
                logging.exception("Failed protocol_id=%s cache=%s", record.db_id, record.cache_path)
                mark_status(conn, record.db_id, "error", str(exc))
                status, marks_count = "error", 0
            total_marks += marks_count
            parsed += status == "parsed"
            partial += status == "partial"
            skipped += status in {"skipped", "error"}
            if index % 100 == 0:
                conn.commit()
                print(f"Parsed protocols: {index}/{len(records)}; parsed={parsed}; partial={partial}; skipped/errors={skipped}; marks={total_marks}", flush=True)
        conn.commit()
        print_summary(conn)
    print(f"\nDone in {time.time() - start:.1f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
