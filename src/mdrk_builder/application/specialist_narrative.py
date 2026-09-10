"""Preserve specialist conclusions and their explicitly written recommendations."""
import re

from mdrk_builder.domain import SpecialistRole


def specialist_title(document, role):
    if role not in {SpecialistRole.FRM, SpecialistRole.NEUROLOGIST}:
        return role.display_name
    pattern = re.compile(r"врач по медицинской реабилитации\s*\(врач физической и реабилитационной медицины\)"
                         r"|врач физической и реабилитационной медицины", re.I)
    matches = pattern.findall("\n".join(document.text.splitlines()[-40:]))
    if matches:
        return re.sub(r"\s+", " ", matches[-1]).capitalize()
    return "Врач физической и реабилитационной медицины" if role is SpecialistRole.FRM else "Врач-невролог"

_END = re.compile(
    r"^(?:медицинский\s+(?:психолог|логопед)|врач\s*[:_]|подпись|"
    r"реабилитационн\w*\s+задач|задач[аи]\b|количественная\s+оценка|"
    r"монреальская\s+шкала|оценка\s+устойчивости|"
    r"отмечается\b.*\bдинамик\w*|функциональный\s+диагноз|нейропсихологический\s+статус|логопедический\s+статус)\b", re.I,
)
_ADVICE = re.compile(r"^(?:рекомендован[оаы]?\b|рекомендации\b|на\s+основании\s+данных\b.*рекомендован[оаы]?\b)", re.I)

_SIGNATURE = re.compile(
    r"^(?:медицинский\s+(?:психолог|логопед)|подпись\b|"
    r"специалист\s+по\s+физической\s+реабилитации\b)", re.I,
)
_EXAM_CONTENT = re.compile(
    r"^(?:.*осмотр\b|.*обследование\b|нейропсихологический\s+статус|"
    r"логопедический\s+статус|заключение|динамика|отмечается|за\s+время)", re.I,
)
_OUTCOME = re.compile(
    r"^(?:за\s+(?:время|период)\b.*реабилитац\w*.*(?:динамик|результат)|"
    r"(?:отмечается|наблюдается)\b.*(?:динамик|улучшени|ухудшени)|"
    r"(?:динамика|результаты\s+реабилитации)\s*:)", re.I,
)
_OUTCOME_END = re.compile(
    r"^(?:с\s+пациентом\s+проводились\s+занятия|"
    r"(?:индивидуальная\s+)?программа\s+реабилитац|"
    r"задач[аи]\b|количественная\s+оценка|факторы\b|"
    r"реабилитационный\s+диагноз|нейропсихологический\s+статус)", re.I,
)


def current_examination_lines(lines: list[str]) -> list[str]:
    """Bound narrative extraction to the latest examination in a combined file."""
    segments: list[list[str]] = []
    current: list[str] = []
    for line in lines:
        if _SIGNATURE.match(line):
            if current:
                segments.append(current)
            current = []
        else:
            current.append(line)
    if current and any(_EXAM_CONTENT.match(line) for line in current):
        segments.append(current)
    return segments[-1] if segments else lines


def extract_course_outcome(lines: list[str], role: SpecialistRole | None) -> str:
    """Use written course outcomes, not the planned tasks or quantitative tables."""
    if role not in {SpecialistRole.PHYSICAL_THERAPIST, SpecialistRole.NEUROPSYCHOLOGIST}:
        return ""
    lines = current_examination_lines(lines)
    start = next((i for i, line in enumerate(lines) if _OUTCOME.match(line)), None)
    if start is None:
        return ""
    result = []
    if role is SpecialistRole.NEUROPSYCHOLOGIST:
        result.extend(line for line in lines[:start] if re.match(
            r"^на\s+основании\s+данных\b.*\b(?:проведена|проведены|проводилась)\b", line, re.I))
    for line in lines[start:]:
        if _SIGNATURE.match(line) or _OUTCOME_END.match(line) or "|" in line:
            break
        if line:
            result.append(line)
    return "\n".join(result)


def _compact_speech_program(lines: list[str]) -> list[str]:
    result = []
    for line in lines:
        if re.search(r"индивидуальн\w*\s+программ\w*", line, re.I):
            label = re.split(r"\b(?:преодолени\w*|восстановлени\w*)\b", line, maxsplit=1, flags=re.I)[0]
            result.append(label.rstrip(" .:;,–—-") + ".")
            break
        result.append(line)
    return result


def complete_specialist_conclusion(base: str, lines: list[str], role: SpecialistRole | None) -> str:
    if role not in {SpecialistRole.NEUROPSYCHOLOGIST, SpecialistRole.PATHOPSYCHOLOGIST, SpecialistRole.LOGOPEDIST}:
        return base
    blocks = []
    for index, line in enumerate(lines):
        if re.match(r"^исследование\s+анамнеза\b", line, re.I):
            values = [line]
            for following in lines[index + 1:]:
                if _END.match(following) or _ADVICE.match(following) or "|" in following:
                    break
                values.append(following)
            text = "\n".join(values)
            if re.search(r"не\s+обнаруж\w*\s+основан\w*", text, re.I):
                blocks.append(text)

    for index, line in enumerate(lines):
        if not _ADVICE.match(line):
            continue
        values = [line]
        for following in lines[index + 1:]:
            if _END.match(following):
                break
            values.append(following)
        if role is SpecialistRole.LOGOPEDIST:
            values = _compact_speech_program(values)
        blocks.append("\n".join(values))
        break

    result = base.splitlines() if base else []
    if role is SpecialistRole.LOGOPEDIST:
        result = _compact_speech_program(result)
    known = {" ".join(line.split()).casefold() for line in result}
    for block in blocks:
        for line in block.splitlines():
            key = " ".join(line.split()).casefold()
            if key and key not in known:
                result.append(line)
                known.add(key)
    return "\n".join(result)
