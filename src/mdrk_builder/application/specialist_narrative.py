"""Preserve specialist conclusions and their explicitly written recommendations."""
import re

from mdrk_builder.domain import SpecialistRole

_END = re.compile(
    r"^(?:медицинский\s+(?:психолог|логопед)|врач\s*[:_]|подпись|"
    r"реабилитационн\w*\s+задач|задачи\s+медицинской|"
    r"отмечается\b.*\bдинамик\w*|функциональный\s+диагноз|нейропсихологический\s+статус|логопедический\s+статус)\b", re.I,
)
_ADVICE = re.compile(r"^(?:рекомендован[оаы]?\b|рекомендации\b|на\s+основании\s+данных\b.*рекомендован[оаы]?\b)", re.I)


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
