from __future__ import annotations

import re


_ORDINALS = {
    "первый": 1,
    "первого": 1,
    "второй": 2,
    "второго": 2,
    "третий": 3,
    "третьего": 3,
    "четвертый": 4,
    "четвертого": 4,
    "пятый": 5,
    "пятого": 5,
    "шестой": 6,
    "шестого": 6,
    "седьмой": 7,
    "седьмого": 7,
    "восьмой": 8,
    "восьмого": 8,
    "девятый": 9,
    "девятого": 9,
    "десятый": 10,
    "десятого": 10,
    "одиннадцатый": 11,
    "двенадцатый": 12,
    "тринадцатый": 13,
    "четырнадцатый": 14,
    "пятнадцатый": 15,
    "шестнадцатый": 16,
    "семнадцатый": 17,
    "восемнадцатый": 18,
    "девятнадцатый": 19,
    "двадцатый": 20,
}


def normalize_query(value: str | None) -> str:
    if not value:
        return ""
    normalized = value.lower().replace("ё", "е")
    normalized = normalized.replace("№", " номер ")
    normalized = re.sub(r"(?<=\d)[-‐‑–—]?(?:й|ый|ой)\b", " ", normalized)
    normalized = re.sub(r"[^0-9a-zа-я]+", " ", normalized)
    return " ".join(normalized.split())


def parse_department_number(value: str | None) -> int | None:
    normalized = normalize_query(value)
    if not normalized:
        return None
    for token in normalized.split():
        number = _ORDINALS.get(token)
        if number is not None:
            return number
    match = re.search(r"\b(?:номер\s+)?(\d{1,4})\b", normalized)
    return int(match.group(1)) if match else None


def normalized_tokens(value: str | None) -> frozenset[str]:
    return frozenset(normalize_query(value).split())
