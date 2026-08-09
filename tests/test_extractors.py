from datetime import datetime
from pathlib import Path

from mdrk_builder.application.extractors import (
    extract_clinical_datetime,
    extract_clinical_sections,
    extract_conclusion,
    extract_icf_observations,
    extract_mdrk_meeting_datetimes,
    extract_patient_identity,
    extract_procedures,
    extract_scale_measurements,
)
from mdrk_builder.domain import SpecialistRole
from mdrk_builder.infrastructure.ooxml_reader import (
    BodyItem,
    ParsedCell,
    ParsedDocument,
    ParsedRow,
    ParsedTable,
)


def _document(
    text: str = "",
    *,
    path: str = "/patient/source.docx",
    tables: list[ParsedTable] | None = None,
) -> ParsedDocument:
    paragraphs = [text] if text else []
    parsed_tables = tables or []
    return ParsedDocument(
        source_path=Path(path),
        normalized_path=Path(path),
        paragraphs=paragraphs,
        tables=parsed_tables,
        body_items=[
            *([BodyItem("paragraph", 0)] if paragraphs else []),
            *(BodyItem("table", index) for index in range(len(parsed_tables))),
        ],
    )


def _row(values: dict[int, str], logical_cols: int) -> ParsedRow:
    return ParsedRow(
        tuple(ParsedCell(column, 1, value) for column, value in sorted(values.items())),
        logical_cols,
    )


def test_clinical_datetime_prefers_labeled_document_time_over_filename() -> None:
    document = _document(
        "\n".join(
            (
                "ПЕРВИЧНЫЙ ОСМОТР СПЕЦИАЛИСТА ПО ФИЗИЧЕСКОЙ РЕАБИЛИТАЦИИ",
                '"05"июня 2026 г. время: 14 час.30 мин.',
            )
        ),
        path="/patient/фт/1 осмотр_05.06.26_16.10.docx",
    )

    assert extract_clinical_datetime(document) == datetime(2026, 6, 5, 14, 30)


def test_identity_preserves_skp_prefix_and_generic_fio_label() -> None:
    document = _document(
        "Фамилия, имя, отчество: Астраханский Алексей Юрьевич\n"
        "СКП: 4318 /26\nДата рождения: 20.02.1968\nПол: муж"
    )

    identity = extract_patient_identity(document)

    assert identity.full_name == "Астраханский Алексей Юрьевич"
    assert identity.medical_record_number == "СКП4318/26"
    assert identity.sex == "мужской"
    assert identity.birth_date and identity.birth_date.isoformat() == "1968-02-20"


def test_sections_stop_at_neighbor_headings_and_split_diagnostics() -> None:
    document = _document(
        "\n".join(
            (
                "Клинический диагноз: I69.3",
                "Реабилитационный диагноз: не копировать",
                "Факторы риска проведения реабилитационных мероприятий: нет",
                "Диагноз клинический: не копировать",
                "Медикаментозная терапия: Лозартан 25 мг",
                "немедикаментозное лечение: ЛФК",
                "Свободный двигательный режим;",
                "Диета: ОВД",
                "Фамилия, имя, отчество: Врач Тест Тестович",
                "Пациентом предоставлены необходимые для госпитализации документы Клинический анализ крови: норма",
                "ВИЧ, вирусные гепатиты: отрицательно",
                "ЭКГ от 05.06.2026: ритм синусовый",
                "Рентгенография ОГК: без патологии",
                "Физикальное исследование, локальный статус",
            )
        )
    )

    sections = extract_clinical_sections(document)

    assert sections["clinical_diagnosis"] == "I69.3"
    assert sections["risks"] == "нет"
    assert sections["medication"] == "Лозартан 25 мг"
    assert sections["movement_regimen"] == "свободный"
    assert sections["diet"] == "ОВД"
    assert "Клинический анализ" in sections["laboratory_results"]
    assert sections["instrumental_results"].startswith("ЭКГ")


