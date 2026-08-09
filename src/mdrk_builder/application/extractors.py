from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, time
from pathlib import Path

from mdrk_builder.domain import (
    IcfQualifier,
    PatientIdentity,
    Procedure,
    ScaleMeasurement,
    SpecialistRole,
)
from mdrk_builder.infrastructure.ooxml_reader import ParsedDocument, ParsedRow, clean_text


RUSSIAN_MONTHS = {
    "январ": 1,
    "феврал": 2,
    "март": 3,
    "апрел": 4,
    "мая": 5,
    "май": 5,
    "июн": 6,
    "июл": 7,
    "август": 8,
    "сентябр": 9,
    "октябр": 10,
    "ноябр": 11,
    "декабр": 12,
}
DATE_NUMERIC_RE = re.compile(r"(?<!\d)([0-3]?\d)[./-]([01]?\d)[./-]((?:19|20)\d{2}|\d{2})(?!\d)")
DATE_TEXT_RE = re.compile(
    r"[\"«_ ]*([0-3]?\d)[\"»_ ]+"
    r"(январ[ья]|феврал[ья]|марта?|апрел[ья]|ма[йя]|июн[ья]|июл[ья]|август[а]?|"
    r"сентябр[ья]|октябр[ья]|ноябр[ья]|декабр[ья])\s+((?:19|20)\d{2})",
    re.IGNORECASE,
)
TIME_COLON_RE = re.compile(r"(?<!\d)([0-2]?\d):([0-5]\d)(?!\d)")
TIME_DOT_RE = re.compile(r"(?<![\d.])([0-2]?\d)\.([0-5]\d)(?!\d|\.\d)")
TIME_WORD_RE = re.compile(r"([0-2]?\d)\s*час\w*\D{0,12}([0-5]?\d)\s*мин", re.IGNORECASE)
SHORT_DATE_RE = re.compile(r"(?<!\d)([0-3]?\d)[./]([01]?\d)(?![./]\d)")
ICF_CODE_RE = re.compile(r"^(?:[bsdeе]\d[\w.]*|pf\d*)$", re.IGNORECASE)
QUALIFIER_RE = re.compile(r"^([0-4])\s*(\+)?$")
PROCEDURE_CODE_RE = re.compile(r"(?=(?:^|\s)([AАBВ]\d{2}(?:\.\d+){2,5}))")
PHYSICIAN_SCALE_TOKENS = (
    "ривермид",
    "рэнкин",
    "rankin",
    "nrs 2002",
    "бартел",
    "реабилитационной маршрутизации",
)
PHYSICIAN_NARRATIVE_SCALES = (
    (
        "Индекс мобильности Ривермид",
        r"^Индекс\s+мобильности\s+Ривермид\s*[:–—-]\s*(.+)$",
    ),
    (
        "Модифицированная шкала Рэнкина",
        r"^Модифицированная\s+шкала\s+Р[еэ]нкин\w*\s*[:–—-]\s*(.+)$",
    ),
    ("NRS 2002", r"^NRS\s*[-–—]?\s*2002\s*[:–—-]\s*(.+)$"),
    ("Шкала Бартел", r"^Шкала\s+Бартел\w*\s*[:–—-]\s*(.+)$"),
)


def _canonical_scale_name(value: str) -> str:
    name = clean_text(value)
    if re.match(r"^Модифицированная\s+шкала\s+Р[еэ]нкин\w*$", name, re.IGNORECASE):
        return "Модифицированная шкала Рэнкина"
    return name


def _year(raw: str) -> int:
    value = int(raw)
    return value + 2000 if value < 100 else value


def _month(raw: str) -> int:
    key = raw.casefold()
    for prefix, value in RUSSIAN_MONTHS.items():
        if key.startswith(prefix):
            return value
    raise ValueError(raw)


