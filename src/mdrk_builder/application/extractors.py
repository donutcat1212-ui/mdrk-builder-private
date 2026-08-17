from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, time
from pathlib import Path

from mdrk_builder.application.scale_registry import canonical_scale_label
from mdrk_builder.domain import (
    IcfQualifier,
    PatientIdentity,
    Procedure,
    ScaleMeasurement,
    SpecialistRole,
)
from mdrk_builder.infrastructure.ooxml_reader import (
    BodyItem,
    ParsedDocument,
    ParsedRow,
    clean_text,
)


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
DATE_NUMERIC_RE = re.compile(
    r"(?<!\d)([0-3]?\d)\s*[./-]\s*([01]?\d)\s*[./-]\s*((?:19|20)\d{2}|\d{2})(?!\d)"
)
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
INSTRUMENTAL_START_RE = re.compile(
    r"(?<!\w)(?:"
    r"рентген\w*|флюорограф\w*|УЗАС|УЗИ|ЭКГ|Эхо[-–— ]?КГ|ЭЭГ|"
    r"МСКТ|КТ|МРТ|СМАД|холтер\w*|дуплекс\w*|"
    r"компьютерн\w*\s+томограф\w*|"
    r"магнитно[-–— ]?резонанс\w*\s+томограф\w*)\b",
    re.IGNORECASE,
)
PHYSICIAN_SCALE_TOKENS = (
    "ривермид",
    "ренкин",
    "рэнкин",
    "rankin",
    "nrs 2002",
    "бартел",
    "скф",
    "реабилитационной маршрутизации",
)
PHYSICIAN_NARRATIVE_SCALES = (
    (
        "Шкала реабилитационной маршрутизации (ШРМ)",
        r"^.*?\bШРМ\s*[:–—-]?\s*([0-6](?:\s*балл\w*)?)\b.*$",
    ),
    (
        "Индекс мобильности Ривермид",
        r"^Индекс\s+мобильности\s+Ривермид\s*[:–—-]\s*(.+)$",
    ),
    (
        "Модифицированная шкала Рэнкина",
        r"^Модифицированная\s+шкала\s+Р[еэ]нкин\w*\s*[:–—-]\s*(.+)$",
    ),
    ("NRS 2002", r"^NRS\s*[-–—]?\s*2002\s*[:–—-]\s*(.+)$"),
    ("СКФ", r"^СКФ\s*[:–—-]\s*(.+)$"),
    ("Шкала Бартел", r"^Шкала\s+Бартел\w*\s*[:–—-]\s*(.+)$"),
)

_SPECIALIST_NAME_INITIALS_RE = re.compile(
    r"\b([А-ЯЁ][А-ЯЁа-яё-]{2,})\s+([А-ЯЁ])\.\s*([А-ЯЁ])\."
)
_SPECIALIST_INITIALS_NAME_RE = re.compile(
    r"\b([А-ЯЁ])\.\s*([А-ЯЁ])\.\s*([А-ЯЁ][А-ЯЁа-яё-]{2,})\b"
)
_SIGNATURE_SEPARATOR_RE = re.compile(r"[_/\\|]+")
_SPECIALIST_ROLE_TOKENS: dict[SpecialistRole, tuple[str, ...]] = {
    SpecialistRole.FRM: ("врач фрм", "врач физической и реабилитационной медицины"),
    SpecialistRole.NEUROLOGIST: ("невролог",),
    SpecialistRole.PHYSICAL_THERAPIST: (
        "специалист по физической реабилитации",
        "физический терапевт",
        "врач лфк",
    ),
    SpecialistRole.OCCUPATIONAL_THERAPIST: (
        "эргореабилитолог",
        "эрготерапевт",
        "специалист по эргореабилитации",
    ),
    SpecialistRole.LOGOPEDIST: ("логопед", "афазиолог"),
    SpecialistRole.NEUROPSYCHOLOGIST: ("нейропсихолог",),
    SpecialistRole.PATHOPSYCHOLOGIST: ("патопсихолог",),
}
_GENERIC_SPECIALIST_LABEL_RE = re.compile(
    r"\b(?:специалист|исполнитель|медицинский\s+работник|лечащий\s+врач)\b",
    re.IGNORECASE,
)
_NON_NAME_SURNAMES = {"фамилия", "пациент", "пациентка"}


