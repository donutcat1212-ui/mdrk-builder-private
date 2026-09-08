from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

from mdrk_builder.application.extractors import (
    RUSSIAN_MONTHS, SECTION_STARTS, SECTION_STOP, parse_first_datetime,
    extract_admission_datetime, extract_clinical_sections,
)
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
    r"обоснование|план\s+обследования|план\s+лечения|"
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
_ICF_PROFILE_HEADING_RE = re.compile(
    r"^\s*(?:\|\s*)*(?:мкф\s+(?:категориальн\w*\s+профиль|категории|классификатор)\b|"
    r"(?:структур\w*|функци\w*|активность(?:\s+и\s+участие)?|участие|"
    r"факторы\s+(?:окружающей\s+среды|среды)|личностные\s+факторы)\s*\|)",
    re.IGNORECASE,
)
_SOURCE_SECTION_BOUNDARY_RE = re.compile(
    r"^(?:\d+(?:\.\d+)*[.)]?\s*)?(?:обоснование\b|"
    r"заключительн\w*\s+клиническ\w*\s+диагноз\b|"
    r"жалобы\b|пациент\w*\s+представлен\w*\b|"
    r"физикальн\w*\s+(?:обследован\w*|исследован\w*)\b|неврологическ\w*\s+статус\b|"
    r"локальн\w*\s+статус\b|шкалы\s+при\b|"
    r"(?:заключение|консультация)\b|рекомендации\b|состояние при выписке\b|"
    r"лечащ\w*\s+врач\b|заведующ\w*\s+отделени\w*\b)",
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
    source_lines: list[str] | None = None,
    use_common_stops: bool = True,
) -> str:
    lines = document_lines(document) if source_lines is None else source_lines
    start_re = re.compile(r"^(?:\d+(?:\.\d+)*[.)]?\s*)?(?:" + "|".join(starts) + r")\b\s*[:–—.-]?\s*(.*)$", re.IGNORECASE)
    stop_re = re.compile(r"^(?:" + "|".join(stops) + r")\b", re.IGNORECASE) if stops else None
    for index, line in enumerate(lines):
        match = start_re.match(line)
        if match is None:
            continue
        values = [clean_text(match.group(1))] if clean_text(match.group(1)) else []
        for following in lines[index + 1 :]:
            # An ICF table can follow the examination without a separate
            # rehabilitation-diagnosis paragraph. Its flattened rows are not prose.
            if (
                (stop_re is not None and stop_re.match(following))
                or _ICF_PROFILE_HEADING_RE.match(following)
                or _SOURCE_SECTION_BOUNDARY_RE.match(following)
                or (use_common_stops and SECTION_STOP.match(following))
            ):
                break
            values.append(following)
        return "\n".join(value for value in values if value)
    return ""


def extract_discharge_clinical_sections(document: ParsedDocument) -> dict[str, str]:
    """Read only the primary-examination fields owned by the discharge template."""
    fields = (
        "clinical_diagnosis", "disease_history", "life_history",
        "movement_regimen", "diet", "risks", "limitations",
    )
    result = {
        name: _extract_labeled_block(document, starts=SECTION_STARTS[name], stops=(),
                                     use_common_stops=name != "life_history")
        for name in fields
    }
    # Keep the diagnosis block in source order, including repeated labels.
    # A primary examination may list several separate accompanying diseases.
    lines = document_lines(document)
    heading = re.compile(r"^(?:(?:заключительный\s+)?клинический\s+диагноз|диагноз)\s*[:–—.-]?\s*(.*)$", re.I)
    start = next((i for i, line in enumerate(lines) if heading.match(line)
                  or re.match(r"^основное\s+заболевание\b", line, re.I)), None)
    if start is not None:
        match = heading.match(lines[start])
        values = [match.group(1)] if match else [lines[start]]
        for line in lines[start + 1:]:
            if SECTION_STOP.match(line) or _SOURCE_SECTION_BOUNDARY_RE.match(line) or _ICF_PROFILE_HEADING_RE.match(line):
                break
            values.append(line)
        result["clinical_diagnosis"] = "\n".join(value for value in values if value)
    # Regimen/diet may be embedded in the primary treatment-plan heading.
    common = extract_clinical_sections(document)
    for name in ("movement_regimen", "diet"):
        if not result[name]:
            result[name] = common.get(name, "")
    return result


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
    explicit = _extract_labeled_block(
        document,
        starts=(r"пациент\w*\s+представлен\w*\s+необходим\w*\s+для\s+госпитализаци\w*\s+документ\w*",),
        stops=(r"физикальн\w*\s+(?:обследовани\w*|исследовани\w*)",),
    )
    if explicit:
        return explicit
    prior = _extract_labeled_block(document,
        starts=(r"выполненные\s+медицинские\s+вмешательства",),
        stops=(r"план\s+обследования", r"план\s+лечения"))
    admission = extract_admission_datetime(document)
    dates = [value for line in prior.splitlines() if (value := parse_first_datetime(line)) is not None]
    if admission and dates and all(value.date() < admission.date() for value in dates):
        return prior
    return ""


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
            values.append(re.sub(r"\s*\|\s*", " ", line).strip())
    return "\n".join(values)


