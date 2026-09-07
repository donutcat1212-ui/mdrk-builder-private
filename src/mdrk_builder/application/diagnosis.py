"""Compose three source-grounded diagnosis sections; never manufacture missing parts."""
import re
from mdrk_builder.domain import ReviewIssue, ReviewSeverity

LABELS = {'main': 'Основное заболевание', 'additional': 'Сопутствующие заболевания', 'factors': 'Дополнительные сведения о заболевании'}
PATTERNS = (
    ('main', r'^основн\w* (?:заболеван\w*|диагноз)'),
    ('additional', r'^(?:сопутствующ\w* заболеван\w*|дополнительный диагноз)'),
    ('factors', r'^дополнительн\w* (?:сведени\w*(?: о заболевании)?|фактор\w*)'),
)


def diagnosis_parts(text):
    parts = {name: [] for name in LABELS}
    active = 'main'
    for line in text.splitlines():
        line = line.strip()
        for name, pattern in PATTERNS:
            match = re.match(pattern, line.strip(), re.I)
            if match:
                active = name
                line = re.sub(pattern + r'[^:]*:\s*', '', line.strip(), flags=re.I) if ':' in line else line[match.end():].strip()
                break
        if line.strip() and line.strip().casefold() not in {'клинический:', 'клинический'}:
            parts[active].append(line.strip())
    return {name: '\n'.join(values) for name, values in parts.items()}


def compose_diagnosis(candidates):
    """Candidates are ordered primary first, then other same-episode clinical sources."""
    chosen, sources, issues = {}, {}, []
    for path, text in candidates:
        for part, value in diagnosis_parts(text).items():
            if not value:
                continue
            if part not in chosen:
                chosen[part], sources['clinical_diagnosis.' + part] = value, path
            elif re.sub(r'\s+', ' ', chosen[part]).casefold() != re.sub(r'\s+', ' ', value).casefold():
                issues.append(ReviewIssue('diagnosis_source_conflict', f'{LABELS[part]}: выбранный источник — {sources["clinical_diagnosis." + part].name}; другой вариант в {path.name}: {value}', ReviewSeverity.WARNING, 'clinical_diagnosis.' + part, path))
    text = '\n\n'.join(LABELS[name] + ':\n' + chosen.get(name, '') for name in LABELS)
    return (text if chosen else ''), sources, issues


def diagnosis_choices(candidates):
    result = {}
    for source, text in candidates:
        for name, value in diagnosis_parts(text).items():
            if value:
                result.setdefault('clinical_diagnosis.'+name, []).append((value,source))
    return {key: rows for key, rows in result.items() if len({value.strip().casefold() for value, _ in rows}) > 1}