def _time_near(text: str, start: int, end: int, default: time = time(0, 0)) -> time:
    window_start = max(0, start - 40)
    window = text[window_start : min(len(text), end + 100)]
    time_matches = [
        match
        for pattern in (TIME_WORD_RE, TIME_COLON_RE, TIME_DOT_RE)
        for match in pattern.finditer(window)
    ]
    if time_matches:
        absolute_end = end - window_start
        match = min(
            time_matches,
            key=lambda item: (
                item.start() < absolute_end,
                abs(item.start() - absolute_end),
            ),
        )
        hour, minute = int(match.group(1)), int(match.group(2))
        if hour <= 23:
            return time(hour, minute)
    return default


def _date_matches(text: str):
    normalized = text.replace("_", " ")
    for match in DATE_NUMERIC_RE.finditer(normalized):
        try:
            yield match, date(_year(match.group(3)), int(match.group(2)), int(match.group(1)))
        except ValueError:
            continue
    for match in DATE_TEXT_RE.finditer(normalized):
        try:
            yield match, date(int(match.group(3)), _month(match.group(2)), int(match.group(1)))
        except ValueError:
            continue


def parse_first_datetime(text: str, *, default_time: time = time(0, 0)) -> datetime | None:
    first = next(_date_matches(text), None)
    if first is None:
        return None
    match, value = first
    return datetime.combine(value, _time_near(text, match.start(), match.end(), default_time))


def _document_lines(document: ParsedDocument) -> list[str]:
    return [
        line
        for paragraph in document.paragraphs
        for raw_line in paragraph.splitlines()
        if (line := clean_text(raw_line))
    ]


def _has_explicit_time(text: str) -> bool:
    return bool(TIME_WORD_RE.search(text) or TIME_COLON_RE.search(text) or TIME_DOT_RE.search(text))


def extract_clinical_datetime(document: ParsedDocument) -> datetime | None:
    candidates: list[tuple[int, datetime]] = []
    search_lines = _document_lines(document)[:80]
    clinical_words = ("осмотр", "прием", "консультац", "заключен", "исследован")
    for index, line in enumerate(search_lines):
        low = line.casefold()
        previous = search_lines[index - 1].casefold() if index else ""
        context = f"{previous} {low}"
        for match, value in _date_matches(line):
            near = low[max(0, match.start() - 80) : match.end() + 80]
            if "рожд" in near or "поступ" in near:
                continue
            explicit_label = "дат" in near and any(token in near for token in clinical_words)
            titled_line = index <= 12 and any(token in context for token in clinical_words)
            if not explicit_label and not titled_line and index > 4:
                continue
            score = 80 - index
            if explicit_label:
                score += 170
            elif titled_line:
                score += 90
            if _has_explicit_time(line):
                score += 35
            candidates.append((score, datetime.combine(value, _time_near(line, match.start(), match.end()))))
    filename = document.source_path.name
    for match, value in _date_matches(filename):
        candidates.append((125, datetime.combine(value, _time_near(filename, match.start(), match.end()))))
    if "выписн" in f"{filename} {' '.join(search_lines[:3])}".casefold():
        for line in search_lines[:20]:
            if "период нахождения" not in line.casefold():
                continue
            period_dates = list(_date_matches(line))
            if period_dates:
                match, value = period_dates[-1]
                candidates.append(
                    (220, datetime.combine(value, _time_near(line, match.start(), match.end())))
                )
    return max(candidates, key=lambda item: item[0])[1] if candidates else None


def _labeled_datetime(text: str, labels: tuple[str, ...]) -> datetime | None:
    for label in labels:
        match = re.search(label, text, re.IGNORECASE | re.DOTALL)
        if not match:
            continue
        window = text[match.end() : match.end() + 180]
        parsed = parse_first_datetime(window)
        if parsed is not None:
            return parsed
    return None


def extract_admission_datetime(document: ParsedDocument) -> datetime | None:
    return _labeled_datetime(
        document.text,
        (r"дата\s+и\s+время\s+поступления", r"дата\s+поступления", r"поступил[аи]?(?:сь)?"),
    )


