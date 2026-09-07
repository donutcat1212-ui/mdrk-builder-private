from datetime import date, timedelta

import pytest

from mdrk_builder.application.extractors import _infer_procedure_frequency, extract_procedures
from mdrk_builder.infrastructure.ooxml_reader import ParsedCell, ParsedRow, ParsedTable, ParsedDocument
from pathlib import Path


@pytest.mark.parametrize(('offsets', 'expected'), [
    ([0, 1, 2, 3, 4, 7, 8, 9, 10, 11], 'ежедневно'),
    ([4, 7, 8], 'ежедневно'),  # Friday, Monday, Tuesday.
    (list(range(14)), 'ежедневно'),
    ([0, 2, 4, 7, 9, 11, 14], '3 раза в неделю'),
    ([0, 2, 4, 7, 9], '3 раза в неделю'),
    ([2, 4, 7, 9, 11], '3 раза в неделю'),
    ([2, 4, 7, 9, 11, 14, 16], '3 раза в неделю'),  # Partial edge weeks.
    ([0, 3, 7, 10, 14, 17], '2 раза в неделю'),
    ([0, 7, 14], '1 раз в неделю'),
    ([0, 1, 3, 4, 7, 8, 10, 11], '4 раза в неделю'),
    ([0, 2, 4, 6, 8], '1 раз в 2 дня'),
    ([0, 3, 6, 9], '1 раз в 3 дня'),
    ([0, 1, 4, 5, 8, 9], '2 раза в 4 дня'),
    ([0, 14, 28], '1 раз в 14 дней'),
    ([0, 2, 4, 8, 10, 13], 'периодически'),  # Equal weekly totals, different weekdays.
    ([0, 2, 4, 7, 11, 14, 16, 18], 'периодически'),  # Missing internal session.
    ([0, 3], 'периодически'),  # One interval does not establish a pattern.
    ([0], 'однократно'),
    ([], ''),
])
def test_frequency_patterns(offsets, expected):
    monday = date(2026, 8, 3)
    dates = tuple(monday + timedelta(days=offset) for offset in offsets)
    assert _infer_procedure_frequency(dates, len(dates)) == expected


def test_missing_or_duplicate_dates_do_not_invent_a_pattern():
    dates = (date(2026, 8, 3), date(2026, 8, 5), date(2026, 8, 7))
    assert _infer_procedure_frequency(dates, 4) == 'периодически'
    assert _infer_procedure_frequency(dates + dates[:1], 4) == 'периодически'


def test_assignment_table_frequency_uses_marked_dates_across_year_boundary():
    dates = [date(2026, 12, 28) + timedelta(days=n) for n in range(19)]
    def row(values):
        return ParsedRow(tuple(ParsedCell(i, 1, value) for i, value in enumerate(values)), len(values))
    table = ParsedTable((
        row(['Назначения', 'время', 'кабинет', *(d.strftime('%d.%m.%Y') for d in dates)]),
        row(['A19.23.002.014 Индивидуальное занятие ЛФК', '', '',
             *('+' if d.weekday() in {0, 2, 4} else '' for d in dates)]),
    ))
    document = ParsedDocument(source_path=Path('/synthetic/assignments.docx'), normalized_path=Path('/synthetic/assignments.docx'), paragraphs=(), tables=(table,))
    procedure = extract_procedures(document)[0]
    assert procedure.actual_count == 9
    assert procedure.frequency == '3 раза в неделю'
