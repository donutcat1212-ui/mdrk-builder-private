"""Keep each examination and its source together when partitioning by admission."""
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import re

from mdrk_builder.application.extractors import INSTRUMENTAL_START_RE, parse_first_datetime
from mdrk_builder.domain import ReviewIssue, ReviewSeverity

_LAB = re.compile(r"\b(?:оак|оам|биохим|кров|моч|лаборатор|коагул|гемоглоб|глюкоз|холестерин)", re.I)
_LAB_TITLE = re.compile(r"^(?:оак|оам|общий\s+анализ|биохимическ\w*\s+(?:анализ|исследован\w*)|коагулограмм\w*)\b", re.I)
_DATE = re.compile(r"\b\d{1,2}[./]\d{1,2}[./]\d{2,4}\b")
_FIELDS = ("provided_documents", "laboratory_results", "instrumental_results")


@dataclass(frozen=True)
class ExaminationSection:
    field: str
    text: str
    source: Path | None


def _blocks(text: str) -> Iterable[str]:
    current: list[str] = []
    dated = False
    for line in text.splitlines():
        line = line.strip()
        title = re.sub(r"^\d+[.)]\s+", "", line)
        new_test = INSTRUMENTAL_START_RE.match(title) or _LAB_TITLE.match(title)
        # A date on its own belongs to the preceding undated title.
        if current and (not line or dated and (new_test or _DATE.match(line))):
            yield "\n".join(current)
            current, dated = [], False
        if line:
            current.append(line)
            dated = dated or bool(_DATE.search(line))
    if current:
        yield "\n".join(current)


def partition_examinations(
    sections: Iterable[ExaminationSection], admission: datetime | None, discharge: datetime | None,
) -> tuple[dict[str, str], dict[str, Path], list[ReviewIssue]]:
    result: dict[str, dict[str, str]] = {name: {} for name in _FIELDS}
    origins: dict[str, list[Path]] = {}
    issues = []
    for section in sections:
        for block in _blocks(section.text):
            dates = {stamp.date() for match in _DATE.finditer(block)
                     if (stamp := parse_first_datetime(match.group())) is not None}
            target = section.field
            if len(dates) > 1:
                issues.append(ReviewIssue(
                    "examination_date_uncertain", "В блоке исследования несколько дат; проверьте раздел. Блок сохранён целиком.",
                    ReviewSeverity.WARNING, target, section.source))
            elif dates and admission:
                day = next(iter(dates))
                if day < admission.date():
                    target = "provided_documents"
                elif discharge and day > discharge.date():
                    issues.append(ReviewIssue(
                        "examination_after_discharge", "Исследование после даты выписки исключено; проверьте период.",
                        ReviewSeverity.WARNING, target, section.source))
                    continue
                elif target == "provided_documents":
                    if INSTRUMENTAL_START_RE.search(block):
                        target = "instrumental_results"
                    elif _LAB.search(block):
                        target = "laboratory_results"
                    else:
                        issues.append(ReviewIssue(
                            "examination_section_uncertain", "Проверьте раздел исследования, датированного текущей госпитализацией.",
                            ReviewSeverity.WARNING, target, section.source))
            key = " ".join(block.split()).casefold()
            result[target].setdefault(key, block)
            if section.source and section.source not in origins.setdefault(target, []):
                origins[target].append(section.source)
    provenance = {name if i == 0 else f"{name}.{i + 1}": path
                  for name, paths in origins.items() for i, path in enumerate(paths)}
    return {name: "\n\n".join(blocks.values()) for name, blocks in result.items()}, provenance, issues