def extract_discharge_datetime(document: ParsedDocument) -> datetime | None:
    return _labeled_datetime(
        document.text,
        (r"дата\s+и\s+время\s+выписки", r"дата\s+выписки", r"выписан[а]?"),
    )


def extract_mdrk_meeting_datetimes(document: ParsedDocument) -> list[datetime]:
    """Return explicitly scheduled MDRK meetings from administrative rows/text.

    A row can contain both an appointment date and an execution date-time.  Keep
    every parsed value and let the scanner choose the last meeting after MDRK-1;
    values with an explicit time naturally sort after a date-only duplicate.
    """

    values: list[datetime] = []
    for table in document.tables:
        for row in table.rows:
            cells = row.as_list()
            row_text = " ".join(clean_text(value) for value in cells).casefold()
            if "консилиум" not in row_text or "мдрк" not in row_text:
                continue
            for cell in cells:
                if parsed := parse_first_datetime(clean_text(cell)):
                    values.append(parsed)
    for line in _document_lines(document):
        low = line.casefold()
        if "консилиум" in low and "мдрк" in low:
            if parsed := parse_first_datetime(line):
                values.append(parsed)
    return sorted(set(values))


def extract_patient_identity(document: ParsedDocument) -> PatientIdentity:
    text = clean_text(document.text)
    name = ""
    name_token = r"[А-ЯЁ][А-ЯЁа-яё-]+"
    name_patterns = (
        rf"ФИО\s+пациента\s*:\s*({name_token}(?:\s+{name_token}){{2}})",
        rf"Фамилия,\s*имя,\s*отчество(?:\s*\(при\s+наличии\))?(?:\s+пациента)?\s*:?\s*({name_token}(?:\s+{name_token}){{2}})",
        rf"Пациент(?:ка)?\s+({name_token}(?:\s+{name_token}){{2}})",
    )
    for pattern in name_patterns:
        match = re.search(pattern, text)
        if match:
            name = clean_text(match.group(1))
            break

    birth_date = None
    birth_match = re.search(r"дата\s+рождения\s*:?\s*(.{0,70})", text, re.IGNORECASE)
    if birth_match:
        parsed = parse_first_datetime(birth_match.group(1))
        birth_date = parsed.date() if parsed else None

    record_number = ""
    record_patterns = (
        r"Номер\s+ИБ\s*:\s*((?:СКП\s*№?\s*)?[А-ЯA-Z]*\s*\d+\s*/\s*\d{2,4})",
        r"Номер\s+медицинской\s+карты\s*(?:пациента)?\s*(?:№\s*)?((?:СКП\s*)?[А-ЯA-Z]*\s*\d+\s*/\s*\d{2,4})",
        r"медицинск(?:ой|ая)\s+карт\w*[^№]{0,80}№\s*((?:СКП\s*)?[А-ЯA-Z]*\s*\d+\s*/\s*\d{2,4})",
        r"\bСКП\s*:?\s*(\d+\s*/\s*\d{2,4})",
        r"№\s*((?:СКП\s*)?\d+\s*/\s*\d{2,4})",
    )
    for pattern in record_patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            raw_record = clean_text(match.group(1)).replace("№", "")
            raw_record = re.sub(r"\s*/\s*", "/", raw_record).replace(" ", "")
            if "СКП" in match.group(0).upper() and not raw_record.upper().startswith("СКП"):
                raw_record = f"СКП{raw_record}"
            record_number = raw_record.upper().replace("СКП", "СКП", 1)
            break
    sex = ""
    sex_match = re.search(r"\bпол\s*:\s*(жен(?:ск\w*)?|муж(?:ск\w*)?)", text, re.IGNORECASE)
    if sex_match:
        sex = "женский" if sex_match.group(1).casefold().startswith("жен") else "мужской"
    return PatientIdentity(name, birth_date, sex, record_number)


