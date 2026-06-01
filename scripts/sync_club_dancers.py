#!/usr/bin/env python3
"""Sync a club dancer list from Compreg dancers search."""

from __future__ import annotations

import argparse
import csv
import json
import re
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

import requests
from bs4 import BeautifulSoup, Tag

from compreg_encoding import write_compreg_html_file


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB_PATH = PROJECT_ROOT / "database" / "compreg_spb_2025_2026.sqlite"
DEFAULT_OUTPUT_PATH = PROJECT_ROOT / "data" / "club_triumph_spb_dancers.csv"
CACHE_DIR = PROJECT_ROOT / "data" / "cache" / "club_dancers"
DEBUG_DIR = PROJECT_ROOT / "data" / "debug"
BASE_URL = "https://compreg.ru/dancerscomp"
REQUEST_TIMEOUT = (8, 25)
USER_AGENT = "DanceAnalyticsClubSync/1.0"
CSV_FIELDS = [
    "idd",
    "name",
    "club",
    "city",
    "class_st",
    "class_la",
    "source_url",
    "fetched_at",
    "internal_dancer_id",
    "protocols_count",
    "has_report",
]


@dataclass(frozen=True)
class SearchRequest:
    method: str
    url: str
    payload: tuple[tuple[str, str], ...]
    source: str


@dataclass(frozen=True)
class FetchResult:
    html: str
    final_url: str
    status_code: int
    content_type: str
    repr_head: str
    cache_path: Path


def clean_text(value: Any) -> str:
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value).replace("\xa0", " ")).strip()


def normalize_text(value: str) -> str:
    return clean_text(value).casefold().replace("ё", "е")


def absolute_url(url: str, base: str = BASE_URL) -> str:
    return urljoin(base, url)


def cache_path_for(name: str) -> Path:
    safe = re.sub(r"[^0-9A-Za-zА-Яа-я_.-]+", "_", name).strip("_") or "page"
    return CACHE_DIR / f"{safe[:160]}.html"


