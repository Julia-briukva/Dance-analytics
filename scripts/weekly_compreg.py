#!/usr/bin/env python3
"""Discover Compreg results, import club protocols and rebuild validated reports."""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

import build_spb_database as crawler
from compreg_encoding import decode_compreg_html
from parse_protocols import ProtocolRecord, extract_metadata, init_schema, parse_protocol
from update_from_compreg_listcat import parse_listcat_url

ROOT = Path(__file__).resolve().parents[1]
RESULTS_URL = "https://compreg.ru/results.php"
CLUB_CSV = ROOT / "data/club_triumph_spb_dancers.csv"
DB = ROOT / "database/compreg_spb_2025_2026.sqlite"
IDD_RE = re.compile(r"(?<!\d)\d{5,10}(?!\d)")
RESULT_RE = re.compile(r"LoadResults\(\s*['\"](\d{10})['\"]\s*,\s*1\s*\)")
LINK_RE = re.compile(r"/resultsdata/(\d{4})/(\d{2})/(\d{4})/listcat\.php")


def read_json(path: Path, fallback):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else fallback


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def normalize_name(value: str) -> str:
    return " ".join(value.casefold().replace("ё", "е").split())


def club_members(path: Path = CLUB_CSV) -> tuple[set[str], set[str]]:
    with path.open(encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    ids = {i for row in rows for i in IDD_RE.findall(row.get("idd", ""))}
    names = {normalize_name(row.get("name", "")) for row in rows if row.get("name")}
    if not ids:
        raise ValueError("Клубный список пуст или не содержит IDD")
    return ids, names


def tournament_url(tid: str) -> str:
    url = f"https://compreg.ru/resultsdata/{tid[:4]}/{tid[4:6]}/{tid[6:]}/listcat.php"
    parse_listcat_url(url)  # Reject invalid dates and malformed identifiers.
    return url


def discover(html: str, today: date, days: int) -> list[str]:
    ids = set(RESULT_RE.findall(html))
    ids.update("".join(parts) for parts in LINK_RE.findall(html))
    if not ids:
        raise ValueError("Не распознана страница результатов Compreg; публикация остановлена")
    urls = []
    for tid in sorted(ids):
        try:
            url = tournament_url(tid)
        except ValueError:
            continue
        event_date = parse_listcat_url(url).event_date
        if today - timedelta(days=days) <= event_date <= today:
            urls.append(url)
    return urls


def get_html(url: str) -> str:
    if urlparse(url).hostname not in {"compreg.ru", "www.compreg.ru"}:
        raise ValueError("Разрешены только страницы Compreg")
    session = requests.Session()
    session.headers["User-Agent"] = "DanceAnalytics/weekly-results (club report refresh)"
    retry = Retry(total=3, backoff_factor=1, status_forcelist=[429, 500, 502, 503, 504])
    session.mount("https://", HTTPAdapter(max_retries=retry))
    try:
        response = session.get(url, timeout=(10, 40))
        response.raise_for_status()
        if urlparse(response.url).hostname not in {"compreg.ru", "www.compreg.ru"}:
            raise ValueError("Compreg перенаправил запрос на другой сайт")
        return decode_compreg_html(response.content, response.encoding)
    finally:
        session.close()
        time.sleep(0.35)


def participant_info(html: str) -> tuple[set[str], set[str]]:
    soup = BeautifulSoup(html, "html.parser")
    if not soup.select_one(".prot-header-box") or not soup.select(".prot-table-name"):
        raise ValueError("Не распознан протокол участников (возможно, изменился формат Compreg)")
    ids = {i for node in soup.select(".prot-table-IDD") for i in IDD_RE.findall(node.get_text(" ", strip=True))}
    names = {normalize_name(node.get_text(" ", strip=True)) for node in soup.select(".prot-table-name")}
    return ids, names


def confirmed_identities(conn: sqlite3.Connection, club_ids: set[str]) -> dict:
    rows = list(conn.execute("SELECT id, name, club, city, external_ref FROM dancers"))
    refs = {}
    for did, name, club, city, external in rows:
        refs.setdefault(did, set()).update(IDD_RE.findall(external or ""))
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='dancer_external_refs'").fetchone():
        for external, did in conn.execute("SELECT external_ref, dancer_id FROM dancer_external_refs"):
            refs.setdefault(did, set()).update(IDD_RE.findall(external or ""))
    identities = {}
    for did, name, club, city, external in rows:
        ids = refs.get(did, set()) & club_ids
        if not ids or not club or not city:
            continue
        key = tuple(normalize_name(v) for v in (name, club, city))
        identities.setdefault(key, {})[did] = ids
    return {key: (next(iter(options)), next(iter(options.values()))) for key, options in identities.items() if len(options) == 1}


def identify_participants(html: str, club_ids: set[str], identities: dict):
    from parse_protocols import normalize_text, split_club_city
    soup = BeautifulSoup(html, "html.parser")
    matches = set()
    bindings = []
    for row in soup.select(".prot-table-box"):
        names = [normalize_text(n.get_text(" ", strip=True)) for n in row.select(".prot-table-name")]
        names = [name for name in names if name and name.casefold() != "участник"]
        if not names:
            continue
        ids = {i for n in row.select(".prot-table-IDD") for i in IDD_RE.findall(n.get_text(" ", strip=True))}
        if ids:
            row_matches = ids & club_ids
            matches.update(row_matches)
            targets = {did for did, confirmed_ids in identities.values() if confirmed_ids & row_matches}
            number_node = row.select_one(".prot-table-num")
            if len(targets) == 1 and number_node is not None:
                bindings.append((normalize_text(number_node.get_text(" ", strip=True)), next(iter(targets)), " / ".join(sorted(ids))))
            continue  # Never override an explicitly different IDD by name.
        club_node = row.select_one(".prot-table-club")
        club, city = split_club_city(club_node.get_text(" ", strip=True) if club_node else None)
        key = tuple(normalize_name(v or "") for v in (" / ".join(names), club, city))
        if key not in identities:
            continue
        number_node = row.select_one(".prot-table-num")
        if number_node is None:
            continue
        did, confirmed_ids = identities[key]
        number = normalize_text(number_node.get_text(" ", strip=True))
        matches.update(confirmed_ids)
        bindings.append((number, did, " / ".join(sorted(confirmed_ids))))
    return matches, bindings


def fetched_protocol(task: tuple[int, int]):
    tid, pid = task
    html = get_html(crawler.protocol_url(tid, pid))
    ids, names = participant_info(html)
    return pid, html, ids, names


def import_protocol(conn: sqlite3.Connection, page, pid: int, html: str, bindings=()) -> None:
    cache_path = crawler.protocol_cache_path(page.tournament_id, pid)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(html, encoding="utf-8")
    metadata = extract_metadata(BeautifulSoup(html, "html.parser"))
    if not metadata.get("category") or not metadata.get("event_date"):
        raise ValueError(f"Не распознаны категория/дата протокола {pid}")
    # A refresh replaces all rows for this protocol inside a transaction. No stale marks.
    conn.execute("SAVEPOINT refresh_protocol")
    try:
        crawler.upsert_tournament(conn, page)
        crawler.upsert_protocol(conn, page.tournament_id, pid, crawler.protocol_url(page.tournament_id, pid), cache_path, 200)
        dbid = conn.execute("SELECT id FROM protocols WHERE tournament_id=? AND protocol_id=?", (page.tournament_id, pid)).fetchone()[0]
        old_marks = conn.execute("SELECT count(*) FROM marks WHERE protocol_id=?", (dbid,)).fetchone()[0]
        for table in ("marks", "protocol_dancers", "protocol_judges", "protocol_parse_status"):
            conn.execute(f"DELETE FROM {table} WHERE protocol_id=?", (dbid,))
        status, marks = parse_protocol(conn, ProtocolRecord(dbid, page.tournament_id, pid, cache_path))
        for number, known_did, confirmed_idd in bindings:
            participant = conn.execute("SELECT dancer_id FROM protocol_dancers WHERE protocol_id=? AND competitor_number=?", (dbid, number)).fetchone()
            if participant is None:
                raise ValueError(f"Не удалось связать подтверждённого участника {number} с протоколом {pid}")
            conn.execute("UPDATE marks SET dancer_id=? WHERE protocol_id=? AND dancer_id=? AND competitor_number=?", (known_did, dbid, participant[0], number))
            conn.execute("UPDATE protocol_dancers SET dancer_id=?, idd=? WHERE protocol_id=? AND competitor_number=?", (known_did, confirmed_idd, dbid, number))
        count = conn.execute("SELECT count(*) FROM protocol_dancers WHERE protocol_id=?", (dbid,)).fetchone()[0]
        message = conn.execute("SELECT message FROM protocol_parse_status WHERE protocol_id=?", (dbid,)).fetchone()[0]
        summary_only = status == "skipped" and message == "no_round_sections_result_summary_only"
        if not count or (status != "parsed" and not summary_only) or (old_marks and not marks):
            raise ValueError(f"Протокол {pid}: неполный разбор ({status}, {message}); старые данные сохранены")
        conn.execute("RELEASE refresh_protocol")
    except Exception:
        conn.execute("ROLLBACK TO refresh_protocol")
        conn.execute("RELEASE refresh_protocol")
        raise


def page_for_url(url: str):
    parsed = parse_listcat_url(url)
    html = get_html(url)
    matches = crawler.LOAD_CATEGORY_RE.findall(html)
    if not matches or {int(tid) for tid, _ in matches} != {parsed.tournament_id}:
        raise ValueError(f"Страница категорий пуста или относится к другому турниру: {url}")
    candidate = crawler.Candidate(parsed.event_date, parsed.suffix, parsed.url, parsed.relative_key)
    path = crawler.listcat_cache_path(candidate)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(html, encoding="utf-8")
    page = crawler.parse_tournament_page(candidate, path, city_protocol_sample=0, refresh=False)
    if isinstance(page, crawler.RejectedTournamentPage):
        # Explicit workflow import accepts any city, keeping its original label.
        page = crawler.TournamentPage(
            event_date=page.event_date, suffix=page.suffix, listcat_url=page.listcat_url,
            local_path=page.local_path, tournament_id=page.tournament_id, title=page.title,
            location_text=page.raw_detected_city, raw_detected_city=page.raw_detected_city,
            normalized_city=page.normalized_city, city_detection_source=page.city_detection_source,
            protocol_ids=page.protocol_ids,
        )
    if not isinstance(page, crawler.TournamentPage):
        raise ValueError(f"Не распознана страница турнира: {url}")
    return page


def old_club_protocols(conn: sqlite3.Connection, tid: int, club_ids: set[str], identities: dict) -> set[int]:
    known_ids = {did for did, ids in identities.values()}
    rows = conn.execute("SELECT p.protocol_id, pd.idd, pd.dancer_id FROM protocols p JOIN protocol_dancers pd ON pd.protocol_id=p.id WHERE p.tournament_id=?", (tid,))
    return {int(pid) for pid, idd, did in rows if set(IDD_RE.findall(idd or "")) & club_ids or did in known_ids}


def run_sync(args, summary: dict) -> None:
    if not DB.exists():
        raise ValueError("Нет полной базы. Сначала выполните однократную установку на Mac")
    club_ids, club_names = club_members()
    ledger_path = args.state / "scan.json"
    ledger = read_json(ledger_path, {"protocols": {}})
    ledger.setdefault("imported", {})
    roster_changed = ledger.get("club_ids") != sorted(club_ids)
    urls = [parse_listcat_url(args.url.strip()).url] if args.url.strip() else discover(get_html(RESULTS_URL), datetime.now(timezone.utc).date(), args.lookback_days)
    summary["urls"] = urls
    with sqlite3.connect(DB) as conn:
        conn.execute("PRAGMA foreign_keys=ON")
        init_schema(conn)
        if conn.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise ValueError("Проверка целостности базы не пройдена")
        identities = confirmed_identities(conn, club_ids)
        conn.execute("BEGIN")
        for url in urls:
            page = page_for_url(url)
            item = {"url": url, "title": page.title, "checked": 0, "updated": 0, "dancers": []}
            summary["tournaments"].append(item)
            previously_ours = old_club_protocols(conn, page.tournament_id, club_ids, identities)
            if not previously_ours.issubset(set(page.protocol_ids)):
                raise ValueError(f"Из списка исчезли протоколы наших танцоров: {url}")
            found_ids: set[str] = set()
            with ThreadPoolExecutor(max_workers=args.workers) as pool:
                results = pool.map(fetched_protocol, [(page.tournament_id, pid) for pid in page.protocol_ids])
                for pid, html, ids, names in results:
                    item["checked"] += 1
                    matches, bindings = identify_participants(html, club_ids, identities)
                    found_ids.update(matches)
                    key = f"{page.tournament_id}/{pid}"
                    digest = hashlib.sha256(html.encode("utf-8")).hexdigest()
                    if matches or pid in previously_ours:
                        if ledger["imported"].get(key) != digest:
                            import_protocol(conn, page, pid, html, bindings)
                            ledger["imported"][key] = digest
                            item["updated"] += 1
                    elif names & club_names:
                        summary["review"].append({"url": crawler.protocol_url(page.tournament_id, pid), "names": sorted(names & club_names), "reason": "Совпало имя, но не IDD; требуется проверка"})
                    ledger["protocols"][key] = digest
            item["dancers"] = sorted(found_ids)
            print(f"{page.tournament_id}: проверено {item['checked']}, обновлено {item['updated']}, наши IDD {', '.join(item['dancers']) or 'нет'}", flush=True)
        # All tournaments succeeded before anything is committed to the working database.
        conn.commit()
    updated = sum(t["updated"] for t in summary["tournaments"])
    summary["updated_protocols"] = updated
    needs_publish = bool(updated or roster_changed or (args.state / "pending_publication.json").exists())
    if needs_publish:
        previous_reports = {}
        for path in (ROOT / "reports").glob("dancer_*_report.json"):
            payload = read_json(path, {})
            previous_reports[path.name] = {str(x.get("protocol_id")) for x in payload.get("tournaments", {}).get("protocol_results", [])}
        subprocess.run([sys.executable, str(ROOT / "scripts/build_club_reports.py"), "--skip-profile-sync"], cwd=ROOT, check=True)
        with (ROOT / "reports/club_report_build_status.csv").open(encoding="utf-8") as f:
            statuses = list(csv.DictReader(f))
        status_by_id = {r["idd"]: r["status"] for r in statuses}
        for name, old_ids in previous_reports.items():
            idd = name.removeprefix("dancer_").removesuffix("_report.json")
            if status_by_id.get(idd) != "success":
                raise ValueError(f"Пропал ранее готовый отчёт {name}")
            new = read_json(ROOT / "reports" / name, {})
            new_ids = {str(x.get("protocol_id")) for x in new.get("tournaments", {}).get("protocol_results", [])}
            if not old_ids.issubset(new_ids):
                raise ValueError(f"Из отчёта {name} пропали исторические протоколы")
        subprocess.run([sys.executable, str(ROOT / "scripts/deploy_pages_reports.py"), "--all", "--index"], cwd=ROOT, check=True)
    ledger["club_ids"] = sorted(club_ids)
    write_json(ledger_path, ledger)
    if needs_publish:
        write_json(args.state / "pending_publication.json", {"pending": True})
    summary["status"] = "success"
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as f:
            f.write(f"updated={'true' if needs_publish else 'false'}\n")


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--url", default="", help="Необязательная ссылка listcat.php")
    p.add_argument("--state", type=Path, required=True)
    p.add_argument("--lookback-days", type=int, default=28)
    p.add_argument("--workers", type=int, default=3, choices=range(1, 7))
    args = p.parse_args(argv)
    if args.lookback_days < 1:
        p.error("lookback-days must be positive")
    summary = {"status": "failed", "tournaments": [], "review": [], "updated_protocols": 0}
    try:
        run_sync(args, summary)
        return 0
    except Exception as exc:
        summary["error"] = str(exc)
        print(f"Обновление остановлено: {exc}", file=sys.stderr)
        return 1
    finally:
        write_json(ROOT / "reports/weekly_update_summary.json", summary)
        lines = ["## Обновление результатов Compreg", f"Статус: {summary['status']}", f"Обновлено протоколов: {summary['updated_protocols']}"]
        for item in summary["tournaments"]:
            title = item["title"].replace("\n", " ")
            lines.append(f"- {title}: проверено {item['checked']}, обновлено {item['updated']}; наши IDD: {', '.join(item['dancers']) or 'нет'}")
        if summary["review"]:
            lines.append(f"Совпадения только по имени для ручной проверки: {len(summary['review'])}")
            for review in summary["review"]:
                lines.append(f"- {review['url']}: {', '.join(review['names'])}")
        if summary.get("error"):
            lines.append("Ошибка: " + summary["error"])
        if os.environ.get("GITHUB_STEP_SUMMARY"):
            with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as f:
                f.write("\n".join(lines) + "\n")


if __name__ == "__main__":
    raise SystemExit(main())