SECTION_STARTS: dict[str, tuple[str, ...]] = {
    "clinical_diagnosis": (r"заключительный\s+клинический\s+диагноз", r"клинический\s+диагноз"),
    "disease_history": (r"анамнез\s+заболевания",),
    "life_history": (r"анамнез\s+жизни",),
    "laboratory_results": (r"лабораторн\w*\s+исследован",),
    "instrumental_results": (r"инструментальн\w*\s+исследован",),
    "rehabilitation_potential": (r"реабилитационн\w*\s+потенциал",),
    "limitations": (
        r"факторы,?\s+ограничивающ\w*(?:\s+проведение\s+(?:медицинской\s+)?реабилитаци\w*(?:\s+мероприятий)?)?",
        r"реабилитационн\w*\s+ограничен\w*",
    ),
    "risks": (
        r"факторы\s+риска(?:\s+проведения\s+(?:медицинской\s+)?реабилитацион\w*(?:\s+мероприятий)?)?",
        r"риск\w*\s+проведения\s+реабилитацион\w*",
    ),
    "movement_regimen": (r"двигательн\w*\s+режим",),
    "diet": (r"\bдиета\b",),
    "medication": (
        r"(?:план\s+лечения\s+)?медикаментозн\w*\s+(?:лечение|терапия)",
        r"лекарственн\w*\s+терап\w*",
    ),
    "goal": (r"цель\s+на\s+этап\s+медицинской\s+реабилитации",),
    "tasks": (r"задачи\s+медицинской\s+реабилитации",),
}
SECTION_STOP = re.compile(
    r"^(?:\d+(?:\.\d+)?[.)]?\s*)?(?:клинический диагноз|реабилитационный диагноз|сведения о реабилитации|"
    r"анамнез заболевания|анамнез жизни|результаты диагностических|лабораторные исследования|"
    r"инструментальные исследования|результаты осмотров|реабилитационный потенциал|факторы,? ограничивающие|"
    r"факторы риска|диагноз клинический|цель на этап|цель,? поставленная на этап|"
    r"задачи медицинской|индивидуальный план|двигательный режим|диета|"
    r"медикаментозная (?:терапия|лечение)|немедикаментозное лечение|"
    r"реабилитационные мероприятия|реабилитационный диагноз|функциональный диагноз|динамика|"
    r"логопедический статус|нейропсихологический статус|обоснование диагноза|"
    r"выполненные медицинские вмешательства|план обследования|план лечения|назначения|"
    r"трансфузии|оперативные вмешательства|медицинские вмешательства|[AАBВ]\d{2}(?:\.\d+){2,5}|"
    r"физикальное исследование|эпидемиологический анамнез|фамилия, имя, отчество)",
    re.IGNORECASE,
)


def extract_section(document: ParsedDocument, patterns: tuple[str, ...]) -> str:
    lines = _document_lines(document)
    heading_prefix = r"^(?:\d+(?:\.\d+)*[.)]?\s*)?"
    for index, line in enumerate(lines):
        for pattern in patterns:
            match = re.match(rf"{heading_prefix}(?:{pattern})", line, re.IGNORECASE)
            if not match:
                continue
            first = line[match.end() :].lstrip(" :.-")
            values = [first] if first else []
            for following in lines[index + 1 :]:
                if SECTION_STOP.search(following.strip()):
                    break
                if following.strip():
                    values.append(following.strip())
            return clean_text(" ".join(values))
    return ""