def fetch(session: requests.Session, method: str, url: str, payload: dict[str, str] | None, cache_name: str, debug_path: Path | None = None) -> FetchResult:
    response = session.request(method, url, params=payload if method == "GET" else None, data=payload if method == "POST" else None, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    cache_path = cache_path_for(cache_name)
    html = write_compreg_html_file(cache_path, response.content, declared_encoding=response.encoding)
    if debug_path:
        debug_path.parent.mkdir(parents=True, exist_ok=True)
        debug_path.write_text(html, encoding="utf-8")
    return FetchResult(
        html=html,
        final_url=response.url,
        status_code=response.status_code,
        content_type=response.headers.get("content-type", ""),
        repr_head=repr(html[:500]),
        cache_path=cache_path,
    )


def form_label(form: Tag, field: Tag) -> str:
    chunks = [
        clean_text(field.get("name")),
        clean_text(field.get("id")),
        clean_text(field.get("placeholder")),
        clean_text(field.get("title")),
        clean_text(field.get("aria-label")),
    ]
    field_id = clean_text(field.get("id"))
    if field_id:
        label = form.select_one(f'label[for="{field_id}"]')
        if label:
            chunks.append(clean_text(label.get_text(" ", strip=True)))
    parent = field.find_parent(["label", "td", "div", "p"])
    if parent:
        chunks.append(clean_text(parent.get_text(" ", strip=True)))
    return " ".join(item for item in chunks if item)


def choose_field_value(label: str, club: str, city: str) -> str | None:
    text = normalize_text(label)
    if any(token in text for token in ["клуб", "club", "организац", "коллектив"]):
        return club
    if any(token in text for token in ["город", "city", "регион", "region", "субъект"]):
        return city
    if any(token in text for token in ["поиск", "search", "filter", "фильтр", "query", "q"]):
        return club
    return None


def form_requests(soup: BeautifulSoup, club: str, city: str) -> list[SearchRequest]:
    requests_to_try: list[SearchRequest] = []
    for index, form in enumerate(soup.find_all("form"), start=1):
        method = clean_text(form.get("method") or "GET").upper()
        if method not in {"GET", "POST"}:
            method = "GET"
        url = absolute_url(clean_text(form.get("action")) or BASE_URL)
        payload: dict[str, str] = {}
        text_inputs = []
        for field in form.find_all(["input", "select", "textarea"]):
            name = clean_text(field.get("name"))
            if not name:
                continue
            field_type = clean_text(field.get("type") or "").lower()
            if field_type in {"submit", "button", "image", "reset", "file"}:
                continue
            value = clean_text(field.get("value"))
            label = form_label(form, field)
            chosen = choose_field_value(label, club, city)
            if chosen is not None:
                payload[name] = chosen
            elif field.name == "select":
                selected = field.select_one("option[selected]")
                if selected:
                    payload[name] = clean_text(selected.get("value"))
                elif value:
                    payload[name] = value
            elif field_type in {"hidden", "checkbox", "radio"}:
                payload[name] = value
            elif value:
                payload[name] = value
            else:
                text_inputs.append(name)

        if club not in payload.values() and text_inputs:
            payload[text_inputs[0]] = club
        if payload:
            requests_to_try.append(SearchRequest(method, url, tuple(sorted(payload.items())), f"form_{index}"))
    return requests_to_try


def official_form_club_request(club: str) -> SearchRequest:
    return SearchRequest(
        "POST",
        BASE_URL,
        (
            ("c", club),
            ("idd", ""),
            ("n", ""),
            ("o", ""),
            ("p", ""),
        ),
        "official_form_club_post",
    )


def debug_forms(soup: BeautifulSoup) -> list[dict[str, Any]]:
    forms = []
    for index, form in enumerate(soup.find_all("form"), start=1):
        fields = []
        for field in form.find_all(["input", "select", "textarea"]):
            options = []
            if field.name == "select":
                options = [
                    {
                        "value": clean_text(option.get("value")),
                        "text": clean_text(option.get_text(" ", strip=True)),
                        "selected": option.has_attr("selected"),
                    }
                    for option in field.find_all("option")[:20]
                ]
            fields.append(
                {
                    "tag": field.name,
                    "type": clean_text(field.get("type")),
                    "name": clean_text(field.get("name")),
                    "id": clean_text(field.get("id")),
                    "value": clean_text(field.get("value")),
                    "label": form_label(form, field),
                    "options": options,
                }
            )
        forms.append(
            {
                "index": index,
                "method": clean_text(form.get("method") or "GET").upper(),
                "action": absolute_url(clean_text(form.get("action")) or BASE_URL),
                "fields": fields,
            }
        )
    return forms


def debug_tables(html: str) -> list[dict[str, Any]]:
    soup = BeautifulSoup(html, "html.parser")
    tables = []
    for index, table in enumerate(soup.find_all("table"), start=1):
        row_samples = []
        for tr in table.find_all("tr")[:4]:
            row_samples.append([clean_text(cell.get_text(" ", strip=True)) for cell in tr.find_all(["th", "td"])])
        tables.append(
            {
                "index": index,
                "headers": parse_headers(table),
                "first_rows": row_samples[1:4] if row_samples else [],
            }
        )
    return tables


def print_debug_response(label: str, result: FetchResult, html: str, club: str) -> None:
    soup = BeautifulSoup(html, "html.parser")
    forms = debug_forms(soup)
    tables = debug_tables(html)
    danceinfo_links = [absolute_url(link.get("href"), result.final_url) for link in soup.find_all("a", href=True) if "danceinfo.php" in clean_text(link.get("href"))]
    hidden_ci = [clean_text(field.get("value")) for field in soup.find_all("input", {"name": "ci"}) if clean_text(field.get("value"))]
    triumph_nodes = []
    for node in soup.find_all(string=lambda text: text and club.casefold() in text.casefold()):
        parent = node.find_parent(["tr", "li", "div", "p", "td", "span", "form"])
        if parent:
            triumph_nodes.append(clean_text(parent.get_text(" ", strip=True)))
        else:
            triumph_nodes.append(clean_text(node))
    print(f"\nDEBUG {label}")
    print(f"status_code: {result.status_code}")
    print(f"final_url: {result.final_url}")
    print(f"content_type: {result.content_type}")
    print(f"cache_path: {result.cache_path}")
    print(f"contains club {club!r}: {club.casefold() in html.casefold()}")
    print(f"danceinfo links: {len(danceinfo_links)}")
    if danceinfo_links:
        print(f"  first danceinfo links: {danceinfo_links[:10]}")
    print(f"hidden ci inputs: {len(hidden_ci)}")
    if hidden_ci:
        print(f"  first ci values: {hidden_ci[:20]}")
    print(f"text blocks with {club!r}: {len(triumph_nodes)}")
    for text in triumph_nodes[:20]:
        print(f"  block: {text[:500]}")
    ref_rows = soup.select(".dancers-table .ref-tr")
    print(f"compreg dancer rows (.dancers-table .ref-tr): {len(ref_rows)}")
    for row in ref_rows[:5]:
        print(f"  ref-tr text: {row.get_text(' | ', strip=True)[:500]}")
        print(f"  ref-tr html: {str(row)[:1000]}")
    print(f"response head repr: {result.repr_head}")
    print(f"forms found: {len(forms)}")
    for form in forms:
        print(f"  form #{form['index']}: method={form['method']} action={form['action']}")
        for field in form["fields"]:
            print(
                "    "
                f"{field['tag']} type={field['type']!r} name={field['name']!r} "
                f"id={field['id']!r} value={field['value']!r} label={field['label']!r}"
            )
            if field["options"]:
                print(f"      options: {field['options'][:5]}")
    print(f"tables found: {len(tables)}")
    for table in tables:
        print(f"  table #{table['index']} headers={table['headers']}")
        for row in table["first_rows"]:
            print(f"    row={row}")


def common_requests(club: str, city: str) -> list[SearchRequest]:
    variants = [
        {"club": club},
        {"query": club},
        {"q": club},
        {"search": club},
        {"filter": club},
        {"clubname": club},
        {"org": club},
        {"club": club, "city": city},
        {"club": club, "town": city},
        {"clubname": club, "city": city},
        {"org": club, "city": city},
        {"query": club, "city": city},
        {"q": club, "city": city},
        {"search": club, "city": city},
        {"filter": club, "city": city},
    ]
    return [SearchRequest("GET", BASE_URL, tuple(sorted(item.items())), f"common_{index}") for index, item in enumerate(variants, start=1)]


def unique_requests(items: list[SearchRequest]) -> list[SearchRequest]:
    seen = set()
    result = []
    for item in items:
        key = (item.method, item.url, item.payload)
        if key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result


def request_url(item: SearchRequest) -> str:
    if item.method == "POST" or not item.payload:
        return item.url
    parts = list(urlparse(item.url))
    query = dict(parse_qsl(parts[4], keep_blank_values=True))
    query.update(dict(item.payload))
    parts[4] = urlencode(query, doseq=True)
    return urlunparse(parts)


def parse_headers(table: Tag) -> list[str]:
    header_row = table.find("tr")
    if not header_row:
        return []
    cells = header_row.find_all(["th", "td"])
    return [normalize_text(cell.get_text(" ", strip=True)) for cell in cells]


def classify_column(header: str, index: int) -> str | None:
    if "idd" in header or "id" == header:
        return "idd"
    if any(token in header for token in ["фам", "пара", "танц", "спорт", "имя"]):
        return "name"
    if "стандарт" in header or header in {"st", "std"}:
        return "class_st"
    if "латин" in header or header in {"la"}:
        return "class_la"
    if "клуб" in header or "club" in header:
        return "club"
    if "город" in header or "city" in header:
        return "city"
    if index == 0 and not header:
        return None
    return None


def extract_idd(text: str, links: list[str]) -> str:
    for value in [text, *links]:
        match = re.search(r"(?:idd|ci|id)=?(\d{5,8})", value, flags=re.IGNORECASE)
        if match:
            return match.group(1)
    match = re.search(r"\b20\d{5}\b", text)
    return match.group(0) if match else ""


def row_links(row: Tag, base_url: str) -> list[str]:
    return [absolute_url(link.get("href"), base_url) for link in row.find_all("a", href=True)]


def split_club_city(value: str, club: str) -> tuple[str, str]:
    text = clean_text(value)
    if "*" in text:
        left, right = text.split("*", 1)
        return clean_text(left), clean_text(right)
    if "," in text:
        left, right = text.rsplit(",", 1)
        return clean_text(left), clean_text(right)
    if club.casefold() in text.casefold():
        city = clean_text(re.sub(re.escape(club), "", text, flags=re.IGNORECASE))
        return club, city
    return text, ""


def parse_compreg_dancers_table(soup: BeautifulSoup, source_url: str, fetched_at: str, club: str) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for block in soup.select(".dancers-table .ref-tr"):
        name_node = block.select_one(".dancers-td-name")
        club_node = block.select_one(".dancers-td-club")
        if not name_node or not club_node:
            continue
        name = clean_text(name_node.get_text(" ", strip=True))
        club_value, city = split_club_city(club_node.get_text(" ", strip=True), club)
        if not name or name == "*" or club.casefold() not in club_value.casefold():
            continue
        idd = extract_idd(clean_text(block.get_text(" ", strip=True)), row_links(block, source_url))
        onclick = clean_text(name_node.get("onclick"))
        if not idd:
            idd = extract_idd(onclick, [])
        item = {field: "" for field in CSV_FIELDS}
        item.update(
            {
                "idd": idd,
                "name": name,
                "club": club_value,
                "city": city,
                "source_url": source_url,
                "fetched_at": fetched_at,
            }
        )
        rows.append(item)
    return rows


def row_from_text_block(text: str, source_url: str, fetched_at: str, club: str) -> dict[str, str] | None:
    if club.casefold() not in text.casefold():
        return None
    parts = [clean_text(part) for part in re.split(r"\s{2,}|\s+\|\s+|\t+", text) if clean_text(part)]
    idd = extract_idd(text, [])
    class_st = ""
    class_la = ""
    class_match = re.search(r"(?:Стандарт|St)\s*[:\-]?\s*([A-ZА-Я0-9+]+)", text, flags=re.IGNORECASE)
    if class_match:
        class_st = clean_text(class_match.group(1))
    class_match = re.search(r"(?:Латина|La)\s*[:\-]?\s*([A-ZА-Я0-9+]+)", text, flags=re.IGNORECASE)
    if class_match:
        class_la = clean_text(class_match.group(1))
    city = "Санкт-Петербург" if "санкт" in text.casefold() and "петербург" in text.casefold() else ""

    name = ""
    for part in parts:
        if part == club or idd and part == idd or "санкт" in part.casefold() or club.casefold() in part.casefold():
            continue
        if re.search(r"[А-Яа-яЁё]", part) and not any(token in part.casefold() for token in ["стандарт", "латина", "класс", "город", "клуб"]):
            name = part
            break
    if not name:
        compact = re.sub(r"\b20\d{5}\b", " ", text)
        compact = compact.replace(club, " ")
        compact = re.sub(r"\b(?:Санкт-Петербург|Москва|St|La|Стандарт|Латина)\b", " ", compact, flags=re.IGNORECASE)
        name = clean_text(compact)
    if not name or name == "*" or name.startswith("* "):
        return None
    row = {field: "" for field in CSV_FIELDS}
    row.update(
        {
            "idd": idd,
            "name": name,
            "club": club,
            "city": city,
            "class_st": class_st,
            "class_la": class_la,
            "source_url": source_url,
            "fetched_at": fetched_at,
        }
    )
    return row


def parse_non_table_dancer_rows(soup: BeautifulSoup, source_url: str, fetched_at: str, club: str) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    seen_blocks: set[str] = set()
    candidates: list[Tag] = []
    for link in soup.find_all("a", href=True):
        href = clean_text(link.get("href"))
        if "danceinfo.php" in href:
            parent = link.find_parent(["tr", "li", "div", "p"]) or link
            candidates.append(parent)
    for field in soup.find_all("input", {"name": "ci"}):
        parent = field.find_parent(["tr", "li", "div", "p", "form"]) or field
        candidates.append(parent)
    for node in soup.find_all(string=lambda text: text and club.casefold() in text.casefold()):
        parent = node.find_parent(["tr", "li", "div", "p", "td", "span"]) or node.parent
        if parent:
            candidates.append(parent)

    for block in candidates:
        text = clean_text(block.get_text(" ", strip=True))
        if not text or text in seen_blocks:
            continue
        seen_blocks.add(text)
        links = row_links(block, source_url) if isinstance(block, Tag) else []
        hidden_ci = ""
        if isinstance(block, Tag):
            ci = block.find("input", {"name": "ci"})
            hidden_ci = clean_text(ci.get("value")) if ci else ""
        row = row_from_text_block(text, source_url, fetched_at, club)
        if row:
            row["idd"] = row["idd"] or hidden_ci or extract_idd(text, links)
            rows.append(row)
    return rows


def parse_dancer_rows(html: str, source_url: str, fetched_at: str, club: str, city: str) -> list[dict[str, str]]:
    soup = BeautifulSoup(html, "html.parser")
    rows: list[dict[str, str]] = []
    rows.extend(parse_compreg_dancers_table(soup, source_url, fetched_at, club))
    for table in soup.find_all("table"):
        headers = parse_headers(table)
        if not headers:
            continue
        mapped = [classify_column(header, index) for index, header in enumerate(headers)]
        if not any(value in {"idd", "name", "club"} for value in mapped):
            continue
        for tr in table.find_all("tr")[1:]:
            cells = tr.find_all(["td", "th"])
            if len(cells) < 2:
                continue
            item = {field: "" for field in CSV_FIELDS}
            text = clean_text(tr.get_text(" ", strip=True))
            links = row_links(tr, source_url)
            item["source_url"] = source_url
            item["fetched_at"] = fetched_at
            item["idd"] = extract_idd(text, links)
            for index, cell in enumerate(cells):
                field = mapped[index] if index < len(mapped) else None
                if not field:
                    continue
                value = clean_text(cell.get_text(" ", strip=True))
                if field == "idd":
                    item["idd"] = extract_idd(value, links) or value
                elif field in item and value:
                    item[field] = value
            if not item["club"] and club.casefold() in text.casefold():
                item["club"] = club
            if not item["city"] and city.casefold() in text.casefold():
                item["city"] = city
            if item["name"] and club.casefold() in item["club"].casefold():
                rows.append(item)
    if not rows:
        rows.extend(parse_non_table_dancer_rows(soup, source_url, fetched_at, club))
    return rows


def page_links(html: str, base_url: str) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    result = []
    for link in soup.find_all("a", href=True):
        text = normalize_text(link.get_text(" ", strip=True))
        href = absolute_url(link.get("href"), base_url)
        if any(token in text for token in ["след", "next", ">", "»"]) or re.search(r"[?&](page|p|start|offset)=", href):
            result.append(href)
    return result


def sqlite_enrichment(db_path: Path) -> dict[str, dict[str, str]]:
    if not db_path.exists():
        return {}
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        by_idd: dict[str, dict[str, str]] = {}
        for row in conn.execute(
            """
            SELECT d.id, d.external_ref, d.name, COUNT(DISTINCT pd.protocol_id) AS protocols_count
            FROM dancers d
            LEFT JOIN protocol_dancers pd ON pd.dancer_id = d.id
            WHERE d.external_ref IS NOT NULL AND TRIM(d.external_ref) != ''
            GROUP BY d.id, d.external_ref, d.name
            """
        ):
            idd = clean_text(row["external_ref"])
            by_idd[idd] = {
                "internal_dancer_id": clean_text(row["id"]),
                "protocols_count": clean_text(row["protocols_count"]),
                "has_report": "yes" if (PROJECT_ROOT / "reports" / f"dancer_{idd}_report.json").exists() else "no",
            }
        return by_idd
    finally:
        conn.close()


def merge_rows(rows: list[dict[str, str]], city: str) -> list[dict[str, str]]:
    merged: dict[tuple[str, str, str], dict[str, str]] = {}
    for row in rows:
        if city and row.get("city") and city.casefold() not in row["city"].casefold():
            continue
        key = (row.get("idd") or "", normalize_text(row.get("name", "")), normalize_text(row.get("club", "")))
        if key not in merged:
            merged[key] = dict(row)
            continue
        target = merged[key]
        for field in CSV_FIELDS:
            if not target.get(field) and row.get(field):
                target[field] = row[field]
    return sorted(merged.values(), key=lambda item: (normalize_text(item.get("name", "")), item.get("idd", "")))


def apply_enrichment(rows: list[dict[str, str]], db_path: Path) -> None:
    enrichment = sqlite_enrichment(db_path)
    for row in rows:
        info = enrichment.get(row.get("idd", ""), {})
        row["internal_dancer_id"] = info.get("internal_dancer_id", "")
        row["protocols_count"] = info.get("protocols_count", "")
        row["has_report"] = info.get("has_report", "no" if row.get("idd") else "")


def sync_club(club: str, city: str, output_path: Path, db_path: Path, max_pages: int, debug: bool) -> list[dict[str, str]]:
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})
    fetched_at = datetime.now(timezone.utc).isoformat()

    initial_result = fetch(
        session,
        "GET",
        BASE_URL,
        None,
        "dancerscomp_initial",
        DEBUG_DIR / "dancerscomp_initial.html" if debug else None,
    )
    initial_html = initial_result.html
    soup = BeautifulSoup(initial_html, "html.parser")
    if debug:
        print_debug_response("initial", initial_result, initial_html, club)
    requests_to_try = unique_requests([official_form_club_request(club)] + form_requests(soup, club, "") + common_requests(club, city))
    if not requests_to_try:
        requests_to_try = common_requests(club, city)

    all_rows: list[dict[str, str]] = []
    visited_pages: set[str] = set()
    debug_search_written = False
    for index, item in enumerate(requests_to_try, start=1):
        payload = dict(item.payload)
        if debug:
            print(f"\nTrying search #{index}: source={item.source} method={item.method} url={item.url} payload={json.dumps(payload, ensure_ascii=False)}")
        try:
            result = fetch(
                session,
                item.method,
                item.url,
                payload,
                f"dancerscomp_{item.source}_{index}",
                DEBUG_DIR / "dancerscomp_search_response.html" if debug and not debug_search_written else None,
            )
            debug_search_written = True
        except requests.RequestException:
            continue
        html = result.html
        final_url = result.final_url
        rows = parse_dancer_rows(html, final_url, fetched_at, club, "")
        if debug:
            print_debug_response(f"search #{index}", result, html, club)
            print(f"rows parsed before city filter: {len(rows)}")
        if rows:
            all_rows.extend(rows)
            queue = page_links(html, final_url)
            while queue and len(visited_pages) < max_pages:
                page_url = queue.pop(0)
                if page_url in visited_pages:
                    continue
                visited_pages.add(page_url)
                try:
                    page_result = fetch(session, "GET", page_url, None, f"dancerscomp_page_{len(visited_pages)}")
                except requests.RequestException:
                    continue
                page_html = page_result.html
                page_final_url = page_result.final_url
                all_rows.extend(parse_dancer_rows(page_html, page_final_url, fetched_at, club, ""))
                queue.extend(link for link in page_links(page_html, page_final_url) if link not in visited_pages)
            break

    rows = merge_rows(all_rows, city)
    apply_enrichment(rows, db_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8", newline="") as file_obj:
        writer = csv.DictWriter(file_obj, fieldnames=CSV_FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    return rows


def print_preview(rows: list[dict[str, str]], output_path: Path) -> None:
    existing = [row for row in rows if row.get("internal_dancer_id")]
    print(f"Compreg dancers found: {len(rows)}")
    print(f"Already in SQLite: {len(existing)}")
    print(f"New dancers: {len(rows) - len(existing)}")
    print(f"CSV path: {output_path}")
    print()
    writer = csv.DictWriter(sys.stdout, fieldnames=CSV_FIELDS)
    writer.writeheader()
    writer.writerows(rows[:30])


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Sync a full club dancer list from Compreg dancers search.")
    parser.add_argument("--club", default="Триумф")
    parser.add_argument("--city", default="Санкт-Петербург")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT_PATH)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--max-pages", type=int, default=30)
    parser.add_argument("--debug", action="store_true", help="Print forms/tables and save debug HTML responses.")
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    try:
        rows = sync_club(args.club, args.city, args.output, args.db, args.max_pages, args.debug)
    except requests.RequestException as exc:
        print(f"Failed to fetch Compreg dancers search: {exc}", file=sys.stderr)
        return 2
    print_preview(rows, args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
