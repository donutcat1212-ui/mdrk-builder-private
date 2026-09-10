"""Editable field rows for the discharge's heterogeneous clinical table."""
from dataclasses import replace

from mdrk_builder.application.editing import mark_manual_changes
from mdrk_builder.domain import DischargeTeamFinding, Procedure
from mdrk_builder.ui.episode_adapter import (
    format_datetime, parse_optional_datetime, parse_optional_nonnegative_int, role_from_name,
)

PROCEDURE_FIELDS = (
    ("code", "Код"), ("name", "Процедура"), ("specialist", "Исполнитель"),
    ("actual_count", "Количество"), ("duration_minutes", "Длительность, мин"),
    ("frequency", "Кратность"),
)
FINDING_FIELDS = (
    ("role", "Специалист"), ("specialist_name", "ФИО специалиста"),
    ("specialist_title", "Должность в документе"),
    ("occurred_at", "Дата осмотра"), ("conclusion", "Заключение и рекомендации"),
)
SCALE_FIELDS = (
    ("role", "Специалист"), ("name", "Шкала"),
    ("initial_value", "Исходное значение"), ("value", "Текущее значение"),
    ("initial_at", "Дата исходной оценки"), ("current_at", "Дата текущей оценки"),
)


def field_rows(row, *, child=False):
    specs = (PROCEDURE_FIELDS if isinstance(row, Procedure) else
             FINDING_FIELDS if isinstance(row, DischargeTeamFinding) else SCALE_FIELDS)
    for name, label in specs:
        if child and name == "role":
            continue
        value = getattr(row, name)
        if name == "role":
            text = value.display_name
        elif name.endswith("_at"):
            text = format_datetime(value)
        else:
            text = str(value) if value is not None else ""
        yield name, label, text


def edit_field(row, name, value):
    if name not in {field for field, _, _ in field_rows(row)}:
        raise ValueError("Это поле нельзя редактировать")
    value = value.strip()
    if name == "role":
        value = role_from_name(value)
    elif name.endswith("_at"):
        value = parse_optional_datetime(value)
    elif name in {"actual_count", "duration_minutes"}:
        value = parse_optional_nonnegative_int(value, name)
    edited = replace(row, **{name: value}, manual_fields=set(row.manual_fields))
    mark_manual_changes(row, edited)
    return edited
