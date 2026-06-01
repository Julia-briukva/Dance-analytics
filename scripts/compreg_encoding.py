"""Encoding helpers for Compreg HTML pages."""

from __future__ import annotations

import re
from pathlib import Path


META_CHARSET_RE = re.compile(
    br"<meta[^>]+charset=[\"']?\s*([a-zA-Z0-9_\-]+)",
    flags=re.IGNORECASE,
)
MOJIBAKE_MARKERS = "–—ҐЄЉЇљї°∞µ±≥"
CYRILLIC_RE = re.compile(r"[А-Яа-яЁё]")


def _html_declared_charset(raw_bytes: bytes) -> str | None:
    head = raw_bytes[:4096]
    match = META_CHARSET_RE.search(head)
    if not match:
        return None
    return match.group(1).decode("ascii", errors="ignore").lower()


def _score_text(text: str) -> int:
    cyrillic = len(CYRILLIC_RE.findall(text))
    mojibake = sum(text.count(char) for char in MOJIBAKE_MARKERS)
    replacement = text.count("\ufffd")
    return cyrillic * 4 - mojibake * 3 - replacement * 5


def repair_compreg_mojibake(text: str) -> str:
    """Repair UTF-8 Russian text that was decoded as Mac Cyrillic."""
    if not text:
        return text
    marker_count = sum(text.count(char) for char in MOJIBAKE_MARKERS)
    if marker_count < 3:
        return text
    try:
        repaired = text.encode("mac_cyrillic").decode("utf-8")
    except UnicodeError:
        return text
    return repaired if _score_text(repaired) > _score_text(text) else text


def decode_compreg_html(raw_bytes: bytes, declared_encoding: str | None = None) -> str:
    """Decode Compreg HTML bytes using stable charset detection.

    Compreg pages can be served with unreliable HTTP charset metadata. Prefer
    an explicit/meta charset, then choose the decoded text with the best
    Cyrillic score. Existing cache files may already contain mojibake, so the
    decoded text is also passed through the Mac Cyrillic repair step.
    """
    if not raw_bytes:
        return ""

    candidates: list[str] = []
    for value in [declared_encoding, _html_declared_charset(raw_bytes), "utf-8", "cp1251", "windows-1251", "mac_cyrillic"]:
        if value and value.lower() not in candidates:
            candidates.append(value.lower())

    decoded: list[str] = []
    for encoding in candidates:
        try:
            decoded.append(raw_bytes.decode(encoding))
        except UnicodeError:
            decoded.append(raw_bytes.decode(encoding, errors="replace"))

    repaired = [repair_compreg_mojibake(text) for text in decoded]
    return max(repaired, key=_score_text)


def read_compreg_html_file(path: Path) -> str:
    return decode_compreg_html(path.read_bytes())


def write_compreg_html_file(path: Path, raw_bytes: bytes, declared_encoding: str | None = None) -> str:
    html = decode_compreg_html(raw_bytes, declared_encoding=declared_encoding)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(html, encoding="utf-8")
    return html
