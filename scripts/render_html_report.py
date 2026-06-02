#!/usr/bin/env python3
"""Render a dancer HTML report from an existing JSON report."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape

from dance_display import DANCE_CODE_ORDER, normalize_dance_code, sort_dance_codes


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REPORTS_DIR = PROJECT_ROOT / "reports"
DEFAULT_TEMPLATES_DIR = PROJECT_ROOT / "templates"
DEFAULT_TEMPLATE_NAME = "report.html.j2"
CATEGORY_SLICE_ORDER = ["all", "n", "n_e", "e", "e_d", "d", "d_c", "c", "c_b", "b", "a", "s", "m", "eadc", "open"]
CATEGORY_SLICE_LABELS = {
    "all": "Все категории",
    "n": "N",
    "n_e": "N+E",
    "e": "E",
    "e_d": "E+D",
    "d": "D",
    "d_c": "D+C",
    "c": "C",
    "c_b": "C+B",
    "b": "B",
    "a": "A",
    "s": "S",
    "m": "M",
    "eadc": "EADC",
    "open": "Open",
}


def load_report(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def report_path_for_idd(idd: str) -> Path:
    return DEFAULT_REPORTS_DIR / f"dancer_{idd}_report.json"


def output_path_for_report(report: dict[str, Any], input_path: Path) -> Path:
    idd = str(report.get("dancer", {}).get("idd") or "").strip()
    if idd:
        return DEFAULT_REPORTS_DIR / f"dancer_{idd}_report.html"
    return input_path.with_suffix(".html")


def format_metric_value(value: Any, digits: int = 3) -> str:
    if value is None:
        return "—"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if value != value:
            return "—"
        rounded = round(value)
        if abs(value - rounded) < 10 ** -digits:
            return str(int(rounded))
        return f"{value:.{digits}f}".rstrip("0").rstrip(".")
    if isinstance(value, str):
        stripped = value.strip()
        try:
            numeric = float(stripped)
        except ValueError:
            return value
        if stripped and any(char.isdigit() for char in stripped):
            return format_metric_value(numeric, digits=digits)
    return str(value)


def fmt(value: Any, digits: int = 3) -> str:
    return format_metric_value(value, digits=digits)


def label_program(value: str | None) -> str:
    return {"standard": "стандарт", "latin": "латина", "unknown": "неизвестно"}.get(str(value or ""), str(value or "—"))


def label_entry_type(value: str | None) -> str:
    return {"solo": "солистка", "pair_or_unknown": "пара / не уточнено", "unknown": "неизвестно"}.get(
        str(value or ""),
        str(value or "—"),
    )


def label_category_slice(value: str | None) -> str:
    return CATEGORY_SLICE_LABELS.get(str(value or ""), str(value or "—"))


def dance_label(value: str | None) -> str:
    return {
        "W": "Медленный вальс",
        "T": "Танго",
        "V": "Венский вальс",
        "F": "Фокстрот",
        "Q": "Квикстеп",
        "S": "Самба",
        "C": "Ча-ча-ча",
        "R": "Румба",
        "P": "Пасодобль",
        "J": "Джайв",
    }.get(str(value or ""), str(value or "—"))


def dance_code(value: str | None) -> str:
    return normalize_dance_code(value)


def first_present(items: list[dict[str, Any]], key: str) -> Any:
    for item in items:
        value = item.get(key)
        if value not in (None, ""):
            return value
    return None


def least_stable(metrics: list[dict[str, Any]]) -> dict[str, Any] | None:
    candidates = [item for item in metrics if item.get("std_deviation") is not None]
    if not candidates:
        return None
    return sorted(candidates, key=lambda item: (item.get("std_deviation") or 0, item.get("n_marks") or 0), reverse=True)[0]


JUDGE_SCALE = {
    5: ("★★★★★", "Значительно строже среднего"),
    4: ("★★★★☆", "Строже среднего"),
    3: ("★★★☆☆", "Близко к среднему"),
    2: ("★★☆☆☆", "Мягче среднего"),
    1: ("★☆☆☆☆", "Значительно мягче среднего"),
}


def percentile_rank(value: float, values: list[float], reverse: bool = False) -> int:
    ordered = sorted(values, reverse=reverse)
    if not ordered:
        return 3
    index = ordered.index(value)
    bucket = int(index * 5 / len(ordered))
    return max(1, min(5, 5 - bucket))


def stability_label_for_percent(percent: int) -> str:
    if percent >= 86:
        return "Очень высокая"
    if percent >= 68:
        return "Высокая"
    if percent >= 48:
        return "Средняя"
    if percent >= 30:
        return "Низкая"
    return "Очень низкая"


def add_stability_interpretation(metrics: list[dict[str, Any]], stability_values: list[float]) -> list[dict[str, Any]]:
    interpreted = []
    values = [float(value) for value in stability_values if value is not None]
    min_value = min(values) if values else None
    max_value = max(values) if values else None
    for item in metrics:
        enriched = dict(item)
        value = item.get("final_std_deviation")
        if value is None:
            percent = 50
        elif min_value is None or max_value is None or max_value == min_value:
            percent = 78
        else:
            normalized = (float(value) - min_value) / (max_value - min_value)
            percent = round(95 - normalized * 70)
            percent = max(18, min(95, percent))
        enriched["stability_score"] = percent
        enriched["stability_label"] = stability_label_for_percent(percent)
        enriched["stability_percent"] = percent
        enriched["stability_style"] = f"--stability-width: {percent}%;"
        interpreted.append(enriched)
    return interpreted


def add_judge_interpretation(items: list[dict[str, Any]], all_deviations: list[float]) -> list[dict[str, Any]]:
    interpreted = []
    for item in items:
        enriched = dict(item)
        value = item.get("avg_deviation")
        if value is None:
            score = 3
        else:
            score = percentile_rank(float(value), all_deviations, reverse=True)
        stars, label = JUDGE_SCALE[score]
        enriched["judge_score"] = score
        enriched["judge_stars"] = stars
        enriched["judge_label"] = label
        interpreted.append(enriched)
    return interpreted


def user_facing_notes(notes: list[str]) -> list[str]:
    replacements = {
        "Performance dance analytics use final_avg_place: one dance result per protocol, round, and dance.": (
            "Танцевальная аналитика использует итоговое место танца: один результат на протокол, тур и танец."
        ),
        "judge_avg_place is diagnostic and is not used for strongest/weakest/stability/trend rankings.": (
            "Судейская метрика используется только как внутренняя диагностика и не входит в основные рейтинги."
        ),
        "Cross analytics are kept separate and are not mixed into place metrics.": (
            "Кресты учитываются отдельно и не смешиваются с метриками мест."
        ),
        "Parser intentionally skips unsupported FKT/EADC raw strings when judge-position mapping is not validated.": (
            "Некоторые технически неоднозначные строки протоколов пропускаются до ручной проверки структуры."
        ),
    }
    return [replacements.get(note, note) for note in notes]


def short_stability_label(score: int | None) -> str:
    if score is None:
        return "Средняя"
    if score >= 4:
        return "Высокая"
    if score <= 2:
        return "Низкая"
    return "Средняя"


def dance_names(items: list[dict[str, Any]], limit: int = 2) -> str:
    names = [dance_label(item.get("dance")).lower() for item in items[:limit]]
    if not names:
        return "нет устойчивого набора танцев"
    if len(names) == 1:
        return names[0]
    return " и ".join([", ".join(names[:-1]), names[-1]]) if len(names) > 2 else " и ".join(names)


def russian_plural(count: int, one: str, few: str, many: str) -> str:
    if count % 10 == 1 and count % 100 != 11:
        return one
    if count % 10 in {2, 3, 4} and count % 100 not in {12, 13, 14}:
        return few
    return many


def pluralize(value: Any, one: str, few: str, many: str) -> str:
    try:
        count = int(value)
    except (TypeError, ValueError):
        count = 0
    return f"{count} {russian_plural(count, one, few, many)}"


def protocol_word(count: int) -> str:
    return russian_plural(count, "протокол", "протокола", "протоколов")


def reliable_metrics(metrics: list[dict[str, Any]], min_protocols: int = 6) -> list[dict[str, Any]]:
    return [item for item in metrics if int(item.get("n_protocols") or 0) >= min_protocols]


def limited_metrics(metrics: list[dict[str, Any]], min_protocols: int = 6) -> list[dict[str, Any]]:
    return [
        item
        for item in metrics
        if 0 < int(item.get("n_protocols") or 0) < min_protocols
    ]


def sort_by_value(items: list[dict[str, Any]], key: str, reverse: bool = False) -> list[dict[str, Any]]:
    return sorted(
        [item for item in items if item.get(key) is not None],
        key=lambda item: (float(item.get(key) or 0), -(int(item.get("n_protocols") or 0))),
        reverse=reverse,
    )


def best_dance_candidates(items: list[dict[str, Any]], close_delta: float = 0.35) -> list[dict[str, Any]]:
    ranked = sort_by_value(items, "final_avg_place")
    if not ranked:
        return []
    best = ranked[0]
    result = [best]
    if len(ranked) > 1:
        second = ranked[1]
        if float(second.get("final_avg_place") or 0) - float(best.get("final_avg_place") or 0) <= close_delta:
            result.append(second)
    return result


def dance_comparison_from_metrics(items: list[dict[str, Any]]) -> dict[str, Any]:
    scored = [
        item
        for item in items
        if item.get("final_avg_place") is not None
    ]
    if not scored:
        return {
            "display_mode": "insufficient_data",
            "best_dances": [],
            "worst_dances": [],
            "evaluated_dance": None,
            "tied_dances": [],
            "tied_metric_value": None,
        }
    scored = sort_by_value(scored, "final_avg_place")
    if len(scored) == 1:
        return {
            "display_mode": "single_dance",
            "best_dances": [],
            "worst_dances": [],
            "evaluated_dance": scored[0],
            "tied_dances": [],
            "tied_metric_value": None,
        }
    best_value = float(scored[0].get("final_avg_place") or 0)
    worst_value = float(scored[-1].get("final_avg_place") or 0)
    if abs(best_value - worst_value) < 0.001:
        return {
            "display_mode": "all_equal",
            "best_dances": [],
            "worst_dances": [],
            "evaluated_dance": None,
            "tied_dances": scored,
            "tied_metric_value": best_value,
        }
    return {
        "display_mode": "best_worst",
        "best_dances": [item for item in scored if abs(float(item.get("final_avg_place") or 0) - best_value) < 0.001],
        "worst_dances": [item for item in scored if abs(float(item.get("final_avg_place") or 0) - worst_value) < 0.001],
        "evaluated_dance": None,
        "tied_dances": [],
        "tied_metric_value": None,
    }


def split_program_trends(
    trends: list[dict[str, Any]],
    min_dates: int = 2,
    eligible_dances: set[Any] | None = None,
) -> dict[str, list[dict[str, Any]]]:
    reliable = [
        item
        for item in trends
        if item.get("trend_over_time") is not None and int(item.get("n_dates") or 0) >= min_dates
    ]
    if eligible_dances is not None:
        reliable = [item for item in reliable if item.get("dance") in eligible_dances]
    for item in reliable:
        if not item.get("trend_status"):
            delta = item.get("first_to_last_delta")
            if delta is None:
                delta = item.get("trend_over_time")
            try:
                delta_value = float(delta)
            except (TypeError, ValueError):
                delta_value = 0.0
            if abs(delta_value) < 0.001:
                item["trend_status"] = "stable"
            elif delta_value < 0:
                item["trend_status"] = "improving"
            else:
                item["trend_status"] = "declining"
    improving = sorted(
        [item for item in reliable if item.get("trend_status") == "improving"],
        key=lambda item: float(item.get("first_to_last_delta") if item.get("first_to_last_delta") is not None else item.get("trend_over_time") or 0),
    )
    declining = sorted(
        [item for item in reliable if item.get("trend_status") == "declining"],
        key=lambda item: float(item.get("first_to_last_delta") if item.get("first_to_last_delta") is not None else item.get("trend_over_time") or 0),
        reverse=True,
    )
    stable = sorted(
        [item for item in reliable if item.get("trend_status") == "stable"],
        key=lambda item: item.get("dance") or "",
    )
    return {"improving": improving, "declining": declining, "stable": stable}


def unique_by_dance(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen = set()
    result = []
    for item in items:
        dance = item.get("dance")
        if dance in seen:
            continue
        seen.add(dance)
        result.append(item)
    return result


def trend_sentence(trend_groups: dict[str, list[dict[str, Any]]], enough_data: bool) -> str:
    if trend_groups["improving"] and trend_groups["declining"]:
        return (
            f"Положительная динамика заметнее всего в танцах: {dance_names(trend_groups['improving'])}. "
            f"Больше внимания стоит уделить танцам: {dance_names(trend_groups['declining'])}."
        )
    if trend_groups["improving"]:
        return f"По динамике лучше всего выглядят {dance_names(trend_groups['improving'])}: итоговые места там постепенно улучшаются."
    if trend_groups["declining"]:
        return f"Тревожная динамика заметнее всего в танцах: {dance_names(trend_groups['declining'])}. Это зона для спокойной проверки на тренировках."
    if enough_data:
        return "Резкой тревожной динамики не видно: результаты держатся примерно в одном диапазоне."
    return "Для уверенного вывода по динамике пока мало наблюдений."


def first_dance_name(items: list[dict[str, Any]]) -> str:
    if not items:
        return ""
    return dance_label(items[0].get("dance")).lower()


def dance_name_case(dance: Any, grammatical_case: str) -> str:
    code = normalize_dance_code(dance)
    forms = {
        "W": {"dative": "медленному вальсу", "prepositional": "медленном вальсе"},
        "T": {"dative": "танго", "prepositional": "танго"},
        "V": {"dative": "венскому вальсу", "prepositional": "венском вальсе"},
        "F": {"dative": "фокстроту", "prepositional": "фокстроте"},
        "Q": {"dative": "квикстепу", "prepositional": "квикстепе"},
        "S": {"dative": "самбе", "prepositional": "самбе"},
        "Ch": {"dative": "ча-ча-ча", "prepositional": "ча-ча-ча"},
        "R": {"dative": "румбе", "prepositional": "румбе"},
        "P": {"dative": "пасодоблю", "prepositional": "пасодобле"},
        "J": {"dative": "джайву", "prepositional": "джайве"},
    }
    return forms.get(code, {}).get(grammatical_case, dance_label(dance).lower())


def parent_strength_text(title_phrase: str, strong_items: list[dict[str, Any]], enough_data: bool) -> str:
    if not enough_data:
        return f"По {title_phrase} пока есть только первые наблюдения, поэтому сильные стороны лучше оценивать осторожно."
    if not strong_items:
        return f"В {title_phrase} результаты основных танцев сейчас выглядят близко друг к другу."
    name = first_dance_name(strong_items)
    return f"Сильная сторона программы сейчас — {name}. В этом танце чаще всего достигаются лучшие результаты."


def parent_attention_text(title_phrase: str, attention_items: list[dict[str, Any]], enough_data: bool) -> str:
    if not enough_data:
        return "Основной фокус развития пока лучше определять по ближайшим турнирам: выборка ещё небольшая."
    if not attention_items:
        return "Отдельный главный фокус развития сейчас не выделяется: результаты выглядят достаточно ровно."
    names = [dance_name_case(item.get("dance"), "dative") for item in attention_items[:2]]
    if len(names) == 1:
        return f"Сейчас больше всего внимания стоит уделить {names[0]}. Здесь результат пока менее устойчив, чем в остальных танцах программы."
    return f"Сейчас больше всего внимания стоит уделить танцам: {', '.join(names)}. Это главный фокус для спокойной работы на тренировках."


def parent_trend_text(
    trend_groups: dict[str, list[dict[str, Any]]],
    excluded_dances: set[Any],
    enough_data: bool,
) -> str:
    if not enough_data:
        return "Для уверенного вывода по динамике пока мало наблюдений."
    improving = [item for item in trend_groups["improving"] if item.get("dance") not in excluded_dances]
    if improving:
        return f"В последних турнирах заметен прогресс в {dance_name_case(improving[0].get('dance'), 'prepositional')}."
    return "Существенных изменений за последнее время не наблюдается."


def parent_trend_items(
    trend_groups: dict[str, list[dict[str, Any]]],
    excluded_dances: set[Any],
    enough_data: bool,
) -> list[dict[str, Any]]:
    if not enough_data:
        return []
    return [item for item in trend_groups["improving"] if item.get("dance") not in excluded_dances][:1]


def parent_overview(
    title_phrase: str,
    strong_items: list[dict[str, Any]],
    attention_items: list[dict[str, Any]],
    trend_text: str,
    enough_data: bool,
) -> list[str]:
    if not enough_data:
        return [
            f"По {title_phrase} пока рано делать устойчивый вывод.",
            "Данные можно использовать как ориентир, но основная картина станет понятнее после нескольких следующих выступлений.",
        ]
    program_name = "Стандартная программа" if "стандарт" in title_phrase else "Латинская программа"
    parts = []
    if strong_items:
        parts.append(f"лучше всего сейчас получается {first_dance_name(strong_items)}")
    if attention_items:
        parts.append(f"главный фокус развития — {first_dance_name(attention_items)}")
    if parts:
        sentences = [f"{program_name}: {', а '.join(parts)}."]
    else:
        sentences = [f"{program_name} выглядит достаточно ровной по текущим данным."]
    if trend_text and trend_text != "Существенных изменений за последнее время не наблюдается.":
        sentences.append("В динамике тоже есть положительный сигнал.")
    return [" ".join(sentences[:2])]


def program_parent_view(key: str, program: dict[str, Any]) -> dict[str, Any]:
    metrics = program.get("metrics", []) or []
    core = reliable_metrics(metrics)
    limited = limited_metrics(metrics)
    enough_data = len(core) >= 2
    comparison = {
        "display_mode": program.get("display_mode"),
        "best_dances": program.get("best_dances") or [],
        "worst_dances": program.get("worst_dances") or [],
        "evaluated_dance": program.get("evaluated_dance"),
        "tied_dances": program.get("tied_dances") or [],
        "tied_metric_value": program.get("tied_metric_value"),
    }
    if not comparison["display_mode"]:
        comparison = dance_comparison_from_metrics(core)
    ranked = comparison["best_dances"] if comparison["display_mode"] == "best_worst" else []
    parent_comparison = dance_comparison_from_metrics(core)
    parent_ranked = parent_comparison["best_dances"] if parent_comparison["display_mode"] == "best_worst" else []
    full_ranked = sort_by_value(core, "final_avg_place")
    stable = sort_by_value(core, "final_std_deviation")
    core_dances = {item.get("dance") for item in core}
    trends = split_program_trends(program.get("trends", []) or [], eligible_dances=core_dances)
    by_dance = {item.get("dance"): item for item in core}
    declining_metrics = [
        by_dance[item.get("dance")]
        for item in trends["declining"]
        if item.get("dance") in by_dance
    ]
    best_dances = {item.get("dance") for item in parent_ranked}
    attention_candidates = unique_by_dance(declining_metrics + sort_by_value(core, "final_avg_place", reverse=True))
    if parent_comparison["display_mode"] == "best_worst":
        growth = [item for item in (parent_comparison["worst_dances"] or attention_candidates) if item.get("dance") not in best_dances]
    else:
        growth = []
    attention_overlap_note = None
    title_phrase = "стандартной программе" if key == "standard" else "латинской программе"
    excluded_for_trend = best_dances | {item.get("dance") for item in growth}
    parent_improving = parent_trend_items(trends, excluded_for_trend, enough_data=enough_data)
    parent_trend = parent_trend_text(trends, excluded_for_trend, enough_data=enough_data)

    if enough_data:
        best_text = parent_strength_text(title_phrase, parent_ranked[:1], enough_data=True)
        attention_text = parent_attention_text(title_phrase, growth[:2], enough_data=True)
        overview = parent_overview(title_phrase, parent_ranked[:1], growth[:1], parent_trend, enough_data=True)
    else:
        best_text = parent_strength_text(title_phrase, [], enough_data=False)
        attention_text = parent_attention_text(title_phrase, [], enough_data=False)
        overview = parent_overview(title_phrase, [], [], parent_trend, enough_data=False)

    if limited:
        overview.append("Для некоторых танцев пока недостаточно данных для устойчивых выводов.")

    return {
        "title": program.get("title"),
        "enough_data": enough_data,
        **comparison,
        "best": ranked[:3],
        "full_ranked": full_ranked,
        "attention": growth[:3],
        "attention_overlap_note": attention_overlap_note,
        "stable": stable[:3],
        "improving": trends["improving"][:3],
        "declining": trends["declining"][:3],
        "stable_trends": trends["stable"][:3],
        "limited": limited,
        "parent_strength_dances": [item.get("dance") for item in parent_ranked[:1] if item.get("dance")],
        "parent_attention_dances": [item.get("dance") for item in growth[:2] if item.get("dance")],
        "parent_trend_dances": [item.get("dance") for item in parent_improving if item.get("dance")],
        "best_text": best_text,
        "attention_text": attention_text,
        "trend_text": parent_trend,
        "overview": overview,
    }


def overall_progress(trends: list[dict[str, Any]]) -> tuple[str, str, dict[str, int]]:
    reliable = [item for item in trends if item.get("first_to_last_delta") is not None]
    counts = {"improving": 0, "declining": 0, "stable": 0}
    for item in reliable:
        delta = float(item.get("first_to_last_delta") or 0)
        if delta <= -0.5:
            counts["improving"] += 1
        elif delta >= 0.5:
            counts["declining"] += 1
        else:
            counts["stable"] += 1
    if len(reliable) < 3:
        return "недостаточно данных", "Пока недостаточно устойчивых наблюдений, чтобы уверенно описать динамику сезона.", counts
    if counts["improving"] >= counts["declining"] + 2:
        return "стабильный прогресс", "По нескольким танцам итоговые места становятся лучше, общий вектор выглядит положительным.", counts
    if counts["improving"] > counts["declining"]:
        return "умеренный прогресс", "Есть положительная динамика, но часть танцев ещё требует закрепления результата.", counts
    if counts["stable"] >= max(counts["improving"], counts["declining"]):
        return "стабильный уровень", "Результаты в основном держатся в близком диапазоне от турнира к турниру.", counts
    return "неоднородная динамика", "Часть танцев улучшается, а часть требует внимания, поэтому полезнее смотреть стандарт и латину отдельно.", counts


def tournament_cards(tournaments: list[dict[str, Any]], limit: int = 5) -> list[dict[str, Any]]:
    cards = []
    for item in sorted(tournaments, key=lambda value: value.get("event_date") or "")[-limit:]:
        card = dict(item)
        card["dance_codes"] = sort_dance_codes([normalize_dance_code(code) for code in item.get("dance_codes", []) or []])
        cards.append(card)
    return cards


def tournament_performance(tournaments: list[dict[str, Any]], dynamics: list[dict[str, Any]], limit: int = 3) -> dict[str, list[dict[str, Any]]]:
    date_to_titles: dict[str, list[str]] = {}
    date_to_cities: dict[str, list[str]] = {}
    for item in tournaments:
        date_to_titles.setdefault(item.get("event_date"), []).append(item.get("tournament_title") or "—")
        if item.get("city"):
            date_to_cities.setdefault(item.get("event_date"), []).append(item.get("city") or "")
    rows = []
    grouped: dict[str, list[float]] = {}
    for item in dynamics:
        if item.get("event_date") and item.get("final_avg_place") is not None:
            grouped.setdefault(item["event_date"], []).append(float(item["final_avg_place"]))
    for date, values in grouped.items():
        if not values:
            continue
        rows.append(
            {
                "event_date": date,
                "tournament_title": "; ".join(dict.fromkeys(date_to_titles.get(date, ["—"]))),
                "city": "; ".join(dict.fromkeys(city for city in date_to_cities.get(date, []) if city)),
                "avg_place": sum(values) / len(values),
            }
        )
    rows = sorted(rows, key=lambda item: (item["avg_place"], item["event_date"]))
    if not rows:
        return {"best": [], "hardest": []}
    display_limit = 2 if len(rows) <= 5 else limit
    best = rows[:display_limit]
    best_keys = {(item["event_date"], item["tournament_title"]) for item in best}
    hardest_pool = [item for item in reversed(rows) if (item["event_date"], item["tournament_title"]) not in best_keys]
    return {"best": best, "hardest": hardest_pool[:display_limit]}


def build_tournament_details(report: dict[str, Any]) -> list[dict[str, Any]]:
    return build_tournament_details_from_records(report.get("tournaments", {}).get("dance_results", []) or [])


def dance_sort_key(row: dict[str, Any]) -> tuple[int, str, str]:
    code = normalize_dance_code(row.get("dance_code") or row.get("dance"))
    return (DANCE_CODE_ORDER.get(code, 999), str(row.get("program") or ""), code)


def build_tournament_details_from_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows_by_tournament: dict[tuple[Any, Any, Any], list[dict[str, Any]]] = {}
    for row in records:
        key = (row.get("event_date"), row.get("tournament_id"), row.get("tournament_title"))
        rows_by_tournament.setdefault(key, []).append(row)

    details = []
    for (event_date, tournament_id, tournament_title), rows in sorted(rows_by_tournament.items(), key=lambda item: item[0][0] or ""):
        dance_rows = sorted(
            rows,
            key=lambda row: (row.get("protocol_id") or 0, row.get("program") or "", dance_sort_key(row)),
        )
        scored = [row for row in dance_rows if row.get("final_place") is not None]
        best = min(scored, key=lambda row: float(row["final_place"])) if scored else None
        weakest = max(scored, key=lambda row: float(row["final_place"])) if scored else None
        avg_place = (sum(float(row["final_place"]) for row in scored) / len(scored)) if scored else None
        details.append(
            {
                "event_date": event_date,
                "tournament_id": tournament_id,
                "tournament_title": tournament_title,
                "city": next((row.get("city") for row in rows if row.get("city")), ""),
                "protocols": len({row.get("protocol_id") for row in rows if row.get("protocol_id") is not None}),
                "categories": sorted({str(row.get("category")) for row in rows if row.get("category")}),
                "programs": sorted({str(row.get("program")) for row in rows if row.get("program")}),
                "dance_results": dance_rows,
                "best_dance": best,
                "weakest_dance": weakest,
                "avg_final_place": avg_place,
                "has_standard": any(row.get("program") == "standard" for row in rows),
                "has_latin": any(row.get("program") == "latin" for row in rows),
            }
        )
    return details


def trainer_tournament_summaries(report: dict[str, Any]) -> list[dict[str, Any]]:
    summaries = report.get("trainer_mode", {}).get("tournament_summaries", []) or []
    if not summaries:
        summaries = build_tournament_details(report)

    rank_by_key = {
        (item.get("event_date"), item.get("tournament_id"), item.get("tournament_title")): {
            "tournament_rank": item.get("tournament_rank"),
            "rank_direction": item.get("rank_direction") or "",
        }
        for item in report.get("tournaments", {}).get("items", []) or []
    }
    result = []
    for item in summaries:
        merged = dict(item)
        rank = rank_by_key.get((item.get("event_date"), item.get("tournament_id"), item.get("tournament_title")), {})
        merged.setdefault("tournament_rank", rank.get("tournament_rank"))
        merged.setdefault("rank_direction", rank.get("rank_direction") or "")
        result.append(merged)
    return result


def report_filter_options(report: dict[str, Any], summary: dict[str, Any]) -> dict[str, Any]:
    tournaments = report.get("tournaments", {}) or {}
    records = []
    for key in ["items", "protocols", "dance_results"]:
        records.extend(tournaments.get(key) or [])
    records.extend((report.get("trainer_mode", {}) or {}).get("tournament_summaries") or [])
    cities = sorted({str(item.get("city")).strip() for item in records if str(item.get("city") or "").strip()})
    dates = sorted({str(item.get("event_date")).strip() for item in records if str(item.get("event_date") or "").strip()})
    return {
        "cities": cities,
        "date_from": dates[0] if dates else summary.get("date_from", ""),
        "date_to": dates[-1] if dates else summary.get("date_to", ""),
        "aggregates_note": "Агрегированные выводы рассчитаны по полной выборке.",
    }


def prepare_program_view(key: str, title: str, payload: dict[str, Any], fallback_trends: list[dict[str, Any]]) -> dict[str, Any]:
    raw_metrics = payload.get("metrics", []) or []
    program_stability_values = [
        float(item["final_std_deviation"])
        for item in raw_metrics
        if item.get("final_std_deviation") is not None and int(item.get("n_protocols") or 0) >= 2
    ]
    metrics = add_stability_interpretation(raw_metrics, program_stability_values)
    trends = payload.get("trends")
    if trends is None:
        trends = [item for item in fallback_trends if item.get("program") == key]
    most_stable = payload.get("most_stable_dance") or payload.get("most_stable")
    if most_stable:
        most_stable = add_stability_interpretation([most_stable], program_stability_values)[0]
    return {
        "key": payload.get("key", "all"),
        "label": payload.get("label", label_category_slice(payload.get("key", "all"))),
        "title": title,
        "included_categories": payload.get("included_categories") or [],
        "evidence": payload.get("evidence") or {},
        "visibility": payload.get("visibility") or {},
        "is_visible_chip": payload.get("is_visible_chip", True),
        "visibility_status": payload.get("visibility_status", "primary"),
        "visibility_reason": payload.get("visibility_reason", ""),
        "marks_derived_dance_metrics": payload.get("marks_derived_dance_metrics") or [],
        "analysis_note": (
            "по оценкам судей, без финального результата"
            if int((payload.get("evidence") or {}).get("marks") or 0) > 0
            and int((payload.get("evidence") or {}).get("results") or 0) == 0
            else ""
        ),
        "metrics": metrics,
        "display_mode": payload.get("display_mode"),
        "best_dances": payload.get("best_dances") or [],
        "worst_dances": payload.get("worst_dances") or [],
        "evaluated_dance": payload.get("evaluated_dance"),
        "tied_dances": payload.get("tied_dances") or [],
        "tied_metric_value": payload.get("tied_metric_value"),
        "best_by_final_average": payload.get("best_by_final_average"),
        "best_by_median": payload.get("best_by_median"),
        "most_stable": most_stable,
        "best_peak": payload.get("best_peak"),
        "worst_by_final_average": payload.get("worst_by_final_average"),
        "judge_level_best": payload.get("judge_level_best"),
        "strongest": payload.get("strongest_dance"),
        "weakest": payload.get("weakest_dance"),
        "least_stable": least_stable(metrics),
        "most_improved": payload.get("most_improved_dance"),
        "trends": trends or [],
        "protocol_count": int(payload.get("protocol_count") or 0),
        "tournament_count": int(payload.get("tournament_count") or 0),
    }


def build_role_views(
    programs: dict[str, dict[str, Any]],
    report: dict[str, Any],
    summary: dict[str, Any],
    parent_programs: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    program_views = {key: program_parent_view(key, program) for key, program in programs.items()}
    parent_views = parent_programs or program_views
    all_metrics = [item for program in programs.values() for item in program["metrics"]]
    trends = report.get("dances", {}).get("trends", []) or []
    progress_label, progress_comment, progress_counts = overall_progress(trends)
    avg_stability = round(sum(item.get("stability_score", 50) for item in all_metrics) / len(all_metrics)) if all_metrics else 50
    stability_label = stability_label_for_percent(avg_stability)
    tournaments = report.get("tournaments", {}).get("items", []) or []
    tournament_perf = tournament_performance(tournaments, report.get("dances", {}).get("dynamics_by_date", []) or [])

    return {
        "parent": {
            "season_status": progress_label,
            "season_comment": progress_comment,
            "progress_counts": progress_counts,
            "stability": stability_label,
            "latest_tournaments": tournament_cards(tournaments),
            "programs": parent_views,
        },
        "trainer": {
            "programs": program_views,
            "best_tournaments": tournament_perf["best"],
            "hardest_tournaments": tournament_perf["hardest"],
        },
    }


def has_category_slice_evidence(slice_payload: dict[str, Any]) -> bool:
    if slice_payload.get("visibility_status") == "hidden":
        return False
    if "is_visible_chip" in slice_payload:
        return bool(slice_payload.get("is_visible_chip"))
    evidence = slice_payload.get("evidence") or {}
    metrics = slice_payload.get("metrics") or []
    if evidence:
        return (
            int(evidence.get("protocols") or 0) > 0
            and (int(evidence.get("marks") or 0) > 0 or int(evidence.get("results") or 0) > 0)
        )
    return int(slice_payload.get("protocol_count") or 0) > 0 or len(metrics) > 0


def build_view_model(report: dict[str, Any], input_path: Path, output_path: Path) -> dict[str, Any]:
    tournaments = report.get("tournaments", {}).get("protocols", [])
    entry_types = {item.get("entry_type") for item in tournaments if item.get("entry_type")}
    entry_label = "солистка" if entry_types == {"solo"} else ", ".join(label_entry_type(item) for item in sorted(entry_types)) or "—"
    all_metrics = report.get("dances", {}).get("metrics", [])
    judges = dict(report.get("judges", {}))
    warnings = dict(report.get("warnings", {}))
    warnings["notes"] = user_facing_notes(warnings.get("notes", []) or [])
    judge_deviations = [
        float(item["avg_deviation"])
        for item in (judges.get("strictest", []) or []) + (judges.get("softest", []) or [])
        if item.get("avg_deviation") is not None
    ]
    judges["strictest"] = add_judge_interpretation(judges.get("strictest", []) or [], judge_deviations)
    judges["softest"] = add_judge_interpretation(judges.get("softest", []) or [], judge_deviations)

    programs: dict[str, dict[str, Any]] = {}
    parent_programs: dict[str, dict[str, Any]] = {}
    for key, title in [("standard", "стандарт"), ("latin", "латина")]:
        payload = report.get("programs", {}).get(key, {})
        programs[key] = prepare_program_view(key, title, payload, report.get("dances", {}).get("trends", []) or [])
        slices = report.get("category_slices", {}).get(key, {}) or {}
        ordered_slice_keys = [slice_key for slice_key in CATEGORY_SLICE_ORDER if slice_key in slices]
        ordered_slice_keys.extend(slice_key for slice_key in slices if slice_key not in ordered_slice_keys)
        programs[key]["category_slices"] = [
            prepare_program_view(key, title, slices[slice_key], [])
            for slice_key in ordered_slice_keys
            if has_category_slice_evidence(slices[slice_key])
        ]
        for slice_view in programs[key]["category_slices"]:
            slice_view.update(program_parent_view(key, slice_view))
        programs[key]["category_primary_slices"] = [
            slice_view
            for slice_view in programs[key]["category_slices"]
            if slice_view.get("visibility_status", "primary") == "primary"
        ]
        programs[key]["category_limited_slices"] = [
            slice_view
            for slice_view in programs[key]["category_slices"]
            if slice_view.get("visibility_status") == "limited"
        ]
        parent_groups = report.get("parent_category_groups", {}).get(key, {}) or {}
        ordered_parent_keys = [slice_key for slice_key in ["all", "n_e", "e_d", "eadc"] if slice_key in parent_groups]
        ordered_parent_keys.extend(slice_key for slice_key in parent_groups if slice_key not in ordered_parent_keys)
        group_views = [
            prepare_program_view(key, title, parent_groups[group_key], [])
            for group_key in ordered_parent_keys
        ]
        for group_view in group_views:
            group_view.update(program_parent_view(key, group_view))
        parent_programs[key] = (group_views[0].copy() if group_views else program_parent_view(key, programs[key]))
        parent_programs[key]["title"] = title
        parent_programs[key]["category_groups"] = group_views

    role_views = build_role_views(programs, report, report.get("summary", {}), parent_programs=parent_programs)

    sections = []
    checks = [
        ("Title", True),
        ("Dancer summary", bool(report.get("summary"))),
        ("Data quality warnings", bool(report.get("warnings"))),
        ("Standard summary", bool(programs["standard"]["metrics"])),
        ("Latin summary", bool(programs["latin"]["metrics"])),
        ("Judge analytics", bool(report.get("judges"))),
        ("Dance analytics", bool(report.get("dances"))),
        ("Tournament list", bool(report.get("tournaments", {}).get("protocols"))),
        ("Methodology note", True),
    ]
    sections.extend(name for name, rendered in checks if rendered)

    return {
        "report": report,
        "input_path": input_path,
        "output_path": output_path,
        "metadata": report.get("metadata", {}),
        "dancer": report.get("dancer", {}),
        "summary": report.get("summary", {}),
        "entry_label": entry_label,
        "programs": programs,
        "judges": judges,
        "dances": report.get("dances", {}),
        "tournaments": report.get("tournaments", {}),
        "tournament_details": build_tournament_details(report),
        "trainer_tournament_summaries": trainer_tournament_summaries(report),
        "warnings": warnings,
        "role_views": role_views,
        "filter_options": report_filter_options(report, report.get("summary", {})),
        "sections": sections,
    }


def build_print_summary(view_model: dict[str, Any]) -> dict[str, int]:
    warnings = view_model["warnings"]
    judges = view_model["judges"]
    dances = view_model["dances"]
    return {
        "sections_filled": len(view_model["sections"]),
        "warnings": sum(
            len(warnings.get(key, []) or [])
            for key in [
                "parser_status",
                "low_confidence_dances",
                "missing_dances",
                "incomplete_protocols",
                "ranking_mismatch",
                "notes",
            ]
        ),
        "judge_rows": len(judges.get("strictest", []) or []) + len(judges.get("softest", []) or []),
        "dance_rows": len(dances.get("metrics", []) or []) + len(dances.get("trends", []) or []),
    }


def render_report(view_model: dict[str, Any]) -> str:
    environment = Environment(
        loader=FileSystemLoader(DEFAULT_TEMPLATES_DIR),
        autoescape=select_autoescape(["html", "xml", "j2"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    environment.filters["fmt"] = fmt
    environment.filters["program_label"] = label_program
    environment.filters["entry_label"] = label_entry_type
    environment.filters["dance_label"] = dance_label
    environment.filters["dance_code"] = dance_code
    environment.filters["pluralize"] = pluralize
    template = environment.get_template(DEFAULT_TEMPLATE_NAME)
    return template.render(**view_model)


def display_path(path: Path) -> str:
    try:
        return str(path.relative_to(PROJECT_ROOT))
    except ValueError:
        return str(path)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Render an HTML report from an existing dancer JSON report.")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--idd", help="External Compreg dancer IDD. Reads reports/dancer_<idd>_report.json.")
    source.add_argument("--input", type=Path, help="Path to an existing JSON report.")
    parser.add_argument("--output", type=Path, default=None, help="Optional HTML output path.")
    if not argv:
        parser.print_help(sys.stderr)
        raise SystemExit(2)
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    input_path = args.input or report_path_for_idd(args.idd)
    input_path = input_path if input_path.is_absolute() else PROJECT_ROOT / input_path
    report = load_report(input_path)
    output_path = args.output or output_path_for_report(report, input_path)
    output_path = output_path if output_path.is_absolute() else PROJECT_ROOT / output_path
    output_path.parent.mkdir(parents=True, exist_ok=True)

    view_model = build_view_model(report, input_path, output_path)
    output_path.write_text(render_report(view_model), encoding="utf-8")
    summary = build_print_summary(view_model)

    print(f"Wrote {display_path(output_path)}")
    print(
        "Summary: "
        f"sections={summary['sections_filled']}; "
        f"warnings={summary['warnings']}; "
        f"judge_rows={summary['judge_rows']}; "
        f"dance_rows={summary['dance_rows']}; "
        f"output={display_path(output_path)}"
    )
    print("Rendered sections:")
    for section in view_model["sections"]:
        print(f"- {section}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