def _specialist_names_from_line(line: str) -> list[str]:
    """Return ordered name candidates after neutralizing Word signature rules."""

    text = clean_text(_SIGNATURE_SEPARATOR_RE.sub(" ", line))
    matches: list[tuple[int, str]] = []
    for match in _SPECIALIST_NAME_INITIALS_RE.finditer(text):
        if match.group(1).casefold() not in _NON_NAME_SURNAMES:
            matches.append(
                (match.start(), f"{match.group(1)} {match.group(2)}.{match.group(3)}.")
            )
    for match in _SPECIALIST_INITIALS_NAME_RE.finditer(text):
        if match.group(3).casefold() not in _NON_NAME_SURNAMES:
            matches.append(
                (match.start(), f"{match.group(3)} {match.group(1)}.{match.group(2)}.")
            )
    return [value for _, value in sorted(matches)]


def extract_specialist_name(document: ParsedDocument, role: SpecialistRole) -> str:
    """Read a clinician name only from a professional header or signature.

    Scale names, patient labels, and narrative mentions are deliberately ignored.
    A combined treating-physician/department-head line belongs to the treating
    neurologist, so its first name is selected; ordinary signature lines use the
    last name next to the professional label.
    """

    lines = [clean_text(line) for line in document.text.splitlines() if clean_text(line)]
    role_tokens = _SPECIALIST_ROLE_TOKENS.get(role, ())
    for line in reversed(lines):
        low = line.casefold().replace("ё", "е")
        if not role_tokens or not any(token.replace("ё", "е") in low for token in role_tokens):
            continue
        names = _specialist_names_from_line(line)
        if names:
            if role is SpecialistRole.NEUROLOGIST and "лечащ" in low:
                return names[0]
            return names[-1]
    for line in reversed(lines):
        if _GENERIC_SPECIALIST_LABEL_RE.search(line):
            names = _specialist_names_from_line(line)
            if names:
                return names[-1]
    return ""


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


