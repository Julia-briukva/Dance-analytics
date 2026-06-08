"""Display helpers for dance names and short dance codes."""

from __future__ import annotations

import re
from typing import Any


DANCE_SHORT_CODES = {
    "W": "W",
    "T": "T",
    "V": "V",
    "F": "F",
    "Q": "Q",
    "S": "S",
    "C": "Ch",
    "CH": "Ch",
    "CHACHACHA": "Ch",
    "R": "R",
    "P": "P",
    "J": "J",
    "МЕДЛЕННЫЙВАЛЬС": "W",
    "ВАЛЬС": "W",
    "ТАНГО": "T",
    "ВЕНСКИЙВАЛЬС": "V",
    "ФОКСТРОТ": "F",
    "КВИКСТЕП": "Q",
    "САМБА": "S",
    "ЧАЧАЧА": "Ch",
    "РУМБА": "R",
    "ПАСОДОБЛЬ": "P",
    "ДЖАЙВ": "J",
    "SLOWWALTZ": "W",
    "WALTZ": "W",
    "TANGO": "T",
    "VIENNESEWALTZ": "V",
    "FOXTROT": "F",
    "QUICKSTEP": "Q",
    "SAMBA": "S",
    "RUMBA": "R",
    "PASODOBLE": "P",
    "JIVE": "J",
}


DANCE_CODE_ORDER = {
    "W": 10,
    "T": 20,
    "V": 30,
    "F": 40,
    "Q": 50,
    "S": 60,
    "Ch": 70,
    "R": 80,
    "P": 90,
    "J": 100,
}


DANCE_RUSSIAN_NAMES = {
    "W": "Медленный вальс",
    "T": "Танго",
    "V": "Венский вальс",
    "VW": "Венский вальс",
    "F": "Медленный фокстрот",
    "Q": "Квикстеп",
    "S": "Самба",
    "Ch": "Ча-ча-ча",
    "C": "Ча-ча-ча",
    "R": "Румба",
    "P": "Пасодобль",
    "J": "Джайв",
}


def normalize_dance_code(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    compact = re.sub(r"[^A-Za-zА-Яа-яЁё]+", "", text).upper().replace("Ё", "Е")
    return DANCE_SHORT_CODES.get(compact, text)


def dance_russian_name(value: Any) -> str:
    code = normalize_dance_code(value)
    return DANCE_RUSSIAN_NAMES.get(code, str(value or "—"))


def sort_dance_codes(codes: list[str]) -> list[str]:
    return sorted(dict.fromkeys(code for code in codes if code), key=lambda code: (DANCE_CODE_ORDER.get(code, 999), code))
