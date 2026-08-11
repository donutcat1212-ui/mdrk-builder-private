from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path


DIAGNOSTIC_DUPLICATE_THRESHOLD = 0.88

_SPACE_RE = re.compile(r"\s+")
_TOKEN_RE = re.compile(r"[0-9a-zа-яё]+", re.IGNORECASE)
_NUMBER_RE = re.compile(r"(?<!\d)\d+(?:[.,]\d+)?(?!\d)")
_SEGMENT_SPLIT_RE = re.compile(r"(?:\r?\n)+|(?<=[.!?;])\s+")
_EMPTY_UPDATE_RE = re.compile(
    r"^(?:(?:без\s+(?:существенных\s+)?(?:изменений|дополнений))|"
    r"(?:(?:по|согласно)\s+ипмр))"
    r"(?:\s+(?:(?:без\s+(?:существенных\s+)?"
    r"(?:изменений|дополнений))|(?:(?:по|согласно)\s+ипмр)))*$",
    re.IGNORECASE,
)
_EMPTY_UPDATE_LABEL_RE = re.compile(
    r"^(?:(?:дополнения?|изменения)\s+к\s+)?"
    r"(?:анамнезу(?:\s+(?:заболевания|жизни))?|"
    r"клиническому\s+диагнозу|диагнозу|"
    r"лабораторным\s+исследованиям|инструментальным\s+исследованиям)\s+",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class ClinicalTextObservation:
    text: str
    occurred_at: datetime | None
    document_type: str
    source: Path


@dataclass(frozen=True, slots=True)
class ComposedClinicalText:
    text: str
    source: Path | None


def _normalized(value: str) -> str:
    return " ".join(_TOKEN_RE.findall(value.casefold().replace("ё", "е")))


def _clean(value: str) -> str:
    return "\n".join(
        _SPACE_RE.sub(" ", line).strip()
        for line in value.splitlines()
        if _SPACE_RE.sub(" ", line).strip()
    )


def is_empty_clinical_update(value: str) -> bool:
    """Return true only for status phrases that carry no clinical content."""

    normalized = _normalized(value)
    normalized = _EMPTY_UPDATE_LABEL_RE.sub("", normalized)
    return not normalized or bool(_EMPTY_UPDATE_RE.fullmatch(normalized))


def _meaningful_segments(value: str) -> list[str]:
    cleaned = _clean(value)
    if not cleaned:
        return []
    segments = [part.strip(" \t,;:-") for part in _SEGMENT_SPLIT_RE.split(cleaned)]
    return [part for part in segments if part and not is_empty_clinical_update(part)]


def clean_clinical_update(value: str) -> str:
    """Drop standalone "no changes" markers while preserving adjacent additions."""

    return " ".join(_meaningful_segments(value)).strip()


def _similarity(left: str, right: str) -> float:
    if not left or not right:
        return 0.0
    return SequenceMatcher(None, left, right, autojunk=False).ratio()


def _numbers(value: str) -> tuple[str, ...]:
    return tuple(item.replace(",", ".") for item in _NUMBER_RE.findall(value))


def _unmatched_candidate_tokens(candidate: str, known: str) -> str:
    candidate_tokens = _TOKEN_RE.findall(candidate)
    known_tokens = _TOKEN_RE.findall(known)
    if not candidate_tokens or not known_tokens:
        return candidate
    matcher = SequenceMatcher(
        None,
        [token.casefold().replace("ё", "е") for token in known_tokens],
        [token.casefold().replace("ё", "е") for token in candidate_tokens],
        autojunk=False,
    )
    novel: list[str] = []
    for tag, _left_start, _left_end, right_start, right_end in matcher.get_opcodes():
        if tag in {"insert", "replace"}:
            novel.extend(candidate_tokens[right_start:right_end])
    return " ".join(novel).strip()


def _inserted_candidate_text(
    candidate: str,
    operations: list[tuple[str, int, int, int, int]],
) -> str:
    matches = list(_TOKEN_RE.finditer(candidate))
    parts: list[str] = []
    for tag, _left_start, _left_end, right_start, right_end in operations:
        if tag != "insert" or right_start >= right_end:
            continue
        start = matches[right_start].start()
        end = matches[right_end].start() if right_end < len(matches) else len(candidate)
        part = candidate[start:end].strip(" \t,;:-")
        if part:
            parts.append(part)
    return " ".join(parts).strip()


def extract_novel_clinical_text(
    candidate: str,
    known: str,
    *,
    duplicate_threshold: float | None = None,
) -> str:
    """Return only the meaningful addition in a copied follow-up section.

    Exact copies and standalone status phrases disappear.  Diagnostic sections
    may opt into fuzzy matching because punctuation and small template edits are
    common there.  When a copied block has a genuinely new sentence, the new
    sentence is preserved instead of the whole repeated block.
    """

    candidate_clean = clean_clinical_update(candidate)
    known_clean = clean_clinical_update(known)
    if not candidate_clean:
        return ""
    if not known_clean:
        return candidate_clean

    candidate_normalized = _normalized(candidate_clean)
    known_normalized = _normalized(known_clean)
    if candidate_normalized == known_normalized or candidate_normalized in known_normalized:
        return ""

    # The common diary pattern is a byte-for-byte copy followed by one phrase.
    literal_start = candidate_clean.casefold().find(known_clean.casefold())
    if literal_start >= 0:
        before = candidate_clean[:literal_start].strip().rstrip(" ,;:-")
        after = candidate_clean[literal_start + len(known_clean) :].strip().lstrip(" ,;:-")
        return " ".join(part for part in (before, after) if part)

    # A copied diagnostic line often loses punctuation and receives one value
    # at the end. Token alignment still exposes a pure insertion, so retain
    # only that inserted tail instead of duplicating the whole baseline.
    candidate_tokens = _TOKEN_RE.findall(candidate_clean)
    known_tokens = _TOKEN_RE.findall(known_clean)
    token_matcher = SequenceMatcher(
        None,
        [token.casefold().replace("ё", "е") for token in known_tokens],
        [token.casefold().replace("ё", "е") for token in candidate_tokens],
        autojunk=False,
    )
    token_operations = token_matcher.get_opcodes()
    if (
        any(tag == "insert" for tag, *_ in token_operations)
        and all(tag in {"equal", "insert"} for tag, *_ in token_operations)
    ):
        inserted = _inserted_candidate_text(candidate_clean, token_operations)
        if inserted:
            return inserted

    candidate_segments = _meaningful_segments(candidate_clean)
    known_segments = _meaningful_segments(known_clean)
    threshold = duplicate_threshold if duplicate_threshold is not None else 0.98
    novel_segments: list[str] = []
    for segment in candidate_segments:
        normalized_segment = _normalized(segment)
        if not normalized_segment or normalized_segment in known_normalized:
            continue
        best_known, similarity = max(
            (
                (item, _similarity(normalized_segment, _normalized(item)))
                for item in known_segments
            ),
            key=lambda item: item[1],
            default=("", 0.0),
        )
        # A changed result is clinical content even when the surrounding copied
        # laboratory sentence is almost identical (for example Hb 120 -> 110).
        if similarity < threshold or _numbers(segment) != _numbers(best_known):
            novel_segments.append(segment)
    if novel_segments:
        return " ".join(novel_segments)

    if duplicate_threshold is None:
        return ""

    if len(candidate_tokens) >= len(known_tokens) + 3:
        return _unmatched_candidate_tokens(candidate_clean, known_clean)
    return ""


def compose_clinical_timeline(
    observations: list[ClinicalTextObservation],
    *,
    include_updates: bool,
    duplicate_threshold: float | None = None,
) -> ComposedClinicalText:
    """Compose a primary baseline and dated, de-duplicated later additions."""

    meaningful = [
        (item, clean_clinical_update(item.text))
        for item in observations
        if clean_clinical_update(item.text)
    ]
    if not meaningful:
        return ComposedClinicalText("", None)

    initial = [item for item in meaningful if item[0].document_type == "initial"]
    baseline_item, baseline_text = (initial or meaningful)[0]
    if not include_updates:
        return ComposedClinicalText(baseline_text, baseline_item.source)

    baseline_index = meaningful.index((baseline_item, baseline_text))
    known = baseline_text
    lines = [baseline_text]
    latest_source = baseline_item.source
    for item, value in meaningful[baseline_index + 1 :]:
        novel = extract_novel_clinical_text(
            value,
            known,
            duplicate_threshold=duplicate_threshold,
        )
        if not novel:
            continue
        label = (
            f"(Дополнение от {item.occurred_at:%d.%m.%Y %H:%M}):"
            if item.occurred_at is not None
            else "(Дополнение, дата и время не найдены):"
        )
        lines.append(f"{label} {novel}")
        known = f"{known}\n{novel}"
        latest_source = item.source
    return ComposedClinicalText("\n".join(lines), latest_source)