def extract_mdrk_document_datetime(document: ParsedDocument) -> datetime | None:
    """Read the meeting timestamp adjacent to the MDRK title.

    The narrow adjacency rule avoids mistaking a birth/admission date elsewhere
    in the assembled document for the meeting timestamp.
    """

    ordered: list[str] = []
    for item in document.body_items:
        if item.kind == "paragraph":
            ordered.append(document.paragraphs[item.index])
        elif item.kind == "table":
            ordered.append(document.tables[item.index].text)
    title = "консилиум мультидисциплинарной реабилитационной команды"
    for index, value in enumerate(ordered):
        if title not in clean_text(value).casefold():
            continue
        for candidate in ordered[index : index + 3]:
            candidate = clean_text(candidate)
            first = next(_date_matches(candidate), None)
            if first is None:
                continue
            date_match, parsed_date = first
            # Some approved forms write the meeting time as ``08-46``.  Keep
            # this deliberately adjacent to a complete date so ``14-05-2026``
            # can never be mistaken for 14:05.
            suffix = candidate[date_match.end() : date_match.end() + 40]
            dash_time = re.match(
                r"^\s*[,;]?\s*(?:время\s*)?([0-2]?\d)\s*[-–—]\s*([0-5]\d)(?!\d)",
                suffix,
                re.IGNORECASE,
            )
            if dash_time is not None and int(dash_time.group(1)) <= 23:
                return datetime.combine(
                    parsed_date,
                    time(int(dash_time.group(1)), int(dash_time.group(2))),
                )
            if parsed := parse_first_datetime(candidate):
                return parsed
        return None
    return None


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
    "laboratory_results": (r"лабораторн\w*\s+исследован\w*",),
    "instrumental_results": (r"инструментальн\w*\s+исследован\w*",),
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
        r"(?:назначения\s+)?(?:план\s+лечения\s+)?медикаментозн\w*\s+(?:лечение|терапия)",
        r"лекарственн\w*\s+терап\w*",
    ),
    "goal": (r"цель\s+на\s+этап\s+медицинской\s+реабилитации",),
    "tasks": (r"задачи\s+медицинской\s+реабилитации",),
}
SECTION_STOP = re.compile(
    r"^(?:\d+(?:\.\d+)?[.)]?\s*)?(?:клинический диагноз|реабилитационный диагноз|сведения о реабилитации|"
    r"анамнез заболевания|анамнез жизни|результаты диагностических|лабораторные исследования|"
    r"инструментальные исследования|результаты осмотров|реабилитационный потенциал|факторы,? ограничивающие|"
    r"дата\s+(?:и\s+время\s+)?выписки|"
    r"факторы риска|диагноз клинический|цель на этап|цель,? поставленная на этап|"
    r"задачи медицинской|реабилитационн\w* задачи? на этап|"
    r"задача на этап|короткосрочн\w* задача|индивидуальный план|двигательный режим|диета|"
    r"медикаментозная (?:терапия|лечение)|немедикаментозн\w* (?:лечение|терапия)|"
    r"реабилитационные мероприятия|реабилитационный диагноз|функциональный диагноз|динамика|"
    r"логопедический статус|нейропсихологический статус|обоснование диагноза|"
    r"выполненные медицинские вмешательства|план обследования|план лечения|назначения|"
    r"трансфузии|оперативные вмешательства|медицинские вмешательства|[AАBВ]\d{2}(?:\.\d+){2,5}|"
    r"физикальное исследование|эпидемиологический анамнез|фамилия, имя, отчество)",
    re.IGNORECASE,
)

_SPECIALIST_STAGE_GOAL_RE = re.compile(
    r"^(?:реабилитационн\w*\s+)?задача\s+на\s+этап\s+"
    r"(?:мр|(?:медицинской\s+)?реабилитации)\s*[:–—.-]?\s*(.*)$",
    re.IGNORECASE,
)
_SPECIALIST_SHORT_TASK_RE = re.compile(
    r"^(?:коротко|кратко)срочн\w*\s+задача(?:\s+(?:медицинской\s+)?реабилитации)?"
    r"(?:\s*№\s*\d+)?\s*[:–—.-]?\s*(.*)$",
    re.IGNORECASE,
)
_SPECIALIST_TASK_BLOCK_RE = re.compile(
    r"^реабилитационн\w*\s+задачи\s+на\s+этап\s+"
    r"(?:мр|(?:медицинской\s+)?реабилитации)\s*[:–—.-]?\s*(.*)$",
    re.IGNORECASE,
)
_SPECIALIST_TASK_BLOCK_STOP_RE = re.compile(
    r"^(?:на\s+основании\s+данных|рекомендован[оаы]?|рекомендации|заключение|"
    r"медицинский\s+логопед|специалист\s+по\s+физической\s+реабилитации|"
    r"[А-ЯЁ][А-ЯЁа-яё-]+\s+[А-ЯЁ]\.\s*[А-ЯЁ]\.(?:\s|$))",
    re.IGNORECASE,
)
_TASK_PREFIX_RE = re.compile(r"^(?:[-•–—]|\d+[.)])\s*")


def _clean_task_item(value: str) -> str:
    return clean_text(_TASK_PREFIX_RE.sub("", value)).strip()


