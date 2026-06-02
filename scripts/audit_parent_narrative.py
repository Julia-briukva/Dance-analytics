#!/usr/bin/env python3
"""Audit parent-mode narrative for conflicts and overly technical output."""

from __future__ import annotations

import csv
import json
import re
from pathlib import Path
from typing import Any

from render_html_report import build_view_model


PROJECT_ROOT = Path(__file__).resolve().parents[1]
REPORTS_DIR = PROJECT_ROOT / "reports"
OUTPUT_PATH = REPORTS_DIR / "parent_mode_narrative_audit.csv"


def sentence_count(text: str) -> int:
    return len([part for part in re.split(r"[.!?]+", text) if part.strip()])


def add_issue(
    rows: list[dict[str, Any]],
    report: dict[str, Any],
    program_key: str,
    issue_type: str,
    comment: str,
    program: dict[str, Any],
) -> None:
    rows.append(
        {
            "idd": (report.get("dancer") or {}).get("idd", ""),
            "name": (report.get("dancer") or {}).get("name", ""),
            "program": program_key,
            "issue_type": issue_type,
            "strength_dances": ", ".join(program.get("parent_strength_dances") or []),
            "attention_dances": ", ".join(program.get("parent_attention_dances") or []),
            "trend_dances": ", ".join(program.get("parent_trend_dances") or []),
            "best_text_len": len(program.get("best_text") or ""),
            "attention_text_len": len(program.get("attention_text") or ""),
            "trend_text_len": len(program.get("trend_text") or ""),
            "overview_sentences": sentence_count(" ".join(program.get("overview") or [])),
            "comment": comment,
        }
    )


def audit_report(path: Path) -> list[dict[str, Any]]:
    report = json.loads(path.read_text(encoding="utf-8"))
    view_model = build_view_model(report, path, path.with_suffix(".html"))
    rows: list[dict[str, Any]] = []
    for program_key, program in (view_model.get("role_views", {}).get("parent", {}).get("programs") or {}).items():
        strength = set(program.get("parent_strength_dances") or [])
        attention = set(program.get("parent_attention_dances") or [])
        trend = set(program.get("parent_trend_dances") or [])
        limited = {item.get("dance") for item in (program.get("limited") or []) if item.get("dance")}

        if strength & attention:
            add_issue(rows, report, program_key, "strength_attention_overlap", ", ".join(sorted(strength & attention)), program)
        if strength & trend:
            add_issue(rows, report, program_key, "strength_trend_overlap", ", ".join(sorted(strength & trend)), program)
        if attention & trend:
            add_issue(rows, report, program_key, "attention_trend_overlap", ", ".join(sorted(attention & trend)), program)
        if (strength | attention | trend) & limited:
            add_issue(rows, report, program_key, "limited_in_main_narrative", ", ".join(sorted((strength | attention | trend) & limited)), program)

        for field, limit in [("best_text", 240), ("attention_text", 240), ("trend_text", 180)]:
            text = program.get(field) or ""
            if len(text) > limit:
                add_issue(rows, report, program_key, f"{field}_too_long", f"{len(text)} chars", program)

        overview = " ".join(program.get("overview") or [])
        if sentence_count(overview) > 3:
            add_issue(rows, report, program_key, "overview_too_long", f"{sentence_count(overview)} sentences", program)
        for field in ["best_text", "attention_text", "trend_text"]:
            text = program.get(field) or ""
            if text and text in overview:
                add_issue(rows, report, program_key, "overview_repeats_block_text", field, program)
    return rows


def main() -> int:
    rows: list[dict[str, Any]] = []
    for path in sorted(REPORTS_DIR.glob("dancer_*_report.json")):
        rows.extend(audit_report(path))

    fieldnames = [
        "idd",
        "name",
        "program",
        "issue_type",
        "strength_dances",
        "attention_dances",
        "trend_dances",
        "best_text_len",
        "attention_text_len",
        "trend_text_len",
        "overview_sentences",
        "comment",
    ]
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with OUTPUT_PATH.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    print(f"Reports checked: {len(list(REPORTS_DIR.glob('dancer_*_report.json')))}")
    print(f"Parent narrative issues: {len(rows)}")
    print(f"CSV: {OUTPUT_PATH.relative_to(PROJECT_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