def test_conclusion_ignores_historical_label_and_uses_neuropsych_status() -> None:
    document = _document(
        "\n".join(
            (
                "Заключение по результатам предшествующего обследования: старое",
                "Нейропсихологический статус:",
                "1. Сохранность высших психических функций.",
                "Исследование анамнеза: дальше не включать",
            )
        )
    )

    assert extract_conclusion(document, SpecialistRole.NEUROPSYCHOLOGIST) == (
        "1. Сохранность высших психических функций."
    )


def test_icf_extracts_owner_and_preserves_merged_personal_factor() -> None:
    table = ParsedTable(
        (
            _row({0: "МКФ", 13: "Ответственный специалист МДРК"}, 15),
            _row({0: "b2351", 1: "Равновесие", 11: "2", 12: "1", 13: "ФТ"}, 15),
            ParsedRow(
                (ParsedCell(0, 1, "Pf"), ParsedCell(1, 14, "Мужчина 58 лет")),
                15,
            ),
        )
    )

    observations = extract_icf_observations(_document(tables=[table]))

    assert observations[0].specialist is SpecialistRole.PHYSICAL_THERAPIST
    assert [value.display() for value in observations[0].ratings] == ["2", "1"]
    assert observations[0].note == ""
    assert observations[1].code == "Pf"
    assert observations[1].description == "Мужчина 58 лет"
    assert observations[1].ratings == ()


def test_cyrillic_e_in_icf_code_is_normalized() -> None:
    table = ParsedTable(
        (
            _row({0: "МКФ"}, 15),
            _row({0: "е1101", 1: "Лекарственные препараты", 11: "4+"}, 15),
        )
    )

    observations = extract_icf_observations(_document(tables=[table]))

    assert observations[0].code == "e1101"


def test_procedure_count_is_number_of_plus_marks_and_zero_is_not_missing() -> None:
    table = ParsedTable(
        (
            _row({0: "Назначения", 1: "время", 2: "кабинет"}, 6),
            _row(
                {
                    0: "A19.23.002.014 Индивидуальное занятие ЛФК",
                    1: "08:00",
                    2: "42",
                    3: "+",
                    4: "10.55++",
                    5: "30 мин",
                },
                6,
            ),
            _row(
                {
                    0: "A13.23.007 Медико-логопедическая процедура",
                    1: "13:00",
                    2: "холл",
                    5: "20 мин",
                },
                6,
            ),
        )
    )

    procedures = extract_procedures(_document(tables=[table]))

    assert [item.actual_count for item in procedures] == [2, 0]
    assert [item.frequency for item in procedures] == ["", ""]
    assert [item.duration_minutes for item in procedures] == [30, 20]
    assert procedures[0].count_needs_review


def test_scale_short_date_headers_use_document_year_and_role() -> None:
    table = ParsedTable(
        (
            _row({0: "", 1: "05.06", 2: "19.06"}, 3),
            _row({0: "Шкала баланса Берга", 1: "44", 2: "48"}, 3),
        )
    )
    document = _document(tables=[table], path="/patient/ft.docx")

    values = extract_scale_measurements(
        document,
        SpecialistRole.PHYSICAL_THERAPIST,
        datetime(2026, 6, 19, 11, 30),
    )

    assert [(item.value, item.measured_at) for item in values] == [
        ("44", datetime(2026, 6, 5, 0, 0)),
        ("48", datetime(2026, 6, 19, 11, 30)),
    ]


def test_scale_date_only_uses_document_time_but_explicit_midnight_is_preserved() -> None:
    table = ParsedTable(
        (
            _row(
                {
                    0: "Дата и время расчета шкалы",
                    1: "Шкала/опросник",
                    2: "Результат расчета",
                },
                3,
            ),
            _row({0: "05.06.2026", 1: "Шкала Бартел", 2: "75"}, 3),
            _row({0: "05.06.2026 00:00", 1: "Индекс мобильности Ривермид", 2: "8"}, 3),
        )
    )

    values = extract_scale_measurements(
        _document(tables=[table]),
        SpecialistRole.FRM,
        datetime(2026, 6, 5, 15),
    )

    assert [(item.name, item.measured_at) for item in values] == [
        ("Шкала Бартел", datetime(2026, 6, 5, 15)),
        ("Индекс мобильности Ривермид", datetime(2026, 6, 5, 0, 0)),
    ]