def _specialist_rehabilitation_plan(document: ParsedDocument) -> tuple[str, list[str]]:
    """Read explicit specialist stage goals and rehabilitation-task blocks."""

    lines = _document_lines(document)
    goals: list[str] = []
    tasks: list[str] = []
    for index, line in enumerate(lines):
        if match := _SPECIALIST_STAGE_GOAL_RE.match(line):
            if value := clean_text(match.group(1)):
                goals.append(value)
            continue
        if match := _SPECIALIST_SHORT_TASK_RE.match(line):
            if value := _clean_task_item(match.group(1)):
                tasks.append(value)
            continue
        match = _SPECIALIST_TASK_BLOCK_RE.match(line)
        if match is None:
            continue
        if value := _clean_task_item(match.group(1)):
            tasks.append(value)
        for following in lines[index + 1 :]:
            if SECTION_STOP.search(following) or _SPECIALIST_TASK_BLOCK_STOP_RE.match(following):
                break
            if value := _clean_task_item(following):
                tasks.append(value)

    unique_goals = list(dict.fromkeys(goals))
    unique_tasks = list(dict.fromkeys(tasks))
    return "\n".join(unique_goals), unique_tasks


def extract_section(
    document: ParsedDocument,
    patterns: tuple[str, ...],
    *,
    preserve_lines: bool = False,
) -> str:
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
            cleaned = [clean_text(value) for value in values if clean_text(value)]
            return "\n".join(cleaned) if preserve_lines else clean_text(" ".join(cleaned))
    return ""


def extract_clinical_sections(document: ParsedDocument) -> dict[str, str]:
    result = {
        name: extract_section(
            document,
            patterns,
            preserve_lines=name in {"medication", "tasks"},
        )
        for name, patterns in SECTION_STARTS.items()
    }
    specialist_goal, specialist_tasks = _specialist_rehabilitation_plan(document)
    if not result["goal"] and specialist_goal:
        result["goal"] = specialist_goal
    if specialist_tasks:
        current_tasks = [
            _clean_task_item(line)
            for line in result["tasks"].splitlines()
            if _clean_task_item(line)
        ]
        result["tasks"] = "\n".join(dict.fromkeys((*current_tasks, *specialist_tasks)))
    if not result["clinical_diagnosis"]:
        result["clinical_diagnosis"] = extract_section(document, (r"основное\s+заболевание",))
    if not result["laboratory_results"] or not result["instrumental_results"]:
        lines = _document_lines(document)
        numbered_prefix = r"^(?:\d+(?:\.\d+)*[.)]?\s*)?"
        diagnostic_specs = (
            (
                numbered_prefix
                + r"пациентом\s+предоставлены\s+необходимые\s+"
                r"для\s+госпитализации\s+документы\s*",
                numbered_prefix + r"физикальное\s+исследование",
            ),
            (
                numbered_prefix + r"выполненные\s+медицинские\s+вмешательства\s*",
                numbered_prefix
                + r"(?:консультация|план\s+обследования|план\s+лечения|назначения|"
                r"физикальное\s+исследование|фамилия,\s*имя,\s*отчество)\b",
            ),
        )
        for start_pattern, stop_pattern in diagnostic_specs:
            start = next(
                (
                    index
                    for index, line in enumerate(lines)
                    if re.match(start_pattern, line, re.IGNORECASE)
                ),
                None,
            )
            if start is None:
                continue
            end = next(
                (
                    index
                    for index in range(start + 1, len(lines))
                    if re.match(stop_pattern, lines[index], re.IGNORECASE)
                ),
                len(lines),
            )
            diagnostic_lines = list(lines[start:end])
            diagnostic_lines[0] = re.sub(
                start_pattern,
                "",
                diagnostic_lines[0],
                flags=re.IGNORECASE,
            ).strip()
            diagnostic_lines = [line for line in diagnostic_lines if line]
            diagnostic_text = clean_text(" ".join(diagnostic_lines))
            inline_stop = re.search(
                r"\b(?:консультация|план\s+обследования|план\s+лечения|назначения)\b",
                diagnostic_text,
                re.IGNORECASE,
            )
            if inline_stop is not None:
                diagnostic_text = clean_text(diagnostic_text[: inline_stop.start()])
            instrumental_match = INSTRUMENTAL_START_RE.search(diagnostic_text)
            instrumental_start = (
                instrumental_match.start() if instrumental_match is not None else len(diagnostic_text)
            )
            if instrumental_match is not None:
                numbered_match = re.search(
                    r"(?:^|\s)(?:\d+[.)]|\d+\.\d+[.)]?)\s*$",
                    diagnostic_text[:instrumental_start],
                )
                if numbered_match is not None:
                    instrumental_start = numbered_match.start()
            if not result["laboratory_results"]:
                result["laboratory_results"] = clean_text(diagnostic_text[:instrumental_start])
            if not result["instrumental_results"]:
                result["instrumental_results"] = clean_text(diagnostic_text[instrumental_start:])
            if result["laboratory_results"] and result["instrumental_results"]:
                break
    if not result["movement_regimen"]:
        for line in _document_lines(document):
            match = re.search(
                r"(?:^|\b)(?:\d+(?:\.\d+)*[.)]?\s*)?"
                r"(?:(свободный|общий|палатный|постельный)\s+двигательный\s+режим|"
                r"назначения\s+режим\s*[:–—.-]?\s*(свободный|общий|палатный|постельный)|"
                r"(?:план\s+лечения\s*[:–—.-]?\s*)?режим\s*[:–—.-]?\s*"
                r"(свободный|общий|палатный|постельный)|"
                r"(?:план\s+лечения\s*[:–—.-]?\s*)?"
                r"двигательн\w*\s+режим\s*[:–—.-]?\s*"
                r"(свободный|общий|палатный|постельный))\b",
                line,
                re.IGNORECASE,
            )
            if match:
                value = next(group for group in match.groups() if group).casefold()
                result["movement_regimen"] = "свободный" if value == "общий" else value
                break
    return result