def extract_discharge_final_fields(document: ParsedDocument, *, final_context: bool = False) -> dict[str, str]:
    """Read actual treatment/discharge data from a MIS or departmental summary."""
    lines = document_lines(document)
    headings = {
        'medications': r'применение лекарственных препаратов[^:]*',
        'transfusions': r'трансфузии[^:]*',
        'operations': r'оперативные вмешательства[^:]*',
        'additional_information': r'дополнительные сведения',
        'discharge_condition': r'состояние при выписке[^:]*',
        'discharge_neurological_status': r'неврологический статус',
        'goal_result': r'(?:цель,? поставленная на этап медицинской реабилитации|результат достижения цели)',
        'work_capacity': r'трудоспособность[^:]*',
        'recommendations': r'рекомендации',
    }
    boundary = re.compile(r'^(?:' + '|'.join(headings.values()) +
        r'|шкалы при выписке|медицинские вмешательства|лучевая нагрузка|'
        r'факторы риска|факторы,? ограничивающие|реабилитационный потенциал|'
        r'цель,? поставленная|лечащ\w* врач|заведующ\w* отделением|'
        r'я,?\s|[«"]\d{1,2}[»"]|выписной эпикриз получен)', re.I)
    discharge_start = next((i for i, line in enumerate(lines)
                            if re.match(r'^состояние при выписке', line, re.I)), len(lines))
    treatment_start = next((i for i, line in enumerate(lines)
                            if re.match(r'^проведен\w* обследования', line, re.I)), len(lines))
    result = {}
    for name, heading in headings.items():
        start = discharge_start if name in {'discharge_neurological_status', 'work_capacity', 'recommendations', 'goal_result'} else treatment_start
        if name == 'discharge_condition':
            start = 0
        elif final_context and name in {'medications', 'recommendations', 'work_capacity', 'goal_result'}:
            start = 0
        pattern = re.compile(r'^' + heading + r'\s*:\s*(.*)$', re.I)
        for i in range(start, len(lines)):
            match = pattern.match(lines[i])
            if match is None:
                continue
            values = [match.group(1)] if match.group(1).strip() else []
            for line in lines[i + 1:]:
                if boundary.match(line):
                    break
                if line != '.' and not re.fullmatch(r'\s*\|(?:\s*\|)*\s*', line):
                    values.append(line)
            result[name] = '\n'.join(values).strip()
            break
    return result


def update_header_period(header: str, admission: datetime | None, discharge: datetime | None) -> str:
    """Replace only hospitalization dates; retain all other MIS header fields."""
    if admission is None or discharge is None:
        return header
    lines = header.splitlines()
    period = f"с {admission:%d.%m.%Y %H:%M} по {discharge:%d.%m.%Y %H:%M}"
    found = False
    count_found = False
    for i, line in enumerate(lines):
        if re.match(r"^Период нахождения", line, re.I):
            prefix = line.split(":", 1)[0]
            lines[i] = prefix + ": " + period
            found = True
        elif re.match(r"^Дата (?:выписки|поступления)", line, re.I):
            value = discharge if "выписки" in line.casefold() else admission
            lines[i] = line.split(":", 1)[0] + f": {value:%d.%m.%Y %H:%M}"
        elif re.match(r"^Количество дней нахождения", line, re.I):
            count_found = True
            lines[i] = line.split(":", 1)[0] + f": {max(1, (discharge.date() - admission.date()).days)}"
    if not found:
        lines.append("Период нахождения в стационаре: " + period)
    if not count_found:
        lines.append(f"Количество дней нахождения в медицинской организации: {max(1, (discharge.date() - admission.date()).days)}")
    return "\n".join(lines)