def test_rankin_spelling_variants_share_one_canonical_scale_name() -> None:
    table = ParsedTable(
        (
            _row(
                {
                    0: "Дата и время расчета шкалы",
                    1: "Шкала/опросник",
                    2: "Результат расчета",
                },
                3,
            ),
            _row({0: "05.06.2026 13:00", 1: "Модифицированная шкала Ренкин", 2: "3"}, 3),
            _row({0: "05.06.2026 13:00", 1: "Модифицированная шкала Рэнкина", 2: "3"}, 3),
        )
    )

    values = extract_scale_measurements(
        _document(tables=[table]),
        SpecialistRole.FRM,
        datetime(2026, 6, 5, 13),
    )

    assert [(item.name, item.value) for item in values] == [
        ("Модифицированная шкала Рэнкина", "3")
    ]


def test_neuropsych_generic_total_is_named_from_document_context() -> None:
    table = ParsedTable(
        (
            _row({0: "", 1: "05.06"}, 2),
            _row({0: "Общий балл", 1: "30"}, 2),
        )
    )
    document = _document(
        "Монреальская шкала оценки психических функций: (от 0 до 30 баллов)",
        tables=[table],
    )

    values = extract_scale_measurements(
        document,
        SpecialistRole.NEUROPSYCHOLOGIST,
        datetime(2026, 6, 5, 15, 30),
    )

    assert values[0].name == "Монреальская шкала оценки психических функций"
    assert values[0].value == "30"


def test_scheduled_mdrk_rows_prefer_explicit_execution_time() -> None:
    table = ParsedTable(
        (
            _row({0: "Медицинское вмешательство", 1: "Дата назначения", 2: "Исполнение"}, 3),
            _row({0: "Консилиум МДРК", 1: "19.06.2026", 2: "19.06.2026 15:30"}, 3),
        )
    )

    values = extract_mdrk_meeting_datetimes(_document(tables=[table]))

    assert values[-1] == datetime(2026, 6, 19, 15, 30)


def test_physician_scale_extraction_rejects_copied_specialist_tables() -> None:
    table = ParsedTable(
        (
            _row(
                {
                    0: "Дата и время расчета шкалы",
                    1: "Шкала/опросник",
                    2: "Результат расчета",
                },
                3,
            ),
            _row({0: "19.06.2026 08:30", 1: "Шкала Бартел", 2: "80"}, 3),
            _row({0: "19.06.2026 11:30", 1: "Шкала баланса Берга", 2: "48"}, 3),
            _row({0: "19.06.2026 08:30", 1: "Шкала дизартрии", 2: "10"}, 3),
        )
    )

    values = extract_scale_measurements(
        _document(tables=[table]),
        SpecialistRole.NEUROLOGIST,
        datetime(2026, 6, 19, 8, 30),
    )

    assert [(item.name, item.value) for item in values] == [("Шкала Бартел", "80")]


def test_physician_scales_are_extracted_from_bounded_narrative_lines() -> None:
    document = _document(
        "\n".join(
            (
                "Шкалы:",
                "Модифицированная шкала Ренкин: 3",
                "NRS-2002: низкий риск",
                "Индекс мобильности Ривермид: 8",
                "Шкала Бартел: 75",
            )
        )
    )

    values = extract_scale_measurements(
        document,
        SpecialistRole.FRM,
        datetime(2026, 6, 5, 13),
    )

    assert [(item.name, item.value) for item in values] == [
        ("Модифицированная шкала Рэнкина", "3"),
        ("NRS 2002", "низкий риск"),
        ("Индекс мобильности Ривермид", "8"),
        ("Шкала Бартел", "75"),
    ]


def test_conclusion_stops_before_specialist_signature_line() -> None:
    document = _document(
        "Заключение: Отмечается положительная динамика.\n"
        "Никитин П.П., специалист по физической реабилитации /_______________/"
    )

    value = extract_conclusion(document, SpecialistRole.PHYSICAL_THERAPIST)

    assert value == "Отмечается положительная динамика."