def extract_clinical_sections(document: ParsedDocument) -> dict[str, str]:
    result = {name: extract_section(document, patterns) for name, patterns in SECTION_STARTS.items()}
    if not result["clinical_diagnosis"]:
        result["clinical_diagnosis"] = extract_section(document, (r"основное\s+заболевание",))
    if not result["laboratory_results"] or not result["instrumental_results"]:
        lines = _document_lines(document)
        start = next(
            (
                index
                for index, line in enumerate(lines)
                if line.casefold().startswith("пациентом предоставлены необходимые")
            ),
            None,
        )
        if start is not None:
            end = next(
                (
                    index
                    for index in range(start + 1, len(lines))
                    if re.match(r"^физикальное\s+исследование", lines[index], re.IGNORECASE)
                ),
                len(lines),
            )
            diagnostic_lines = lines[start:end]
            instrumental_start = next(
                (
                    index
                    for index, line in enumerate(diagnostic_lines)
                    if re.match(
                        r"^(?:УЗАС|УЗИ|ЭКГ|Эхо-КГ|ЭЭГ|Рентген|КТ|МРТ)\b",
                        line,
                        re.IGNORECASE,
                    )
                ),
                len(diagnostic_lines),
            )
            if not result["laboratory_results"]:
                laboratory = " ".join(diagnostic_lines[:instrumental_start])
                laboratory = re.sub(
                    r"^Пациентом предоставлены необходимые для госпитализации документы\s*",
                    "",
                    laboratory,
                    flags=re.IGNORECASE,
                )
                result["laboratory_results"] = clean_text(laboratory)
            if not result["instrumental_results"]:
                result["instrumental_results"] = clean_text(
                    " ".join(diagnostic_lines[instrumental_start:])
                )
    if not result["movement_regimen"]:
        for line in _document_lines(document):
            match = re.match(
                r"^(?:(свободный|общий|палатный|постельный)\s+двигательный\s+режим|"
                r"назначения\s+режим\s+(свободный|общий|палатный|постельный))\b",
                line,
                re.IGNORECASE,
            )
            if match:
                value = (match.group(1) or match.group(2)).casefold()
                result["movement_regimen"] = "свободный" if value == "общий" else value
                break
    return result


def extract_conclusion(
    document: ParsedDocument,
    role: SpecialistRole | None = None,
) -> str:
    blocks: list[str] = []
    lines = _document_lines(document)
    for index, line in enumerate(lines):
        match = re.match(r"^заключение(?:[^:\n]{0,180})?\s*:\s*", line, re.IGNORECASE)
        if not match:
            continue
        if "предшествующ" in line[: match.end()].casefold():
            continue
        label_owner = _specialist_from_text(line[: match.end()])
        if label_owner is None and index:
            previous = lines[index - 1]
            if previous.casefold().startswith("заключение"):
                label_owner = _specialist_from_text(previous)
        physician_roles = {SpecialistRole.FRM, SpecialistRole.NEUROLOGIST}
        if role is not None and label_owner is not None and not (
            label_owner is role or role in physician_roles and label_owner in physician_roles
        ):
            continue
        values = [line[match.end() :].strip()]
        for following in lines[index + 1 :]:
            if SECTION_STOP.search(following.strip()) or re.match(
                r"^(?:рекомендации|рекомендовано|дата|врач|специалист|медицинский\s+психолог|медицинский\s+логопед|подпись)\b",
                following,
                re.IGNORECASE,
            ) or (
                _specialist_from_text(following) is not None
                and bool(re.search(r"_{3,}|/{1,}\s*_+|\bподпись\b", following, re.IGNORECASE))
            ):
                break
            if following.strip():
                values.append(following.strip())
        value = clean_text(" ".join(values))
        if value:
            blocks.append(value)
    if not blocks and role in {SpecialistRole.NEUROPSYCHOLOGIST, SpecialistRole.LOGOPEDIST}:
        heading = (
            r"^нейропсихологический статус\s*:"
            if role is SpecialistRole.NEUROPSYCHOLOGIST
            else r"^логопедический статус(?: при выписке)?\s*:"
        )
        for index, line in enumerate(lines):
            if not re.match(heading, line, re.IGNORECASE):
                continue
            values: list[str] = []
            for following in lines[index + 1 :]:
                if re.match(
                    r"^(?:исследование анамнеза|рекомендовано|медицинский психолог|медицинский логопед)\b",
                    following,
                    re.IGNORECASE,
                ):
                    break
                values.append(following)
            value = clean_text(" ".join(values))
            if value:
                blocks.append(value)
            break
    return blocks[-1] if blocks else ""


