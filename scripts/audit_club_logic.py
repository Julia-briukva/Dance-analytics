#!/usr/bin/env python3
"""Audit club category visibility, dance comparisons, trends, and tournament summaries."""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any

from build_dancer_report import PROJECT_ROOT
from render_html_report import tournament_performance


DEFAULT_STATUS_CSV = PROJECT_ROOT / "reports" / "club_report_build_status.csv"
CATEGORY_AUDIT_CSV = PROJECT_ROOT / "reports" / "category_visibility_audit.csv"
DANCE_COMPARISON_CSV = PROJECT_ROOT / "reports" / "dance_comparison_audit.csv"
TREND_AUDIT_CSV = PROJECT_ROOT / "reports" / "trend_audit.csv"
TOURNAMENT_AUDIT_CSV = PROJECT_ROOT / "reports" / "tournament_summary_audit.csv"
CATEGORY_CHIP_VISIBILITY_CSV = PROJECT_ROOT / "reports" / "category_chip_visibility_audit.csv"
PROGRAMS = ("standard", "latin")
TOLERANCE = 0.001


def clean_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").replace("\xa0", " ")).strip()


def read_status_rows(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as file_obj:
        return [row for row in csv.DictReader(file_obj)]


def load_report(row: dict[str, str]) -> dict[str, Any] | None:
    if row.get("status") != "success" or not row.get("report_json"):
        return None
    path = PROJECT_ROOT / row["report_json"]
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def html_category_chips(row: dict[str, str]) -> dict[str, set[str]]:
    result = {program: set() for program in PROGRAMS}
    if row.get("status") != "success" or not row.get("report_html"):
        return result
    path = PROJECT_ROOT / row["report_html"]
    if not path.exists():
        return result
    html = path.read_text(encoding="utf-8", errors="replace")
    for program, key in re.findall(r'id="cat-(standard|latin)-([^"]+)"', html):
        if key != "all":
            result[program].add(key)
    return result


def write_csv(path: Path, fieldnames: list[str], rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as file_obj:
        writer = csv.DictWriter(file_obj, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in fieldnames})


def number(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def dance_values(metrics: list[dict[str, Any]], field: str = "final_avg_place") -> list[tuple[str, float]]:
    values = []
    for item in metrics:
        value = number(item.get(field))
        if value is not None:
            values.append((clean_text(item.get("dance")), value))
    return values


def values_all_equal(values: list[tuple[str, float]]) -> bool:
    if len(values) < 2:
        return False
    first = values[0][1]
    return all(abs(value - first) < TOLERANCE for _, value in values)


def audit_category_visibility(row: dict[str, str], report: dict[str, Any]) -> list[dict[str, Any]]:
    visible = html_category_chips(row)
    issues = []
    for program in PROGRAMS:
        slices = report.get("category_slices", {}).get(program, {}) or {}
        for key, payload in slices.items():
            if key == "all" or key in visible.get(program, set()):
                continue
            evidence = payload.get("evidence") or {}
            metrics = payload.get("metrics") or []
            marks_count = int(evidence.get("marks") or 0)
            results_count = int(evidence.get("results") or 0)
            dances = {
                clean_text(item.get("dance"))
                for item in metrics
                if clean_text(item.get("dance"))
            }
            if not dances:
                for item in payload.get("tournament_dance_results") or []:
                    if clean_text(item.get("dance")):
                        dances.add(clean_text(item.get("dance")))
            group = (
                "B_marks_useful_no_final_result"
                if marks_count > 0 and results_count == 0 and len(dances) > 0
                else "A_insufficient_data"
            )
            reason = (
                "hidden_in_html_with_marks"
                if marks_count > 0
                else "hidden_in_html_without_marks_or_results"
            )
            issues.append(
                {
                    "idd": row.get("idd"),
                    "name": report.get("dancer", {}).get("name") or row.get("name"),
                    "program": program,
                    "category": payload.get("label") or key,
                    "category_key": key,
                    "marks_count": marks_count,
                    "results_count": results_count,
                    "dances_count": len(dances),
                    "has_dance_metrics": bool(metrics),
                    "group": group,
                    "hidden_reason": reason,
                }
            )
    return issues


def category_chip_visibility_rows(row: dict[str, str], report: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    visible_html = html_category_chips(row)
    for program in PROGRAMS:
        for key, payload in (report.get("category_slices", {}).get(program, {}) or {}).items():
            if key == "all":
                continue
            evidence = payload.get("evidence") or {}
            visibility = payload.get("visibility") or {}
            rows.append(
                {
                    "idd": row.get("idd"),
                    "name": report.get("dancer", {}).get("name") or row.get("name"),
                    "program": program,
                    "category": payload.get("label") or key,
                    "marks_count": visibility.get("marks_count", evidence.get("marks", 0)),
                    "results_count": visibility.get("results_count", evidence.get("results", 0)),
                    "dance_metrics_count": visibility.get("dance_metrics_count", len(payload.get("metrics") or [])),
                    "tournaments_count": visibility.get("tournaments_count", evidence.get("tournaments", 0)),
                    "trainer_summaries_count": visibility.get("trainer_summaries_count", 0),
                    "trend_items_count": visibility.get("trend_items_count", len(payload.get("trends") or [])),
                    "is_visible_chip": bool(payload.get("is_visible_chip", key in visible_html.get(program, set()))),
                    "visibility_status": payload.get("visibility_status", "primary" if payload.get("is_visible_chip", True) else "hidden"),
                    "visibility_reason": payload.get("visibility_reason", ""),
                    "visible_in_html": key in visible_html.get(program, set()),
                }
            )
    return rows


def audit_dance_comparisons(row: dict[str, str], report: dict[str, Any]) -> list[dict[str, Any]]:
    issues = []
    scopes: list[tuple[str, str, str, dict[str, Any]]] = []
    for program in PROGRAMS:
        scopes.append(("program", program, "all", report.get("programs", {}).get(program, {}) or {}))
        for key, payload in (report.get("category_slices", {}).get(program, {}) or {}).items():
            scopes.append(("category_slice", program, key, payload))

    for scope_type, program, category, payload in scopes:
        metrics = payload.get("metrics") or []
        values = dance_values(metrics)
        if not values:
            continue
        display_mode = clean_text(payload.get("display_mode"))
        min_value = min(value for _, value in values)
        max_value = max(value for _, value in values)
        best = [dance for dance, value in values if abs(value - min_value) < TOLERANCE]
        worst = [dance for dance, value in values if abs(value - max_value) < TOLERANCE]
        overlap = sorted(set(best) & set(worst))
        if len(values) == 1 and display_mode != "single_dance":
            issues.append(
                {
                    "idd": row.get("idd"),
                    "name": report.get("dancer", {}).get("name") or row.get("name"),
                    "scope_type": scope_type,
                    "program": program,
                    "category": category,
                    "issue_type": "single_dance_no_comparison",
                    "best_dances": ", ".join(best),
                    "worst_dances": ", ".join(worst),
                    "values": "; ".join(f"{dance}={value:.3f}" for dance, value in values),
                    "comment": "Only one dance has a value; best/worst wording should be replaced by evaluated dance wording.",
                }
            )
        elif values_all_equal(values) and display_mode != "all_equal":
            issues.append(
                {
                    "idd": row.get("idd"),
                    "name": report.get("dancer", {}).get("name") or row.get("name"),
                    "scope_type": scope_type,
                    "program": program,
                    "category": category,
                    "issue_type": "all_values_equal",
                    "best_dances": ", ".join(best),
                    "worst_dances": ", ".join(worst),
                    "values": "; ".join(f"{dance}={value:.3f}" for dance, value in values),
                    "comment": "All dance values are equal; best/worst comparison should be hidden or tied.",
                }
            )
        elif overlap and display_mode == "best_worst":
            issues.append(
                {
                    "idd": row.get("idd"),
                    "name": report.get("dancer", {}).get("name") or row.get("name"),
                    "scope_type": scope_type,
                    "program": program,
                    "category": category,
                    "issue_type": "best_worst_overlap",
                    "best_dances": ", ".join(best),
                    "worst_dances": ", ".join(worst),
                    "values": "; ".join(f"{dance}={value:.3f}" for dance, value in values),
                    "comment": "Best and attention dances overlap; UI should use tied/single/neutral copy.",
                }
            )
    return issues


def trend_expected_status(item: dict[str, Any]) -> str:
    old_value = number(item.get("first_avg_place"))
    new_value = number(item.get("last_avg_place"))
    delta = None
    if old_value is not None and new_value is not None:
        delta = new_value - old_value
    else:
        delta = number(item.get("first_to_last_delta"))
    if delta is None:
        return "stable"
    if abs(delta) < TOLERANCE:
        return "stable"
    return "improving" if delta < 0 else "declining"


def audit_trends(row: dict[str, str], report: dict[str, Any]) -> list[dict[str, Any]]:
    issues = []
    scopes: list[tuple[str, str, str, dict[str, Any]]] = [("overall", "", "all", report.get("dances", {}) or {})]
    for program in PROGRAMS:
        for key, payload in (report.get("category_slices", {}).get(program, {}) or {}).items():
            scopes.append(("category_slice", program, key, payload))
    seen_membership: dict[tuple[str, str, str], set[str]] = {}
    for scope_type, program, category, payload in scopes:
        for trend in payload.get("trends", []) or []:
            dance = clean_text(trend.get("dance"))
            actual = clean_text(trend.get("trend_status"))
            expected = trend_expected_status(trend)
            if actual and actual != expected:
                issues.append(
                    {
                        "idd": row.get("idd"),
                        "name": report.get("dancer", {}).get("name") or row.get("name"),
                        "scope_type": scope_type,
                        "program": program or trend.get("program"),
                        "category": category,
                        "dance": dance,
                        "issue_type": "trend_status_mismatch",
                        "first_avg_place": trend.get("first_avg_place"),
                        "last_avg_place": trend.get("last_avg_place"),
                        "trend_status": actual,
                        "expected_status": expected,
                    }
                )
            if expected == "stable" and actual in {"improving", "declining"}:
                issues.append(
                    {
                        "idd": row.get("idd"),
                        "name": report.get("dancer", {}).get("name") or row.get("name"),
                        "scope_type": scope_type,
                        "program": program or trend.get("program"),
                        "category": category,
                        "dance": dance,
                        "issue_type": "stable_in_directional_group",
                        "first_avg_place": trend.get("first_avg_place"),
                        "last_avg_place": trend.get("last_avg_place"),
                        "trend_status": actual,
                        "expected_status": expected,
                    }
                )
            key = (scope_type, program or clean_text(trend.get("program")), category, dance)
            seen_membership.setdefault(key, set()).add(actual)
    for (scope_type, program, category, dance), statuses in seen_membership.items():
        if "improving" in statuses and "declining" in statuses:
            issues.append(
                {
                    "idd": row.get("idd"),
                    "name": report.get("dancer", {}).get("name") or row.get("name"),
                    "scope_type": scope_type,
                    "program": program,
                    "category": category,
                    "dance": dance,
                    "issue_type": "improving_declining_overlap",
                    "trend_status": ", ".join(sorted(statuses)),
                }
            )
    return issues


def audit_tournaments(row: dict[str, str], report: dict[str, Any]) -> list[dict[str, Any]]:
    issues = []
    perf = tournament_performance(
        report.get("tournaments", {}).get("items", []) or [],
        report.get("dances", {}).get("dynamics_by_date", []) or [],
    )
    best_keys = {(item.get("event_date"), item.get("tournament_title")) for item in perf.get("best", [])}
    hardest_keys = {(item.get("event_date"), item.get("tournament_title")) for item in perf.get("hardest", [])}
    for event_date, title in sorted(best_keys & hardest_keys):
        issues.append(
            {
                "idd": row.get("idd"),
                "name": report.get("dancer", {}).get("name") or row.get("name"),
                "issue_type": "best_hardest_tournament_overlap",
                "event_date": event_date,
                "tournament_title": title,
                "comment": "Tournament appears in both best and hardest lists.",
            }
        )

    for tournament in report.get("trainer_mode", {}).get("tournament_summaries", []) or []:
        for summary in tournament.get("program_summaries", []) or []:
            best = {
                clean_text(item.get("dance_code") or item.get("dance"))
                for item in summary.get("best_dances", []) or []
            }
            worst = {
                clean_text(item.get("dance_code") or item.get("dance"))
                for item in summary.get("worst_dances", []) or []
            }
            overlap = sorted((best & worst) - {""})
            if overlap:
                issues.append(
                    {
                        "idd": row.get("idd"),
                        "name": report.get("dancer", {}).get("name") or row.get("name"),
                        "issue_type": "best_worst_dance_overlap",
                        "event_date": tournament.get("event_date"),
                        "tournament_id": tournament.get("tournament_id"),
                        "tournament_title": tournament.get("tournament_title"),
                        "program": summary.get("program"),
                        "display_mode": summary.get("display_mode"),
                        "best_dances": ", ".join(sorted(best)),
                        "worst_dances": ", ".join(sorted(worst)),
                        "comment": "Best and worst dances overlap in tournament summary.",
                    }
                )
            metric_values = []
            for item in (summary.get("best_dances", []) or []) + (summary.get("worst_dances", []) or []) + (summary.get("tied_dances", []) or []):
                if not isinstance(item, dict):
                    continue
                value = number(item.get("metric_value"))
                dance = clean_text(item.get("dance_code") or item.get("dance"))
                if value is not None and dance:
                    metric_values.append((dance, value))
            unique_values = {}
            for dance, value in metric_values:
                unique_values[dance] = value
            if len(unique_values) > 1 and values_all_equal(list(unique_values.items())) and summary.get("display_mode") not in {"tied"}:
                issues.append(
                    {
                        "idd": row.get("idd"),
                        "name": report.get("dancer", {}).get("name") or row.get("name"),
                        "issue_type": "all_tournament_dances_equal_without_tied_mode",
                        "event_date": tournament.get("event_date"),
                        "tournament_id": tournament.get("tournament_id"),
                        "tournament_title": tournament.get("tournament_title"),
                        "program": summary.get("program"),
                        "display_mode": summary.get("display_mode"),
                        "best_dances": ", ".join(sorted(best)),
                        "worst_dances": ", ".join(sorted(worst)),
                        "comment": "All tournament dance metrics are equal but display_mode is not tied.",
                    }
                )
    return issues


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Audit club logic without changing reports.")
    parser.add_argument("--status-csv", type=Path, default=DEFAULT_STATUS_CSV)
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    status_rows = read_status_rows(args.status_csv)
    category_rows: list[dict[str, Any]] = []
    category_chip_rows: list[dict[str, Any]] = []
    dance_rows: list[dict[str, Any]] = []
    trend_rows: list[dict[str, Any]] = []
    tournament_rows: list[dict[str, Any]] = []
    checked = 0
    for row in status_rows:
        report = load_report(row)
        if not report:
            continue
        checked += 1
        category_rows.extend(audit_category_visibility(row, report))
        category_chip_rows.extend(category_chip_visibility_rows(row, report))
        dance_rows.extend(audit_dance_comparisons(row, report))
        trend_rows.extend(audit_trends(row, report))
        tournament_rows.extend(audit_tournaments(row, report))

    write_csv(
        CATEGORY_AUDIT_CSV,
        ["idd", "name", "program", "category", "category_key", "marks_count", "results_count", "dances_count", "has_dance_metrics", "group", "hidden_reason"],
        category_rows,
    )
    write_csv(
        CATEGORY_CHIP_VISIBILITY_CSV,
        [
            "idd",
            "name",
            "program",
            "category",
            "marks_count",
            "results_count",
            "dance_metrics_count",
            "tournaments_count",
            "trainer_summaries_count",
            "trend_items_count",
            "is_visible_chip",
            "visibility_status",
            "visibility_reason",
            "visible_in_html",
        ],
        category_chip_rows,
    )
    write_csv(
        DANCE_COMPARISON_CSV,
        ["idd", "name", "scope_type", "program", "category", "issue_type", "best_dances", "worst_dances", "values", "comment"],
        dance_rows,
    )
    write_csv(
        TREND_AUDIT_CSV,
        ["idd", "name", "scope_type", "program", "category", "dance", "issue_type", "first_avg_place", "last_avg_place", "trend_status", "expected_status"],
        trend_rows,
    )
    write_csv(
        TOURNAMENT_AUDIT_CSV,
        ["idd", "name", "issue_type", "event_date", "tournament_id", "tournament_title", "program", "display_mode", "best_dances", "worst_dances", "comment"],
        tournament_rows,
    )

    category_groups = Counter(row.get("group") for row in category_rows)
    print(f"Reports checked: {checked}")
    print(f"Hidden category slices: {len(category_rows)}")
    print(f"Category chip rows: {len(category_chip_rows)}")
    print(f"  A insufficient: {category_groups.get('A_insufficient_data', 0)}")
    print(f"  B marks useful without final result: {category_groups.get('B_marks_useful_no_final_result', 0)}")
    print(f"Dance comparison issues: {len(dance_rows)}")
    print(f"Trend issues: {len(trend_rows)}")
    print(f"Tournament summary issues: {len(tournament_rows)}")
    print(f"CSV: {CATEGORY_AUDIT_CSV.relative_to(PROJECT_ROOT)}")
    print(f"CSV: {CATEGORY_CHIP_VISIBILITY_CSV.relative_to(PROJECT_ROOT)}")
    print(f"CSV: {DANCE_COMPARISON_CSV.relative_to(PROJECT_ROOT)}")
    print(f"CSV: {TREND_AUDIT_CSV.relative_to(PROJECT_ROOT)}")
    print(f"CSV: {TOURNAMENT_AUDIT_CSV.relative_to(PROJECT_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
