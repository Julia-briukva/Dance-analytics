#!/usr/bin/env python3
"""Inspect Compreg dancer endpoints for one external IDD.

Diagnostic-only: downloads candidate pages, stores raw HTML in cache, and
prints extractable profile/result signals. It does not touch SQLite or reports.
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from compreg_encoding import write_compreg_html_file


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CACHE_DIR = PROJECT_ROOT / "data" / "cache" / "dancer_profiles"
REQUEST_TIMEOUT = (6, 15)
USER_AGENT = "DanceAnalyticsDiagnostics/1.0"

ENDPOINTS = [
    "https://compreg.ru/danceinfo.php?idd={idd}",
    "https://compreg.ru/danceinfo.php?id={idd}",
    "https://compreg.ru/danceinfo.php?ID={idd}",
    "https://compreg.ru/danceresults.php?idd={idd}",
    "https://compreg.ru/dancer_resultsp.php?idd={idd}",
]

POST_ENDPOINTS = [
    ("https://compreg.ru/dancer_resultsp.php", {"ci": "{idd}"}),
    ("https://compreg.ru/danceinfo.php", {"ci": "{idd}"}),
    ("https://compreg.ru/dancer_resultsp.php", {"ci": "{idd}", "tab": "stp", "query": ""}),
    ("https://compreg.ru/danceinfo.php", {"ci": "{idd}", "tab": "stp", "query": ""}),
]

FIELD_PATTERNS = {
    "name": [
        r"(?:ФИО|Фамилия\s+Имя|Участник|Танцор(?:ка)?|Спортсмен(?:ка)?)[\s:.-]+([А-ЯЁ][А-ЯЁа-яё -]{3,80})",
    ],
    "club": [
        r"(?:Клуб|СТК|ТСК)[\s:.-]+([^,\n\r\t]{2,120})",
    ],
    "city": [
        r"(?:Город|Регион|Страна/город|Город/Страна)[\s:.-]+([А-ЯЁA-Z][^,\n\r\t]{2,80})",
    ],
    "idd": [
        r"(?:IDD|ID|Номер)[\s:.-]+(\d{4,12})",
    ],
}


@dataclass(frozen=True)
class EndpointResult:
    method: str
    url: str
    payload: dict[str, str] | None
    status_code: int | None
    cache_path: Path | None
    error: str | None
    title: str | None
    fields: dict[str, list[str]]
    result_links: list[str]
    protocol_links: list[str]
    text_length: int
    text_preview: str


def cache_path_for(url: str, idd: str, method: str, payload: dict[str, str] | None = None) -> Path:
    parsed = urlparse(url)
    endpoint = Path(parsed.path).stem or "endpoint"
    query_key = re.sub(r"[^a-zA-Z0-9]+", "_", parsed.query).strip("_")
    if method.upper() == "POST":
        payload_key = "_".join(f"{key}_{value}" for key, value in (payload or {}).items())
        query_key = f"post_{re.sub(r'[^a-zA-Z0-9]+', '_', payload_key).strip('_')}"
    if not query_key:
        query_key = "no_query"
    return CACHE_DIR / f"{idd}_{endpoint}_{query_key}.html"


def normalize_space(value: str | None) -> str:
    if not value:
        return ""
    return re.sub(r"\s+", " ", value.replace("\xa0", " ")).strip()


def unique(values: list[str], limit: int = 10) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        clean = normalize_space(value)
        if not clean or clean in seen:
            continue
        seen.add(clean)
        result.append(clean)
        if len(result) >= limit:
            break
    return result


def page_title(soup: BeautifulSoup) -> str | None:
    candidates = [
        soup.title.get_text(" ", strip=True) if soup.title else None,
        *(node.get_text(" ", strip=True) for node in soup.select("h1, h2, .prot-caption, .caption")[:5]),
    ]
    return next((normalize_space(item) for item in candidates if normalize_space(item)), None)


def extract_fields(text: str, idd: str) -> dict[str, list[str]]:
    fields: dict[str, list[str]] = {}
    for field, patterns in FIELD_PATTERNS.items():
        matches: list[str] = []
        for pattern in patterns:
            matches.extend(re.findall(pattern, text, flags=re.I))
        fields[field] = unique(matches)
    if idd in text and idd not in fields.get("idd", []):
        fields.setdefault("idd", []).insert(0, idd)
    return fields


def extract_links(soup: BeautifulSoup, base_url: str) -> tuple[list[str], list[str]]:
    result_links: list[str] = []
    protocol_links: list[str] = []
    for node in soup.find_all("a", href=True):
        url = urljoin(base_url, node["href"])
        low = url.lower()
        if any(token in low for token in ["result", "dancer", "danceinfo"]):
            result_links.append(url)
        if "resultsdata" in low or re.search(r"/\d{4}/\d{2}/\d{4}/\d+\.php", low):
            protocol_links.append(url)
    return unique(result_links, limit=20), unique(protocol_links, limit=20)


def parse_response(
    response: requests.Response,
    url: str,
    idd: str,
    method: str,
    payload: dict[str, str] | None,
    cache_path: Path,
) -> EndpointResult:
    html = write_compreg_html_file(cache_path, response.content, declared_encoding=response.encoding)

    soup = BeautifulSoup(html, "html.parser")
    text = normalize_space(soup.get_text("\n", strip=True))
    result_links, protocol_links = extract_links(soup, url)
    return EndpointResult(
        method=method,
        url=url,
        payload=payload,
        status_code=response.status_code,
        cache_path=cache_path,
        error=None,
        title=page_title(soup),
        fields=extract_fields(text, idd),
        result_links=result_links,
        protocol_links=protocol_links,
        text_length=len(text),
        text_preview=text[:500],
    )


def inspect_get_endpoint(session: requests.Session, url: str, idd: str) -> EndpointResult:
    cache_path = cache_path_for(url, idd, "GET")
    try:
        response = session.get(url, timeout=REQUEST_TIMEOUT)
    except requests.RequestException as exc:
        return EndpointResult("GET", url, None, None, None, str(exc), None, {}, [], [], 0, "")
    return parse_response(response, url, idd, "GET", None, cache_path)


def inspect_post_endpoint(session: requests.Session, url: str, payload_template: dict[str, str], idd: str) -> EndpointResult:
    payload = {key: value.format(idd=idd) for key, value in payload_template.items()}
    cache_path = cache_path_for(url, idd, "POST", payload)
    try:
        response = session.post(url, data=payload, timeout=REQUEST_TIMEOUT)
    except requests.RequestException as exc:
        return EndpointResult("POST", url, payload, None, None, str(exc), None, {}, [], [], 0, "")
    return parse_response(response, url, idd, "POST", payload, cache_path)


def is_working(result: EndpointResult) -> bool:
    if result.status_code != 200 or result.error:
        return False
    if result.text_length < 200:
        return False
    return bool(result.fields.get("idd") or result.fields.get("name") or result.result_links or result.protocol_links)


def print_result(result: EndpointResult) -> None:
    print(f"\n{result.method}: {result.url}")
    if result.payload is not None:
        print(f"  Body: {result.payload}")
    print(f"  HTTP status: {result.status_code if result.status_code is not None else '-'}")
    if result.error:
        print(f"  Error: {result.error}")
        return
    print(f"  Cache: {result.cache_path.relative_to(PROJECT_ROOT) if result.cache_path else '-'}")
    print(f"  Title: {result.title or '-'}")
    print(f"  Text length: {result.text_length}")
    for field in ["name", "club", "city", "idd"]:
        values = result.fields.get(field) or []
        print(f"  {field}: {', '.join(values) if values else '-'}")
    print(f"  Result links: {len(result.result_links)}")
    for link in result.result_links[:5]:
        print(f"    - {link}")
    print(f"  Protocol/result URLs: {len(result.protocol_links)}")
    for link in result.protocol_links[:5]:
        print(f"    - {link}")
    if not result.fields.get("name") and result.text_preview:
        print(f"  Clean text preview: {result.text_preview}")


def print_summary(results: list[EndpointResult]) -> None:
    working = [item for item in results if is_working(item)]
    print("\nSummary")
    print(f"  Working endpoints: {len(working)}")
    for item in working:
        extracted = [key for key, values in item.fields.items() if values]
        if item.result_links:
            extracted.append("result_links")
        if item.protocol_links:
            extracted.append("protocol_urls")
        label = item.url if item.payload is None else f"{item.url} body={item.payload}"
        print(f"  - {item.method} {label}: {', '.join(extracted) if extracted else 'no structured fields'}")

    best = max(
        results,
        key=lambda item: (
            is_working(item),
            len([values for values in item.fields.values() if values]),
            len(item.result_links),
            len(item.protocol_links),
            item.text_length,
        ),
        default=None,
    )
    if best and is_working(best):
        best_label = best.url if best.payload is None else f"{best.url} body={best.payload}"
    else:
        best_label = "-"
    print(f"  Best endpoint: {best_label}")

    has_identity = any(result.fields.get("name") or result.fields.get("idd") for result in working)
    has_context = any(result.fields.get("club") or result.fields.get("city") or result.protocol_links for result in working)
    enough = has_identity and has_context
    print(f"  Enough data for future local matching: {'yes' if enough else 'no'}")


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Inspect Compreg endpoints for one dancer IDD.")
    parser.add_argument("--idd", required=True, help="External Compreg dancer IDD.")
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    idd = str(args.idd).strip()
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})

    results = [inspect_get_endpoint(session, endpoint.format(idd=idd), idd) for endpoint in ENDPOINTS]
    results.extend(inspect_post_endpoint(session, endpoint, payload, idd) for endpoint, payload in POST_ENDPOINTS)
    print(f"Compreg IDD inspection: {idd}")
    for result in results:
        print_result(result)
    print_summary(results)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