@dataclass(frozen=True, slots=True)
class IcfObservation:
    code: str
    description: str
    ratings: tuple[IcfQualifier, ...]
    note: str = ""
    specialist: SpecialistRole | None = None

    @property
    def current(self) -> IcfQualifier | None:
        return self.ratings[-1] if self.ratings else None


def _exact_qualifier(value: str) -> IcfQualifier | None:
    match = QUALIFIER_RE.fullmatch(clean_text(value))
    if not match:
        return None
    return IcfQualifier(int(match.group(1)), bool(match.group(2)))


def _specialist_from_text(value: str) -> SpecialistRole | None:
    low = clean_text(value).casefold()
    if not low:
        return None
    if "патопсих" in low:
        return SpecialistRole.PATHOPSYCHOLOGIST
    if "нейропсих" in low:
        return SpecialistRole.NEUROPSYCHOLOGIST
    if "логоп" in low:
        return SpecialistRole.LOGOPEDIST
    if "эрго" in low:
        return SpecialistRole.OCCUPATIONAL_THERAPIST
    if re.fullmatch(r"ф\s*\.?\s*т\s*\.?", low) or any(
        token in low for token in ("физической реабилитац", "физический терапевт", "кинезио")
    ):
        return SpecialistRole.PHYSICAL_THERAPIST
    if re.search(r"\bфрм\b", low) or "реабилитационной медицины" in low:
        return SpecialistRole.FRM
    if "невролог" in low:
        return SpecialistRole.NEUROLOGIST
    if "психолог" in low:
        return SpecialistRole.NEUROPSYCHOLOGIST
    return None


def extract_icf_observations(document: ParsedDocument) -> list[IcfObservation]:
    observations: list[IcfObservation] = []
    for table in document.tables:
        candidate_rows: list[tuple[ParsedRow, int]] = []
        for row in table.rows:
            for cell in row.cells:
                if ICF_CODE_RE.fullmatch(clean_text(cell.text)):
                    candidate_rows.append((row, cell.col))
                    break
        if "мкф" not in table.text.casefold() and len(candidate_rows) < 2:
            continue
        for row, code_col in candidate_rows:
            values = row.as_map()
            code = (
                clean_text(values[code_col])
                .replace(" ", "")
                .translate(str.maketrans({"е": "e", "Е": "E"}))
            )
            description = ""
            for col in sorted(values):
                value = clean_text(values[col])
                if col > code_col and value and not _exact_qualifier(value):
                    description = value
                    break
            qualifier_candidates: list[tuple[int, IcfQualifier]] = []
            threshold = 10 if row.logical_cols >= 14 else max(2, row.logical_cols - 6)
            for col, value in values.items():
                qualifier = _exact_qualifier(value)
                if col >= threshold and qualifier is not None:
                    qualifier_candidates.append((col, qualifier))
            qualifier_candidates.sort(key=lambda item: item[0])
            is_personal_factor = code.casefold().startswith("pf")
            if not qualifier_candidates and not is_personal_factor:
                continue
            specialist = None
            for col in sorted(values, reverse=True):
                if col > code_col and (candidate := _specialist_from_text(values[col])) is not None:
                    specialist = candidate
                    break
            note = ""
            for col in sorted(values, reverse=True):
                value = clean_text(values[col])
                if value and col > code_col and not _exact_qualifier(value) and value != description:
                    if (
                        not ICF_CODE_RE.fullmatch(value)
                        and value.casefold() not in {"+", "-"}
                        and not re.fullmatch(r"\d+(?:\s*,\s*\d+)*", value)
                        and _specialist_from_text(value) is None
                    ):
                        note = value
                        break
            observations.append(
                IcfObservation(
                    code,
                    description,
                    tuple(item[1] for item in qualifier_candidates),
                    note,
                    specialist,
                )
            )
    return observations


