"""Reconcile clinician edits with refreshed source facts without losing new rows."""
from copy import deepcopy
from dataclasses import fields, is_dataclass, replace
from datetime import datetime

from mdrk_builder.domain import ReviewIssue, ReviewSeverity


def row_key(row):
    source = str(getattr(row, 'source', '') or '')
    if hasattr(row, 'code') and hasattr(row, 'initial'):
        return ('icf', row.code, row.specialist, source)
    if hasattr(row, 'intervention'):
        return ('reverse', row.intervention, source, row.performed_at)
    if hasattr(row, 'scales'):
        return ('finding', row.role, source)
    if hasattr(row, 'name'):
        return ('row', getattr(row, 'role', getattr(row, 'specialist', '')), row.name, source, getattr(row, 'measured_at', None))
    return ('row', source, repr(row))


def _index_rows(rows):
    counts, result = {}, {}
    for row in rows:
        key = row_key(row)
        occurrence = counts.get(key, 0)
        counts[key] = occurrence + 1
        result[(key, occurrence)] = row
    return result


def merge_rows(baseline, edited, incoming):
    """Three-way merge. Preserve deliberate deletions and field edits, not whole lists."""
    base = _index_rows(baseline)
    old = _index_rows(edited)
    fresh = _index_rows(incoming)
    result, messages = [], []
    for key, new in fresh.items():
        if key in base and key not in old:
            continue
        current = old.get(key)
        if current is None:
            result.append(deepcopy(new))
            continue
        original = base.get(key)
        changes = {}
        superseded_fields = set()
        if hasattr(current, 'initial_measured_at') and hasattr(current, 'final_measured_at'):
            for phase in ('initial', 'final'):
                old_at = getattr(current, phase + '_measured_at')
                new_at = getattr(new, phase + '_measured_at')
                if old_at is not None and new_at is not None and old_at != new_at:
                    superseded_fields.update({phase, phase + '_source', phase + '_measured_at'})
                elif phase in current.manual_fields:
                    for suffix in ('', '_source', '_measured_at'):
                        changes[phase + suffix] = deepcopy(getattr(current, phase + suffix))
        for field in fields(current):
            name = field.name
            if name in {'source', 'field_sources', 'manual_fields', 'conflict_choices'} or name in superseded_fields:
                continue
            value = getattr(current, name)
            before = getattr(original, name) if original is not None else getattr(new, name)
            if name == 'scales':
                merged, notes = merge_rows(before, value, getattr(new, name))
                changes[name] = type(value)(merged)
                messages.extend(notes)
            elif value != before or name in getattr(current, 'manual_fields', ()):
                changes[name] = deepcopy(value)
                if getattr(new, name) != before and getattr(new, name) != value:
                    messages.append(f'{getattr(current, "name", getattr(current, "code", "Строка"))}: {name}; источник: {getattr(new, name)}; ручная правка: {value}')
        result.append(replace(deepcopy(new), **changes))
        if hasattr(current, 'manual_fields'):
            result[-1].manual_fields.update(current.manual_fields - superseded_fields)
        if hasattr(current, 'conflict_choices'):
            for phase in ('initial', 'final'):
                if phase in result[-1].manual_fields:
                    result[-1].conflict_choices.pop(phase, None)
    for key, row in old.items():
        if key not in fresh and (key not in base or row != base[key]):
            result.append(deepcopy(row))
            if key in base:
                messages.append(f'Источник строки больше не найден; ручная правка сохранена: {getattr(row, "name", getattr(row, "code", "Строка"))}')
    return result, messages


def merge_issues(messages):
    return [ReviewIssue('manual_source_conflict', text, ReviewSeverity.WARNING, 'manual_edits') for text in messages]


def hospitalization_days(admission, discharge):
    if admission is None or discharge is None or discharge < admission:
        return None
    return max(1, (discharge.date() - admission.date()).days)


def change_summary(before, after):
    if before is None:
        return ''
    added = removed = changed = 0
    for name in ('icf_domains', 'procedures', 'findings', 'rows', 'completed_procedures', 'team_findings', 'admission_scale_rows', 'discharge_scale_rows'):
        if not hasattr(after, name):
            continue
        old = {row_key(row): row for row in getattr(before, name, ())}
        new = {row_key(row): row for row in getattr(after, name)}
        added += len(new.keys() - old.keys())
        removed += len(old.keys() - new.keys())
        changed += sum(old[key] != new[key] for key in old.keys() & new.keys())
    return f'Изменения источников: добавлено {added}, изменено {changed}, удалено {removed} строк.'