def _extract_neuropsych_conclusion(lines: list[str]) -> str:
    """Return the bounded status and rationale, leaving scale tables structured."""

    heading_re = re.compile(
        r"^нейропсихологический статус(?:\s+и\s+топический\s+диагноз)?\s*:",
        re.IGNORECASE,
    )
    starts = [index for index, line in enumerate(lines) if heading_re.match(line)]
    for start in reversed(starts):
        status: list[str] = [lines[start]]
        for line in lines[start + 1 :]:
            if re.match(r"^на основании данных\b", line, re.IGNORECASE):
                break
            if re.match(
                r"^(?:количественная\s+оценка|монреальская\s+шкала|"
                r"оценка\s+устойчивости|исследование\s+анамнеза|"
                r"отмечается\b.*\bдинамик|рекомендовано|медицинский\s+психолог|подпись)\b",
                line,
                re.IGNORECASE,
            ):
                break
            status.append(line)

        rationale = ""
        for line in lines[start + 1 :]:
            if re.match(r"^на основании данных\b", line, re.IGNORECASE):
                rationale = line
                break
            if re.match(
                r"^(?:отмечается\b.*\bдинамик|рекомендовано|"
                r"медицинский\s+психолог|подпись)\b",
                line,
                re.IGNORECASE,
            ):
                break
        result = [clean_text(line) for line in status if clean_text(line)]
        if rationale:
            result.append(clean_text(rationale))
        if len(result) > 1 or heading_re.sub("", result[0]).strip():
            return "\n".join(result)
    return ""


