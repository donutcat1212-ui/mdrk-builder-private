from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ScaleDefinition:
    key: str
    canonical_name: str
    aliases: tuple[str, ...]
    bounds: tuple[int, int] | None = None
    display_name: str = ""


SCALE_REGISTRY = (
    ScaleDefinition(
        "rivermead",
        "индекс мобильности ривермид",
        ("ривермид",),
        (0, 15),
    ),
    ScaleDefinition(
        "rankin",
        "модифицированная шкала рэнкина",
        ("ренкин", "рэнкин", "rankin"),
        (0, 6),
        "Модифицированная шкала Рэнкина",
    ),
    ScaleDefinition("barthel", "индекс бартел", ("бартел",), (0, 100)),
    ScaleDefinition(
        "shrm",
        "шкала реабилитационной маршрутизации",
        ("реабилитационной маршрутизации", "шрм"),
        (0, 6),
    ),
    ScaleDefinition("berg", "шкала баланса берга", ("берг",), (0, 56)),
    ScaleDefinition("moca", "moca", ("moca", "монреальск",), (0, 30)),
    ScaleDefinition(
        "fac",
        "fac",
        ("fac", "функциональная категория передвижения"),
        (0, 5),
    ),
    ScaleDefinition("arat", "arat", ("arat",), (0, 57)),
    ScaleDefinition(
        "tis",
        "tis",
        ("tis", "ухудшения координации торса"),
        (0, 23),
    ),
    ScaleDefinition(
        "vas",
        "ваш",
        ("ваш", "визуально аналоговая"),
        (0, 10),
    ),
    ScaleDefinition("hauser", "шкала хаузера", ("хаузер",), (0, 9)),
    ScaleDefinition("tinetti", "шкала тинетти", ("тинетт",)),
)


def canonical_scale_key(value: str) -> str:
    normalized = _normalized_text(value, strip_punctuation=False)
    searchable = _normalized_text(value, strip_punctuation=True)
    definition = _definition_for(searchable)
    return definition.key if definition is not None else normalized


def canonical_scale_name(value: str) -> str:
    normalized = _normalized_text(value, strip_punctuation=True)
    definition = _definition_for(normalized)
    return definition.canonical_name if definition is not None else normalized


def canonical_scale_label(value: str) -> str:
    normalized = " ".join(value.split())
    definition = _definition_for(_normalized_text(value, strip_punctuation=True))
    if definition is not None and definition.display_name:
        return definition.display_name
    return normalized


def scale_bounds(value: str) -> tuple[int, int] | None:
    definition = _definition_for(_normalized_text(value, strip_punctuation=True))
    return definition.bounds if definition is not None else None


def numeric_scale_value(value: str) -> float | None:
    match = re.fullmatch(
        r"\s*(-?\d+(?:[.,]\d+)?)\s*(?:балл\w*|б\.)?\s*",
        value,
        re.IGNORECASE,
    )
    return float(match.group(1).replace(",", ".")) if match else None


def _definition_for(normalized: str) -> ScaleDefinition | None:
    return next(
        (
            definition
            for definition in SCALE_REGISTRY
            if any(alias in normalized for alias in definition.aliases)
        ),
        None,
    )


def _normalized_text(value: str, *, strip_punctuation: bool) -> str:
    normalized = " ".join(value.casefold().replace("ё", "е").split())
    if strip_punctuation:
        normalized = re.sub(r"[^0-9a-zа-я]+", " ", normalized)
    return " ".join(normalized.split())
