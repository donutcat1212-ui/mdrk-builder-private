"""Keep displayed discharge demographics consistent with confirmed identity fields."""
import re

from mdrk_builder.application.discharge_extractors import update_header_period


def synchronize_header(draft):
    identity = draft.identity
    reference = draft.discharge_datetime or draft.admission_datetime
    age = None
    if identity.birth_date and reference:
        age = reference.year - identity.birth_date.year - ((reference.month, reference.day) < (identity.birth_date.month, identity.birth_date.day))
    birth = identity.birth_date.strftime('%d.%m.%Y') if identity.birth_date else ''
    if age is not None and age >= 0:
        unit = 'лет' if 11 <= age % 100 <= 14 else 'год' if age % 10 == 1 else 'года' if 2 <= age % 10 <= 4 else 'лет'
        birth += f' ({age} {unit})'
    rules = (
        (r'^(?:Фамилия,? имя,? отчество[^:]*|ФИО(?: пациента)?|Пациент)\s*:', identity.full_name),
        (r'^Дата рождения[^:]*:', birth),
        (r'^Пол\s*:', identity.sex),
        (r'^(?:Номер медицинской карты[^:]*|Номер ИБ|Номер медкарты)\s*:?', identity.medical_record_number),
    )
    lines = []
    for line in draft.header_text.splitlines():
        for pattern, value in rules:
            match = re.match(pattern, line, re.I)
            if match:
                # Header labels may omit a colon, especially the record number.
                label = match.group().rstrip(': ').strip()
                if label.casefold().startswith('номер медицинской карты'):
                    label = 'Номер медицинской карты'
                line = label + ': ' + value
                break
        lines.append(line)
    draft.header_text = update_header_period('\n'.join(lines), draft.admission_datetime, draft.discharge_datetime)