def _extract_logopedist_conclusion(lines: list[str]) -> str:
    """Prefer course dynamics plus the final speech status when available."""

    dynamics = [
        index for index, line in enumerate(lines) if re.match(r"^динамика\s*:", line, re.IGNORECASE)
    ]
    status_re = re.compile(
        r"^логопедический статус(?:\s+при\s+выписке)?(?:\s+измен[её]н)?\s*:",
        re.IGNORECASE,
    )
    signature_re = re.compile(r"^(?:медицинский\s+логопед|подпись)\b", re.IGNORECASE)
    if dynamics:
        start = dynamics[-1]
        result = [lines[start]]
        status_index = next(
            (index for index in range(start + 1, len(lines)) if status_re.match(lines[index])),
            None,
        )
        if status_index is not None:
            result.append(lines[status_index])
            for line in lines[status_index + 1 :]:
                if signature_re.match(line) or re.match(
                    r"^(?:факторы,?\s+ограничивающие|функциональный\s+диагноз|"
                    r"задач[аи]\s+на\s+этап|короткосрочная\s+задача|на основании данных)\b",
                    line,
                    re.IGNORECASE,
                ):
                    break
                result.append(line)
        return "\n".join(clean_text(line) for line in result if clean_text(line))

    status_indices = [index for index, line in enumerate(lines) if status_re.match(line)]
    if status_indices:
        start = status_indices[-1]
        result = [lines[start]]
        for line in lines[start + 1 :]:
            if signature_re.match(line) or re.match(
                r"^(?:факторы,?\s+ограничивающие|функциональный\s+диагноз|"
                r"задач[аи]\s+на\s+этап|короткосрочная\s+задача|на основании данных)\b",
                line,
                re.IGNORECASE,
            ):
                break
            result.append(line)
        return "\n".join(clean_text(line) for line in result if clean_text(line))
    return ""


