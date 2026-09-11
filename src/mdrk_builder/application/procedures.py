"""Merge execution sheets and project plans/executions at a document boundary."""
from dataclasses import replace
from datetime import datetime
from mdrk_builder.domain import MdrkKind, Procedure
from mdrk_builder.application.extractors import _infer_procedure_frequency


def merge_procedures(rows: list[Procedure]) -> list[Procedure]:
    grouped = {}
    for row in rows:
        key = (row.code.casefold(), ' '.join(row.name.casefold().split()),
               row.specialist.casefold(), row.duration_minutes)
        grouped.setdefault(key, []).append(row)
    result = []
    for copies in grouped.values():
        sample = copies[0]
        paths = tuple(dict.fromkeys(path for row in copies
                                   for path in (row.source_paths or ((row.source,) if row.source else ()))))
        dated = all(row.actual_count == len(row.performed_dates) for row in copies)
        dates = tuple(sorted({day for row in copies for day in row.performed_dates}))
        plans = {row.planned_count for row in copies if row.planned_count is not None}
        frequencies = {row.planned_frequency for row in copies if row.planned_frequency}
        ambiguous = any(row.count_needs_review for row in copies) or len(plans) > 1 or len(frequencies) > 1
        # Without dated marks we cannot tell a continuation from a duplicate.
        count = len(dates) if dated else (sample.actual_count if len(copies) == 1 else None)
        result.append(replace(sample, actual_count=count, performed_dates=dates,
            source_paths=paths, count_needs_review=ambiguous or not dated,
            planned_count=next(iter(plans)) if len(plans) == 1 else None,
            planned_frequency=next(iter(frequencies)) if len(frequencies) == 1 else '',
            frequency=_infer_procedure_frequency(dates, count) if count is not None else ''))
    return result


def select_procedures(rows, admission: datetime | None, boundary: datetime | None,
                      kind: MdrkKind = MdrkKind.FINAL) -> tuple[Procedure, ...]:
    result = []
    for row in rows:
        if kind is MdrkKind.INITIAL:
            # Preserve the source plan separately; completed marks supply the
            # displayed count only when no explicit or manually cleared plan exists.
            count = (row.planned_count
                     if row.planned_count is not None or 'planned_count' in row.manual_fields
                     else row.actual_count)
            result.append(replace(row, actual_count=count,
                                  frequency=(row.planned_frequency if 'planned_frequency' in row.manual_fields
                                             else row.planned_frequency or row.frequency), performed_dates=()))
            continue
        dates = tuple(day for day in row.performed_dates
                      if (admission is None or day >= admission.date())
                      and (boundary is None or day <= boundary.date()))
        if dates == row.performed_dates:
            result.append(replace(row))
            continue
        manual = 'actual_count' in row.manual_fields
        complete = row.actual_count == len(row.performed_dates)
        result.append(replace(row, performed_dates=dates,
            actual_count=row.actual_count if manual else len(dates) if complete else None,
            frequency=row.frequency if 'frequency' in row.manual_fields else _infer_procedure_frequency(dates, len(dates)) if complete else '',
            count_needs_review=row.count_needs_review or manual or not complete))
    return tuple(result)
