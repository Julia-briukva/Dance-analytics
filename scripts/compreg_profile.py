"""Parse Compreg dancer profile cards for report headers."""

from __future__ import annotations

import re
from typing import Any

from bs4 import BeautifulSoup


SPACE_RE = re.compile(r"\s+")


def clean_text(value: object | None) -> str:
    if value is None:
        return ""
    return SPACE_RE.sub(" ", str(value).replace("\xa0", " ")).strip()


def clean_profile(profile: dict[str, Any]) -> dict[str, str]:
    cleaned: dict[str, str] = {}
    for key, value in profile.items():
        text = clean_text(value)
        if text:
            cleaned[key] = text
    return cleaned


def parse_class_value(value: str | None) -> str:
    text = clean_text(value)
    if not text:
        return ""
    match = re.search(r"Класс\s+([A-ZА-ЯЁ+]+)", text, flags=re.I)
    return match.group(1).upper() if match else text


def parse_skr_class_line(value: str | None) -> dict[str, str]:
    text = clean_text(value)
    if not text:
        return {}
    match = re.search(r"СКРкл\s*=\s*([0-9]+(?:[.,][0-9]+)?)\s*-?\s*(.*)", text, flags=re.I)
    if not match:
        return {}
    return {
        "skr_class": match.group(1).replace(",", "."),
        "norm_status": clean_text(match.group(2)),
    }


def _program_from_group(value: str) -> str:
    parts = [part for part in re.split(r"[_\W]+", clean_text(value).lower()) if part]
    if "st" in parts:
        return "st"
    if "la" in parts:
        return "la"
    return ""


def _class_from_group(value: str) -> str:
    group = clean_text(value)
    return group.split("_", 1)[0].upper() if group else ""


def _parse_class_result_containers(soup: BeautifulSoup, profile: dict[str, Any]) -> None:
    """Read St/La СКРкл from the matching Compreg class-result container.

    A profile can contain several result containers, for example E_st_solo,
    N_st_solo, E_la_solo, N_la_solo. The report header must use the container
    for the same program and current class. Reading the first two СКРкл rows in
    document order mixes programs/classes for some dancers.
    """

    fallback_by_program: dict[str, dict[str, str]] = {}
    exact_by_program: dict[str, dict[str, str]] = {}

    for container in soup.select(".res-class-container"):
        group_node = container.select_one("[data-group]")
        group = clean_text(group_node.get("data-group") if group_node else "")
        program = _program_from_group(group)
        if program not in {"st", "la"}:
            continue

        class_value = _class_from_group(group)
        if not class_value:
            for header in container.select(".res-class-hdr"):
                header_text = clean_text(header.get_text(" ", strip=True))
                if "Класс участия" in header_text:
                    class_value = parse_class_value(header_text)
                    break

        skr_text = ""
        for header in container.select(".res-class-hdr"):
            header_text = clean_text(header.get_text(" ", strip=True))
            if "СКРкл" in header_text:
                skr_text = header_text
                break

        parsed = parse_skr_class_line(skr_text)
        if not parsed.get("skr_class"):
            continue

        candidate = {
            "class": class_value,
            "skr_class": parsed["skr_class"],
            "norm_status": parsed.get("norm_status", ""),
        }
        fallback_by_program.setdefault(program, candidate)

        current_class = clean_text(profile.get(f"class_{program}"))
        if current_class and class_value and current_class.upper() == class_value.upper():
            exact_by_program[program] = candidate

    for program in ("st", "la"):
        candidate = exact_by_program.get(program) or fallback_by_program.get(program)
        if not candidate:
            continue
        profile[f"skr_class_{program}"] = candidate["skr_class"]
        if candidate.get("norm_status"):
            profile[f"norm_status_{program}"] = candidate["norm_status"]


def parse_profile_html(html: str) -> dict[str, str]:
    soup = BeautifulSoup(html, "html.parser")
    profile: dict[str, Any] = {}

    for prop in soup.select(".res-dancer-info-prop"):
        label_node = prop.select_one(".res-dancer-info-label")
        value_node = prop.select_one(".res-dancer-info")
        label = clean_text(label_node.get_text(" ", strip=True) if label_node else "")
        value = clean_text(value_node.get_text(" ", strip=True) if value_node else "")
        full_text = clean_text(prop.get_text(" ", strip=True))
        label_norm = (label or full_text).lower().replace("ё", "е")

        if "тренеры st" in label_norm:
            profile["coaches_st"] = value
        elif "тренеры la" in label_norm:
            profile["coaches_la"] = value
        elif "соло st" in label_norm or "соло st" in full_text.lower():
            profile["class_st"] = parse_class_value(value or full_text)
        elif "соло la" in label_norm or "соло la" in full_text.lower():
            profile["class_la"] = parse_class_value(value or full_text)
        elif not label and full_text and "," in full_text and "тренер" not in label_norm:
            parts = [clean_text(part) for part in full_text.split(",")]
            parts = [part for part in parts if part]
            if len(parts) >= 2:
                profile.setdefault("club", parts[0])
                profile.setdefault("city", parts[1])

    _parse_class_result_containers(soup, profile)
    return clean_profile(profile)