def extract_conclusion(
    document: ParsedDocument,
    role: SpecialistRole | None = None,
) -> str:
    blocks: list[str] = []
    lines = _document_lines(document)
    if role is SpecialistRole.NEUROPSYCHOLOGIST:
        if value := _extract_neuropsych_conclusion(lines):
            return value
    if role is SpecialistRole.LOGOPEDIST:
        if value := _extract_logopedist_conclusion(lines):
            return value
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
            r"^нейропсихологический статус(?:\s+и\s+топический\s+диагноз)?\s*:"
            if role is SpecialistRole.NEUROPSYCHOLOGIST
            else r"^логопедический статус(?: при выписке)?\s*:"
        )
        for index, line in enumerate(lines):
            heading_match = re.match(heading, line, re.IGNORECASE)
            if not heading_match:
                continue
            same_line = line[heading_match.end() :].strip()
            values: list[str] = [same_line] if same_line else []
            for following in lines[index + 1 :]:
                if re.match(
                    r"^(?:исследование анамнеза|на основании данных|рекомендовано|медицинский психолог|медицинский логопед)\b",
                    following,
                    re.IGNORECASE,
                ):
                    break
                values.append(following)
            value = clean_text(" ".join(values))
            if value:
                blocks.append(value)
            break
    if not blocks and role is SpecialistRole.LOGOPEDIST:
        for index, line in enumerate(lines):
            if not re.match(r"^(?:т\s*\.\s*о\s*\.|\u0442аким\s+образом\b)", line, re.IGNORECASE):
                continue
            values = [line]
            for following in lines[index + 1 : index + 4]:
                if re.match(r"^(?:медицинский\s+логопед|подпись)\b", following, re.IGNORECASE):
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
        if any("шкала/опросник" in value for value in first_low) and any(
            "результат" in value for value in first_low
        ):
            for row in rows[1:]:
                values = [clean_text(value) for value in row]
                if len(values) < 3:
                    continue
                measured = header_datetime(values[0]) or document_datetime
                name, value = canonical_scale_label(values[1]), values[2]
                if name and value:
                    measurements.append(ScaleMeasurement(name, value, measured, role, document.source_path))
            continue
        if len(first) >= 2 and any(header_datetime(value) for value in first[1:] if value):
            dates = [header_datetime(value) for value in first]
            for row in rows[1:]:
                values = [clean_text(value) for value in row]
                if not values or not values[0]:
                    continue
                scale_name = canonical_scale_label(values[0])
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
            continue

        # Some neuropsychology forms contain an ordinary two-column
        # "characteristic / score" table without a date in its header.  Keep
        # those rows as scales so they reach the common MDRK scale table.
        if role is SpecialistRole.NEUROPSYCHOLOGIST and any(
            token in table.text.casefold()
            for token in (
                "критичность",
                "понимание смысл",
                "серийный счет",
                "нейродинамич",
                "психическая устойчивость",
            )
        ):
            for row in rows:
                populated = [clean_text(value) for value in row if clean_text(value)]
                if len(populated) < 2:
                    continue
                name, value = populated[0], populated[-1]
                if name == value or not re.fullmatch(
                    r"-?\d+(?:[.,]\d+)?(?:\s*(?:балл\w*|%))?",
                    value,
                    re.IGNORECASE,
                ):
                    continue
                measurements.append(
                    ScaleMeasurement(name, value, document_datetime, role, document.source_path)
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


def extract_mdrk_scale_measurements(
    document: ParsedDocument,
    document_datetime: datetime | None,
) -> list[ScaleMeasurement]:
    """Extract scale tables from MDRK while retaining their specialist owner.

    An MDRK document contains several specialists' tables.  Passing the whole
    document through the ordinary extractor under one role would relabel all of
    them, so each table is parsed only under its immediately preceding
    ``Результат осмотра ...`` heading.
    """

    measurements: list[ScaleMeasurement] = []
    role: SpecialistRole | None = None
    role_datetime = document_datetime
    for item in document.body_items:
        if item.kind == "paragraph":
            paragraph = clean_text(document.paragraphs[item.index])
            if re.match(r"^результат\s+осмотра\b", paragraph, re.IGNORECASE):
                role = _specialist_from_text(paragraph)
                if role is None and re.match(
                    r"^результат\s+осмотра\s+врача\b",
                    paragraph,
                    re.IGNORECASE,
                ):
                    role = SpecialistRole.FRM
                role_datetime = parse_first_datetime(paragraph) or document_datetime
            elif re.match(r"^7(?:\.|\s)", paragraph):
                role = None
            continue
        if item.kind != "table" or role is None:
            continue
        table = document.tables[item.index]
        if not table.rows:
            continue
        header = " ".join(clean_text(value) for value in table.rows[0].as_list()).casefold()
        if "шкала/опросник" not in header:
            continue
        table_document = ParsedDocument(
            source_path=document.source_path,
            normalized_path=document.normalized_path,
            tables=[table],
            body_items=[BodyItem("table", 0)],
            sha256=document.sha256,
        )
        measurements.extend(
            extract_scale_measurements(table_document, role, role_datetime)
        )

    unique: dict[tuple[SpecialistRole, str, str, datetime | None], ScaleMeasurement] = {}
    for measurement in measurements:
        key = (
            measurement.specialist,
            measurement.name.casefold(),
            measurement.value,
            measurement.measured_at,
        )
        unique[key] = measurement
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
    return match.group(1), clean_text(match.group(2)).lstrip(" .:–—-")


def _is_procedure_row_name(value: str) -> bool:
    return bool(PROCEDURE_CODE_RE.search(value) or re.search(r"\bSiS\s*терап", value, re.IGNORECASE))


def _shift_month(year: int, month: int, offset: int) -> tuple[int, int]:
    index = year * 12 + (month - 1) + offset
    return divmod(index, 12)[0], divmod(index, 12)[1] + 1


def _resolve_procedure_header_dates(
    headers: list[str],
    reference_date: date | None,
) -> dict[int, date]:
    if reference_date is None:
        return {}
    result: dict[int, date] = {}
    previous: date | None = None
    for column, raw in enumerate(headers):
        value = clean_text(raw)
        day_match = re.fullmatch(r"([0-3]?\d)", value)
        short_match = re.fullmatch(r"([0-3]?\d)[./]([01]?\d)", value)
        if day_match is None and short_match is None:
            continue
        day = int((short_match or day_match).group(1))
        if not 1 <= day <= 31:
            continue
        if short_match is not None:
            month = int(short_match.group(2))
            years = (reference_date.year - 1, reference_date.year, reference_date.year + 1)
            candidates: list[date] = []
            for year in years:
                try:
                    candidates.append(date(year, month, day))
                except ValueError:
                    pass
            if previous is not None:
                forward = [candidate for candidate in candidates if candidate >= previous]
                candidate = min(forward, default=None)
            else:
                candidate = min(
                    candidates,
                    key=lambda item: abs((item - reference_date).days),
                    default=None,
                )
        elif previous is None:
            candidates = []
            for offset in (-1, 0, 1):
                year, month = _shift_month(reference_date.year, reference_date.month, offset)
                try:
                    candidates.append(date(year, month, day))
                except ValueError:
                    pass
            candidate = min(
                candidates,
                key=lambda item: abs((item - reference_date).days),
                default=None,
            )
        else:
            year, month = previous.year, previous.month
            if day < previous.day:
                year, month = _shift_month(year, month, 1)
            try:
                candidate = date(year, month, day)
            except ValueError:
                candidate = None
        if candidate is None:
            continue
        result[column] = candidate
        previous = candidate
    return result


def _infer_procedure_frequency(
    performed_dates: tuple[date, ...],
    actual_count: int,
) -> str:
    if actual_count <= 0:
        return ""
    if actual_count == 1:
        return "однократно"
    dates = tuple(sorted(set(performed_dates)))
    if len(dates) != actual_count:
        return "периодически"

    expected: set[date] = set()
    current = dates[0]
    while current <= dates[-1]:
        if current.weekday() < 5:
            expected.add(current)
        current = date.fromordinal(current.toordinal() + 1)
    if expected and expected.issubset(dates):
        return "ежедневно"

    gaps = [(right - left).days for left, right in zip(dates, dates[1:])]
    if gaps and all(gap == 2 for gap in gaps):
        return "через день"

    weekly_counts: dict[tuple[int, int], int] = {}
    for value in dates:
        year, week, _ = value.isocalendar()
        weekly_counts[(year, week)] = weekly_counts.get((year, week), 0) + 1
    counts = list(weekly_counts.values())
    if len(counts) >= 2 and len(set(counts)) == 1 and counts[0] in {1, 2, 3}:
        count = counts[0]
        return f"{count} {'раз' if count == 1 else 'раза'} в неделю"
    return "периодически"


def extract_procedures(
    document: ParsedDocument,
    reference_date: date | None = None,
) -> list[Procedure]:
    procedures: list[Procedure] = []
    for table in document.tables:
        low = table.text.casefold()
        if "назначения" not in low and "реабилитационные процедуры" not in low:
            continue
        if not table.rows:
            continue
        headers = table.rows[0].as_list()
        date_columns = _resolve_procedure_header_dates(headers, reference_date)
        raw_date_columns = {
            index
            for index, value in enumerate(headers)
            if re.fullmatch(r"[0-3]?\d(?:[./][01]?\d)?", clean_text(value))
        }
        for row in table.rows[1:]:
            values = row.as_list()
            if not values:
                continue
            raw_name = clean_text(values[0])
            if not raw_name or not _is_procedure_row_name(raw_name):
                continue
            code, name = _split_procedure_name(raw_name)
            marker_columns = raw_date_columns or set(range(3, len(values)))
            plus_cells = [values[index] for index in marker_columns if index < len(values) and "+" in values[index]]
            plus_count = len(plus_cells)
            count_needs_review = any(value.count("+") > 1 for value in plus_cells)
            performed_dates = tuple(
                date_columns[index]
                for index in sorted(date_columns)
                if index < len(values) and "+" in values[index]
            )
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
                    frequency=_infer_procedure_frequency(performed_dates, plus_count),
                    code=code,
                    source=document.source_path,
                    count_needs_review=count_needs_review,
                    performed_dates=performed_dates,
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
                frequency=_infer_procedure_frequency((), plus_count),
                code=code,
                source=document.source_path,
                count_needs_review=True,
            )
        )
    return procedures
