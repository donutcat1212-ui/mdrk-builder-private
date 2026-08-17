from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

from mdrk_builder.application.extractors import RUSSIAN_MONTHS, parse_first_datetime
from mdrk_builder.infrastructure.ooxml_reader import ParsedDocument, clean_text


_DISCHARGE_HEADING_RE = re.compile(
    r"^выписной(?:\s+\(переводной\))?\s+эпикриз$",
    re.IGNORECASE,
)
_CLINICAL_DIAGNOSIS_RE = re.compile(
    r"^заключительн\w*\s+клиническ\w*\s+диагноз\b",
    re.IGNORECASE,
)
_CONSULTATION_HEADING_RE = re.compile(
    r"^(?:консультация|осмотр)\s+(.+)$",
    re.IGNORECASE,
)
_CONSULTATION_STOP_RE = re.compile(
    r"^(?:консультация|осмотр|результат\s+осмотра|"
    r"результаты\s+медицинского\s+обследования|"
    r"применение\s+лекарственных\s+препаратов|"
    r"трансфузии|оперативные\s+вмешательства|"
    r"медицинские\s+вмешательства|дополнительные\s+сведения|"
    r"шкалы\s+при\s+выписке|лучевая\s+нагрузка|лечащ\w*\s+врач)\b",
    re.IGNORECASE,
)
_CORE_REHABILITATION_SPECIALIST_RE = re.compile(
    r"(?:невролог|врач\s+фрм|физическ\w*\s+реабилитац|физическ\w*\s+терапевт|"
    r"нейропсихолог|патопсихолог|логопед|эргореабилит|эрготерапевт)",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class ExtractedConsultation:
    text: str
    occurred_at: datetime | None = None


def document_lines(document: ParsedDocument) -> list[str]:
    return [
        cleaned
        for raw_line in document.text.splitlines()
        if (cleaned := clean_text(raw_line))
    ]


def _extract_labeled_block(
    document: ParsedDocument,
    *,
    starts: tuple[str, ...],
    stops: tuple[str, ...],
) -> str:
    lines = document_lines(document)
    start_re = re.compile(r"^(?:" + "|".join(starts) + r")\b\s*[:–—.-]?\s*(.*)$", re.IGNORECASE)
    stop_re = re.compile(r"^(?:" + "|".join(stops) + r")\b", re.IGNORECASE)
    for index, line in enumerate(lines):
        match = start_re.match(line)
        if match is None:
            continue
        values = [clean_text(match.group(1))] if clean_text(match.group(1)) else []
        for following in lines[index + 1 :]:
            if stop_re.match(following):
                break
            values.append(following)
        return "\n".join(dict.fromkeys(value for value in values if value))
    return ""


def extract_discharge_header(document: ParsedDocument) -> str:
    lines = document_lines(document)
    start = next(
        (index + 1 for index, line in enumerate(lines) if _DISCHARGE_HEADING_RE.match(line)),
        None,
    )
    if start is None:
        return ""
    end = next(
        (
            index
            for index in range(start, len(lines))
            if _CLINICAL_DIAGNOSIS_RE.match(lines[index])
        ),
        None,
    )
    if end is None:
        return ""
    return "\n".join(lines[start:end])


def extract_summary_discharge_datetime(document: ParsedDocument) -> datetime | None:
    for line in document_lines(document):
        if "период нахождения" not in line.casefold():
            continue
        match = re.search(
            r"\bпо\s+[«\" ]*(\d{1,2})[»\" ]+"
            r"(январ[ья]|феврал[ья]|марта?|апрел[ья]|ма[йя]|июн[ья]|июл[ья]|"
            r"август[а]?|сентябр[ья]|октябр[ья]|ноябр[ья]|декабр[ья])\s+"
            r"((?:19|20)\d{2})\s*г?\.?\s*(?:время\s*:\s*)?"
            r"([0-2]?\d)?\s*:?\s*([0-5]\d)?",
            line,
            re.IGNORECASE,
        )
        if match is None:
            continue
        month_text = match.group(2).casefold()
        month = next(
            value
            for prefix, value in RUSSIAN_MONTHS.items()
            if month_text.startswith(prefix)
        )
        hour = int(match.group(4) or 0)
        minute = int(match.group(5) or 0)
        if hour > 23:
            continue
        return datetime(
            int(match.group(3)),
            month,
            int(match.group(1)),
            hour,
            minute,
        )
    return None


def extract_complaints(document: ParsedDocument) -> str:
    return _extract_labeled_block(
        document,
        starts=(r"жалоб\w*(?:\s+\(на\s+момент\s+поступления\)|\s+при\s+поступлении)?",),
        stops=(r"анамнез\s+заболевания",),
    )


def extract_provided_documents(document: ParsedDocument) -> str:
    return _extract_labeled_block(
        document,
        starts=(r"пациент\w*\s+представлен\w*\s+необходим\w*\s+для\s+госпитализаци\w*\s+документ\w*",),
        stops=(r"физикальн\w*\s+(?:обследовани\w*|исследовани\w*)",),
    )


def extract_physical_exam(document: ParsedDocument) -> str:
    return _extract_labeled_block(
        document,
        starts=(r"физикальн\w*\s+(?:обследовани\w*|исследовани\w*)",),
        stops=(
            r"неврологическ\w*\s+(?:статус|осмотр)",
            r"локальн\w*\s+статус",
            r"шкалы\s+при\s+поступлении",
            r"реабилитационн\w*\s+диагноз",
        ),
    )


def extract_neurological_status(document: ParsedDocument) -> str:
    return _extract_labeled_block(
        document,
        starts=(
            r"неврологическ\w*\s+(?:статус|осмотр)",
            r"статус\s+неврологическ\w*",
        ),
        stops=(
            r"локальн\w*\s+статус",
            r"шкалы\s+при\s+поступлении",
            r"реабилитационн\w*\s+диагноз",
            r"план\s+обследования",
            r"план\s+лечения",
        ),
    )


def extract_local_status(document: ParsedDocument) -> str:
    return _extract_labeled_block(
        document,
        starts=(r"локальн\w*\s+статус",),
        stops=(
            r"шкалы\s+при\s+поступлении",
            r"реабилитационн\w*\s+диагноз",
            r"проведен\w*\s+обследования",
        ),
    )


def extract_laboratory_results(document: ParsedDocument) -> str:
    return _extract_labeled_block(
        document,
        starts=(r"лабораторн\w*\s+исследован\w*",),
        stops=(
            r"инструментальн\w*\s+исследован\w*",
            r"(?:консультация|осмотр)\s+.+",
            r"применение\s+лекарственных\s+препаратов",
            r"трансфузии",
            r"оперативные\s+вмешательства",
            r"медицинские\s+вмешательства",
            r"лучевая\s+нагрузка",
        ),
    )


def extract_instrumental_results(document: ParsedDocument) -> str:
    return _extract_labeled_block(
        document,
        starts=(r"инструментальн\w*\s+исследован\w*",),
        stops=(
            r"(?:консультация|осмотр)\s+.+",
            r"применение\s+лекарственных\s+препаратов",
            r"трансфузии",
            r"оперативные\s+вмешательства",
            r"медицинские\s+вмешательства",
            r"лучевая\s+нагрузка",
        ),
    )


def extract_medical_examination_summary(document: ParsedDocument) -> str:
    return _extract_labeled_block(
        document,
        starts=(r"результаты\s+медицинского\s+обследования",),
        stops=(
            r"применение\s+лекарственных\s+препаратов",
            r"трансфузии",
            r"оперативные\s+вмешательства",
            r"медицинские\s+вмешательства",
        ),
    )


def extract_discharge_scale_values(document: ParsedDocument) -> dict[str, str]:
    lines = document_lines(document)
    start = next(
        (
            index + 1
            for index, line in enumerate(lines)
            if re.match(r"^шкалы\s+при\s+выписке\s*:?$", line, re.IGNORECASE)
        ),
        None,
    )
    if start is None:
        return {}
    result: dict[str, str] = {}
    for line in lines[start:]:
        if re.match(r"^состояние\s+при\s+выписке\b", line, re.IGNORECASE):
            break
        match = re.match(r"^(.+?)\s*[:–—-]\s*(.+)$", line)
        if match is not None:
            result[clean_text(match.group(1))] = clean_text(match.group(2))
    return result


def extract_other_consultations(document: ParsedDocument) -> tuple[ExtractedConsultation, ...]:
    lines = document_lines(document)
    consultations: list[ExtractedConsultation] = []
    for index, line in enumerate(lines):
        match = _CONSULTATION_HEADING_RE.match(line)
        if match is None or _CORE_REHABILITATION_SPECIALIST_RE.search(match.group(1)):
            continue
        values = [line]
        for following in lines[index + 1 :]:
            if _CONSULTATION_STOP_RE.match(following):
                break
            values.append(following)
        text = "\n".join(values)
        consultation = ExtractedConsultation(text, parse_first_datetime(text))
        if consultation.text not in {item.text for item in consultations}:
            consultations.append(consultation)
    return tuple(consultations)


def extract_radiation_exposure(document: ParsedDocument) -> str:
    for line in document_lines(document):
        match = re.match(
            r"^лучевая\s+нагрузка\s*[:–—-]\s*(.+)$",
            line,
            re.IGNORECASE,
        )
        if match is None:
            continue
        value = clean_text(match.group(1))
        value = re.sub(r"\bм[зс]в\b", "мЗв", value, flags=re.IGNORECASE)
        return value
    return ""


def extract_signature_block(document: ParsedDocument) -> str:
    lines = document_lines(document)
    start = next(
        (
            index
            for index, line in enumerate(lines)
            if re.match(r"^лечащ\w*\s+врач", line, re.IGNORECASE)
        ),
        None,
    )
    if start is None:
        return ""
    values: list[str] = []
    for line in lines[start : start + 12]:
        if values and re.match(
            r"^(?:[\"«]\d{1,2}[\"»]|я,?\s+.+\s+получил)",
            line,
            re.IGNORECASE,
        ):
            break
        if not re.fullmatch(r"\s*\|(?:\s*\|)*\s*", line):
            values.append(line)
    return "\n".join(values)
