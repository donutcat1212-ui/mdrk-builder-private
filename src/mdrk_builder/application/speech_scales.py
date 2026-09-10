"""Read speech scores explicitly written in prose, including baseline/repeat pairs."""
import re
from datetime import datetime
from pathlib import Path

from mdrk_builder.domain import MdrkKind, ScaleMeasurement, SpecialistRole

_WASSERMAN = re.compile(
    r"^шкала\s+Вассермана\b.*?[-–—:]\s*(\d+(?:[.,]\d+)?)\s*(?:балл\w*)?", re.I,
)
_REPEAT = re.compile(
    r"\b(?:повт(?:орн\w*)?\.?(?:\s+оценка)?|при\s+выписке|итогов\w*)\s*[:–—-]?\s*(\d+(?:[.,]\d+)?)", re.I,
)


def speech_narrative_scores(lines: list[str], occurred_at: datetime | None, source: Path) -> list[ScaleMeasurement]:
    result = []
    for line in lines:
        match = _WASSERMAN.match(line)
        if match is None:
            continue
        repeat = _REPEAT.search(line[match.end():])
        result.append(ScaleMeasurement(
            "Шкала Вассермана Л.И.", match.group(1), None if repeat else occurred_at,
            SpecialistRole.LOGOPEDIST, source, phase=MdrkKind.INITIAL if repeat else None,
        ))
        if repeat:
            result.append(ScaleMeasurement(
                "Шкала Вассермана Л.И.", repeat.group(1), occurred_at,
                SpecialistRole.LOGOPEDIST, source, phase=MdrkKind.FINAL,
            ))
    return result