def extract_scale_measurements(
    document: ParsedDocument,
    role: SpecialistRole,
    document_datetime: datetime | None,
) -> list[ScaleMeasurement]:
    def header_datetime(value: str) -> datetime | None:
        cleaned = clean_text(value)
        parsed = parse_first_datetime(cleaned)
        if parsed is not None:
            if (
                document_datetime is not None
                and parsed.date() == document_datetime.date()
                and not _has_explicit_time(cleaned)
            ):
                return datetime.combine(parsed.date(), document_datetime.time())
            return parsed
        if document_datetime is None:
            return None
        match = SHORT_DATE_RE.search(cleaned)
        if not match:
            return None
        try:
            value_date = date(document_datetime.year, int(match.group(2)), int(match.group(1)))
        except ValueError:
            return None
        text_without_short_date = f"{cleaned[:match.start()]} {cleaned[match.end():]}"
        if _has_explicit_time(text_without_short_date):
            value_time = _time_near(cleaned, match.start(), match.end())
        elif value_date == document_datetime.date():
            value_time = document_datetime.time()
        else:
            value_time = time(0, 0)
        return datetime.combine(value_date, value_time)

    measurements: list[ScaleMeasurement] = []
    for table in document.tables:
        rows = [row.as_list() for row in table.rows]
        if not rows:
            continue
        first = [clean_text(value) for value in rows[0]]
        first_low = [value.casefold() for value in first]
        if any("шкала/опросник" in value for value in first_low) or (
            any("дата" in value for value in first_low)
            and any("результат" in value for value in first_low)
        ):
            for row in rows[1:]:
                values = [clean_text(value) for value in row]
                if len(values) < 3:
                    continue
                measured = header_datetime(values[0]) or document_datetime
                name, value = _canonical_scale_name(values[1]), values[2]
                if name and value:
                    measurements.append(ScaleMeasurement(name, value, measured, role, document.source_path))
            continue
        if len(first) >= 2 and any(header_datetime(value) for value in first[1:] if value):
            dates = [header_datetime(value) for value in first]
            for row in rows[1:]:
                values = [clean_text(value) for value in row]
                if not values or not values[0]:
                    continue
                scale_name = _canonical_scale_name(values[0])
                if (
                    role is SpecialistRole.NEUROPSYCHOLOGIST
                    and scale_name.casefold() == "общий балл"
                    and "монреальская шкала оценки психических функций" in document.text.casefold()
                ):
                    scale_name = "Монреальская шкала оценки психических функций"
                for col in range(1, min(len(values), len(dates))):
                    if values[col]:
                        measurements.append(
                            ScaleMeasurement(
                                scale_name,
                                values[col],
                                dates[col] or document_datetime,
                                role,
                                document.source_path,
                            )
                        )

    if role is SpecialistRole.LOGOPEDIST:
        narrative_patterns = (
            ("Шкала дизартрии", r"^Шкала(?:\s+оценки)?\s+дизартрии\s*[–—-]\s*([^.(]+(?:\s+балл\w*)?)"),
            (
                "Оценка MASA для классификации степени тяжести дисфагии и аспирации",
                r"^Оценка\s+MASA[^–—\n]*[–—-]\s*([^.]*)",
            ),
            (
                "Тест оценки глотания с продуктами различной плотности и объёма (VVT)",
                r"^Тест\s+оценки\s+глотания[^–—\n]*[–—-]\s*([^.]*)",
            ),
        )
        for line in _document_lines(document):
            for name, pattern in narrative_patterns:
                match = re.match(pattern, line, re.IGNORECASE)
                if match and (value := clean_text(match.group(1))):
                    measurements.append(
                        ScaleMeasurement(name, value, document_datetime, role, document.source_path)
                    )
                    break
    if role in {SpecialistRole.FRM, SpecialistRole.NEUROLOGIST}:
        for line in _document_lines(document):
            for name, pattern in PHYSICIAN_NARRATIVE_SCALES:
                match = re.match(pattern, line, re.IGNORECASE)
                if match and (value := clean_text(match.group(1))):
                    measurements.append(
                        ScaleMeasurement(name, value, document_datetime, role, document.source_path)
                    )
                    break
        # Discharge summaries often embed copies of every specialist's scale
        # table.  Those copies must not be re-labelled as physician results.
        measurements = [
            item
            for item in measurements
            if any(token in item.name.casefold() for token in PHYSICIAN_SCALE_TOKENS)
        ]
    unique: dict[tuple[str, str, datetime | None], ScaleMeasurement] = {}
    for item in measurements:
        unique[(item.name.casefold(), item.value, item.measured_at)] = item
    return list(unique.values())


