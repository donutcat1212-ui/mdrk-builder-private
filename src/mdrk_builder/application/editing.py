"""Reconcile clinician edits with refreshed source facts without losing new rows."""
from copy import deepcopy
from dataclasses import fields, replace

from mdrk_builder.domain import (
    DischargeScaleRow, DischargeTeamFinding, IcfDomain, Procedure,
    ReverseSheetRow, ReviewIssue, ReviewSeverity, ScaleMeasurement, SpecialistFinding,
)

EditableRow = IcfDomain | Procedure | ReverseSheetRow | ScaleMeasurement | SpecialistFinding | DischargeScaleRow | DischargeTeamFinding


def _normalized(value):
    return " ".join(str(value or "").casefold().split())


def row_key(row: EditableRow) -> tuple[str, ...]:
    """Source identity survives edits to the row's visible identifying fields."""
    if row.origin_key is not None:
        return row.origin_key
    source = str(row.source or '')
    if isinstance(row, IcfDomain):
        code, description, role = row.key
        return ('icf', code, description, role.value, source)
    if isinstance(row, Procedure):
        return ('procedure', _normalized(row.name), _normalized(row.specialist), source)
    if isinstance(row, ReverseSheetRow):
        return ('reverse', _normalized(row.intervention), source, str(row.performed_at or ''))
    if isinstance(row, ScaleMeasurement):
        return ('measurement', row.specialist.value, _normalized(row.name), source, str(row.measured_at or ''))
    if isinstance(row, SpecialistFinding):
        return ('finding', row.role.value, source, str(row.source_datetime or ''))
    if isinstance(row, DischargeScaleRow):
        return ('discharge_scale', row.role.value, _normalized(row.name), source,
                str(row.current_at or ''), str(row.initial_source or ''), str(row.initial_at or ''))
    if isinstance(row, DischargeTeamFinding):
        return ('team', row.role.value, source, str(row.occurred_at or ''))
    raise TypeError(f'Unsupported editable row: {type(row).__name__}')


def mark_manual_changes(previous: EditableRow, current: EditableRow) -> None:
    """Anchor provenance before marking changed clinical fields."""
    ignored = {'manual_fields', 'origin_key', 'source', 'initial_source', 'final_source',
               'field_sources', 'scales', 'origin_note'}
    object.__setattr__(current, 'origin_key', row_key(previous))
    object.__setattr__(current, 'manual_fields', set(previous.manual_fields))
    for item in fields(current):
        if item.name not in ignored and getattr(previous, item.name) != getattr(current, item.name):
            current.manual_fields.add(item.name)


def _index_rows(rows):
    result = {}
    for row in rows:
        result.setdefault(row_key(row), []).append(row)
    return result


def merge_rows(baseline, edited, incoming):
    """Three-way merge. Preserve deliberate deletions and field edits, not whole lists."""
    base = _index_rows(baseline)
    old = _index_rows(edited)
    fresh = _index_rows(incoming)
    result, messages = [], []
    for key, new_rows in fresh.items():
        if key in base and key not in old:
            continue
        previous_rows = base.get(key, [])
        edited_rows = old.get(key, [])
        if max(len(previous_rows), len(edited_rows), len(new_rows)) > 1:
            if edited_rows and edited_rows != previous_rows:
                result.extend(deepcopy(edited_rows))
                messages.append('Несколько строк имеют одинаковую исходную идентичность. '
                                'Ручная группа сохранена без сопоставления по позиции; проверьте строки источника: '
                                + str(new_rows[0].source or 'источник не указан'))
            else:
                result.extend(deepcopy(new_rows))
            continue
        new = new_rows[0]
        current = edited_rows[0] if edited_rows else None
        if current is None:
            result.append(deepcopy(new))
            continue
        original = previous_rows[0] if previous_rows else None
        changes = {}
        superseded_fields = set()
        if isinstance(current, IcfDomain):
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
            if name in {'source', 'field_sources', 'manual_fields', 'conflict_choices', 'origin_key'} or name in superseded_fields:
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
        result.append(replace(deepcopy(new), origin_key=current.origin_key, **changes))
        if hasattr(current, 'manual_fields'):
            result[-1].manual_fields.update(current.manual_fields - superseded_fields)
        if hasattr(current, 'conflict_choices'):
            for phase in ('initial', 'final'):
                if phase in result[-1].manual_fields:
                    result[-1].conflict_choices.pop(phase, None)
    for key, rows in old.items():
        if key not in fresh and (key not in base or rows != base[key]):
            result.extend(deepcopy(rows))
            if key in base:
                row = rows[0]
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