def _procedure_specialist(name: str) -> str:
    low = name.casefold()
    if "логоп" in low:
        return SpecialistRole.LOGOPEDIST.display_name
    if "нейропсих" in low or "психическ" in low:
        return SpecialistRole.NEUROPSYCHOLOGIST.display_name
    if any(token in low for token in ("лечебной физкультур", "трениров", "механотерап", "стабил", "thera")):
        return SpecialistRole.PHYSICAL_THERAPIST.display_name
    if "эрго" in low or "кист" in low:
        return SpecialistRole.OCCUPATIONAL_THERAPIST.display_name
    return "Медицинская сестра по физиотерапии"


def _split_procedure_name(raw: str) -> tuple[str, str]:
    match = re.match(r"\s*([AАBВ]\d{2}(?:\.\d+){2,5})\s*(.*)", raw)
    if not match:
        return "", clean_text(raw)
    return match.group(1), clean_text(match.group(2))


def extract_procedures(document: ParsedDocument) -> list[Procedure]:
    procedures: list[Procedure] = []
    for table in document.tables:
        low = table.text.casefold()
        if "назначения" not in low and "реабилитационные процедуры" not in low:
            continue
        for row in table.rows[1:]:
            values = row.as_list()
            if not values:
                continue
            raw_name = clean_text(values[0])
            if not raw_name or not PROCEDURE_CODE_RE.search(raw_name):
                continue
            code, name = _split_procedure_name(raw_name)
            plus_cells = [value for value in values[3:] if "+" in value]
            plus_count = len(plus_cells)
            count_needs_review = any(value.count("+") > 1 for value in plus_cells)
            duration = None
            for value in reversed(values):
                match = re.search(r"(?<!\d)(\d{1,3})\s*мин", value, re.IGNORECASE)
                if match:
                    duration = int(match.group(1))
                    break
            procedures.append(
                Procedure(
                    name=name or raw_name,
                    specialist=_procedure_specialist(raw_name),
                    actual_count=plus_count,
                    duration_minutes=duration,
                    frequency="",
                    code=code,
                    source=document.source_path,
                    count_needs_review=count_needs_review,
                )
            )
    if procedures:
        return procedures

    flat = clean_text(document.text)
    matches = list(PROCEDURE_CODE_RE.finditer(flat))
    for index, match in enumerate(matches):
        start = match.start(1)
        end = matches[index + 1].start(1) if index + 1 < len(matches) else len(flat)
        segment = flat[start:end]
        code, name = _split_procedure_name(segment)
        plus_count = segment.count("+")
        duration_match = re.search(r"(\d{1,3})\s*мин", segment, re.IGNORECASE)
        duration = int(duration_match.group(1)) if duration_match else None
        name = re.split(r"\b\d{1,2}[:.]\d{2}\b", name, maxsplit=1)[0]
        procedures.append(
            Procedure(
                name=clean_text(name),
                specialist=_procedure_specialist(name),
                actual_count=plus_count,
                duration_minutes=duration,
                frequency="",
                code=code,
                source=document.source_path,
                count_needs_review=True,
            )
        )
    return procedures
