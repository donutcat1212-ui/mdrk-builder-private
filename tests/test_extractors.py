from datetime import date, datetime
from pathlib import Path

from mdrk_builder.application.extractors import (
    extract_clinical_datetime,
    extract_clinical_sections,
    extract_conclusion,
    extract_icf_observations,
    extract_mdrk_document_datetime,
    extract_mdrk_meeting_datetimes,
    extract_mdrk_scale_measurements,
    extract_patient_identity,
    extract_procedures,
    extract_scale_measurements,
    extract_specialist_name,
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
    path: str = "fixtures/source.docx",
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
        path="fixtures/фт/1 осмотр_05.06.26_16.10.docx",
    )

    assert extract_clinical_datetime(document) == datetime(2026, 6, 5, 14, 30)


def test_clinical_datetime_accepts_space_inside_numeric_date() -> None:
    document = _document(
        "Дата приема, время: 03.08. 2026, 16:00\n"
        "Первичное обследование нейропсихолога"
    )

    assert extract_clinical_datetime(document) == datetime(2026, 8, 3, 16)


def test_specialist_name_comes_from_role_signature_not_scale_attribution() -> None:
    document = _document(
        "Оценка по шкале КОЗЫРЕВОЙ: 12 баллов\n"
        "ФАМИЛИЯ А.Д.\n"
        "Медицинский психолог (нейропсихолог)__________СОТРУДНИК А.Д."
    )

    assert (
        extract_specialist_name(document, SpecialistRole.NEUROPSYCHOLOGIST)
        == "СОТРУДНИК А.Д."
    )


def test_treating_neurologist_wins_over_department_head_on_shared_line() -> None:
    document = _document(
        "СОТРУДНИК А.А., лечащий врач, врач-невролог /___/ "
        "РУКОВОДИТЕЛЬ Б.Б., заведующий отделением"
    )

    assert (
        extract_specialist_name(document, SpecialistRole.NEUROLOGIST)
        == "СОТРУДНИК А.А."
    )


def test_identity_preserves_skp_prefix_and_generic_fio_label() -> None:
    document = _document(
        "Фамилия, имя, отчество: АЛЬФА БЕТА ГАММА\n"
        "СКП: 0000 /00\nДата рождения: 01.01.1900\nПол: муж"
    )

    identity = extract_patient_identity(document)

    assert identity.full_name == "АЛЬФА БЕТА ГАММА"
    assert identity.medical_record_number == "СКП0000/00"
    assert identity.sex == "мужской"
    assert identity.birth_date and identity.birth_date.isoformat() == "1900-01-01"


def test_sections_stop_at_neighbor_headings_and_split_diagnostics() -> None:
    document = _document(
        "\n".join(
            (
                "Клинический диагноз: ДИАГНОЗ_ТЕСТ",
                "Реабилитационный диагноз: не копировать",
                "Факторы риска проведения реабилитационных мероприятий: нет",
                "Диагноз клинический: не копировать",
                "Медикаментозная терапия: ТЕРАПИЯ_ТЕСТ",
                "немедикаментозное лечение: ЛФК",
                "Свободный двигательный режим;",
                "Диета: ОВД",
                "Фамилия, имя, отчество: ДЕЛЬТА ЭПСИЛОН ДЗЕТА",
                "Пациентом предоставлены необходимые для госпитализации документы Клинический анализ крови: ЛАБ_1",
                "Клинический анализ мочи: ЛАБ_2",
                "ЭКГ от 05.06.2026: ИНСТ_1",
                "Рентгенография ОГК: ИНСТ_2",
                "Физикальное исследование, локальный статус",
            )
        )
    )

    sections = extract_clinical_sections(document)

    assert sections["clinical_diagnosis"] == "ДИАГНОЗ_ТЕСТ"
    assert sections["risks"] == "нет"
    assert sections["medication"] == "ТЕРАПИЯ_ТЕСТ"
    assert sections["movement_regimen"] == "свободный"
    assert sections["diet"] == "ОВД"
    assert "Клинический анализ" in sections["laboratory_results"]
    assert sections["instrumental_results"].startswith("ЭКГ")


def test_specialist_rehabilitation_plan_extracts_explicit_goal_and_tasks() -> None:
    document = _document(
        "\n".join(
            (
                "Задача на этап МР: через 17 дней пациент ведёт диалог.",
                "Короткосрочная задача реабилитации №1: Улучшить речевой выдох.",
                "Короткосрочная задача реабилитации №2: Улучшить артикуляцию.",
                "На основании данных обследования рекомендовано:",
                "При выполнении сложных задач использовать самоинструкции.",
            )
        )
    )

    sections = extract_clinical_sections(document)

    assert sections["goal"] == "через 17 дней пациент ведёт диалог."
    assert sections["tasks"].splitlines() == [
        "Улучшить речевой выдох.",
        "Улучшить артикуляцию.",
    ]


def test_specialist_rehabilitation_task_block_stops_before_recommendations() -> None:
    document = _document(
        "\n".join(
            (
                "Реабилитационные задачи на этап МР:",
                "• Развитие силовой выносливости;",
                "• Улучшение функции равновесия;",
                "На основании данных обследования рекомендовано:",
                "• Индивидуальное занятие лечебной физкультурой;",
            )
        )
    )

    sections = extract_clinical_sections(document)

    assert sections["tasks"].splitlines() == [
        "Развитие силовой выносливости;",
        "Улучшение функции равновесия;",
    ]


def test_section_does_not_absorb_discharge_metadata() -> None:
    document = _document(
        "Анамнез заболевания: АНАМНЕЗ_ТЕСТ.\n"
        "Дата и время выписки: 20.08.2026 12:00\n"
        "Клинический диагноз: ДИАГНОЗ_ТЕСТ"
    )

    sections = extract_clinical_sections(document)

    assert sections["disease_history"] == "АНАМНЕЗ_ТЕСТ."
    assert "выписк" not in sections["disease_history"].casefold()


def test_sections_split_completed_interventions_and_prefixed_movement_regimen() -> None:
    document = _document(
        "\n".join(
            (
                "Выполненные медицинские вмешательства Клинический анализ крови: ЛАБ_1",
                "Клинический анализ мочи: ЛАБ_2",
                "Рентгенография ОГК: ИНСТ_1",
                "ЭКГ: ИНСТ_2",
                "Консультация хирурга: не копировать",
                "План лечения Двигательный режим свободный",
            )
        )
    )

    sections = extract_clinical_sections(document)

    assert sections["laboratory_results"].startswith("Клинический анализ крови")
    assert "Консультация" not in sections["instrumental_results"]
    assert sections["instrumental_results"].startswith("Рентгенография")
    assert sections["movement_regimen"] == "свободный"


def test_plan_treatment_preserves_medication_lines_and_short_regimen() -> None:
    document = _document(
        "\n".join(
            (
                "План лечения Режим свободный",
                "Диета ОВД",
                "Назначения Медикаментозная терапия:",
                "ТЕРАПИЯ_СТРОКА_1",
                "ТЕРАПИЯ_СТРОКА_2",
                "ТЕРАПИЯ_СТРОКА_3",
                "Немедикаментозная терапия:",
            )
        )
    )

    sections = extract_clinical_sections(document)

    assert sections["movement_regimen"] == "свободный"
    assert sections["medication"].splitlines() == [
        "ТЕРАПИЯ_СТРОКА_1",
        "ТЕРАПИЯ_СТРОКА_2",
        "ТЕРАПИЯ_СТРОКА_3",
    ]


def test_completed_interventions_split_inline_instrumental_marker() -> None:
    document = _document(
        "Выполненные медицинские вмешательства "
        "Клинический анализ крови от 01.08.2026: ЛАБ_1. "
        "Рентгенография ОГК от 01.08.2026: ИНСТ_1. "
        "План лечения: Двигательный режим: свободный"
    )

    sections = extract_clinical_sections(document)

    assert sections["laboratory_results"].startswith("Клинический анализ крови")
    assert sections["instrumental_results"].startswith("Рентгенография")
    assert "План лечения" not in sections["instrumental_results"]
    assert sections["movement_regimen"] == "свободный"


def test_numbered_completed_interventions_split_and_stop_cleanly() -> None:
    document = _document(
        "\n".join(
            (
                "1. Выполненные медицинские вмешательства Клинический анализ крови: ЛАБ_1",
                "1.1 Клинический анализ мочи: ЛАБ_2",
                "2. Рентгенография ОГК: ИНСТ_1",
                "2.1 ЭКГ: ИНСТ_2",
                "3. План лечения Двигательный режим свободный",
            )
        )
    )

    sections = extract_clinical_sections(document)

    assert sections["laboratory_results"].startswith("Клинический анализ крови")
    assert "Рентгенография" not in sections["laboratory_results"]
    assert "План лечения" not in sections["instrumental_results"]
    assert sections["instrumental_results"].startswith("2. Рентгенография")
    assert sections["movement_regimen"] == "свободный"


def test_conclusion_ignores_historical_label_and_uses_neuropsych_status() -> None:
    document = _document(
        "\n".join(
            (
                "Заключение по результатам предшествующего обследования: МАРКЕР_СТАРЫЙ",
                "Нейропсихологический статус:",
                "1. ЗАКЛЮЧЕНИЕ_НЕЙРОПСИХОЛОГА.",
                "Исследование анамнеза: дальше не включать",
            )
        )
    )

    assert extract_conclusion(document, SpecialistRole.NEUROPSYCHOLOGIST) == (
        "Нейропсихологический статус:\n1. ЗАКЛЮЧЕНИЕ_НЕЙРОПСИХОЛОГА."
    )


def test_conclusion_keeps_neuropsych_status_and_basis_but_not_later_dynamics() -> None:
    document = _document(
        "\n".join(
            (
                "Нейропсихологический статус и топический диагноз :",
                "1. ЗАКЛЮЧЕНИЕ_СТРОКА_1.",
                "2. ЗАКЛЮЧЕНИЕ_СТРОКА_2.",
                "Количественная оценка данных обследования:",
                "На основании данных рекомендован курс: ВКЛЮЧИТЬ_ОБОСНОВАНИЕ",
                "Отмечается положительная динамика: НЕ_ВКЛЮЧАТЬ",
            )
        )
    )

    assert extract_conclusion(document, SpecialistRole.NEUROPSYCHOLOGIST) == (
        "Нейропсихологический статус и топический диагноз :\n"
        "1. ЗАКЛЮЧЕНИЕ_СТРОКА_1.\n"
        "2. ЗАКЛЮЧЕНИЕ_СТРОКА_2.\n"
        "На основании данных рекомендован курс: ВКЛЮЧИТЬ_ОБОСНОВАНИЕ"
    )


def test_neuropsych_topical_diagnosis_keeps_text_on_heading_line() -> None:
    document = _document(
        "Нейропсихологический статус и топический диагноз: ДИАГНОЗ_НЕЙРОПСИХОЛОГА\n"
        "Рекомендовано: занятия"
    )

    assert extract_conclusion(document, SpecialistRole.NEUROPSYCHOLOGIST) == (
        "Нейропсихологический статус и топический диагноз: ДИАГНОЗ_НЕЙРОПСИХОЛОГА"
    )


def test_logopedist_final_conclusion_is_dynamics_plus_discharge_status() -> None:
    document = _document(
        "На основании данных: НЕ_ВКЛЮЧАТЬ\n"
        "Динамика: ДИНАМИКА_ЛОГОПЕДА.\n"
        "Логопедический статус при выписке изменен:\n"
        "СТАТУС_СТРОКА_1.\nСТАТУС_СТРОКА_2.\n"
        "Медицинский логопед АЛЬФА А.А."
    )

    assert extract_conclusion(document, SpecialistRole.LOGOPEDIST).splitlines() == [
        "Динамика: ДИНАМИКА_ЛОГОПЕДА.",
        "Логопедический статус при выписке изменен:",
        "СТАТУС_СТРОКА_1.",
        "СТАТУС_СТРОКА_2.",
    ]


def test_logopedist_summary_paragraphs_are_used_as_conclusion() -> None:
    document = _document(
        "\n".join(
            (
                "ВВОДНЫЙ_ТЕКСТ.",
                "Т.о. ЗАКЛЮЧЕНИЕ_ЛОГОПЕДА.",
                "На основании данных РЕКОМЕНДАЦИЯ_ТЕСТ.",
                "Медицинский логопед АЛЬФА А.А.",
            )
        )
    )

    conclusion = extract_conclusion(document, SpecialistRole.LOGOPEDIST)

    assert conclusion.startswith("Т.о.")
    assert "РЕКОМЕНДАЦИЯ_ТЕСТ" in conclusion
    assert "АЛЬФА" not in conclusion


def test_icf_extracts_owner_and_preserves_merged_personal_factor() -> None:
    table = ParsedTable(
        (
            _row({0: "МКФ", 13: "Ответственный специалист МДРК"}, 15),
            _row({0: "b2351", 1: "Равновесие", 11: "2", 12: "1", 13: "ФТ"}, 15),
            ParsedRow(
                (ParsedCell(0, 1, "Pf"), ParsedCell(1, 14, "ПЕРСОНАЛЬНЫЙ_ФАКТОР_ТЕСТ")),
                15,
            ),
        )
    )

    observations = extract_icf_observations(_document(tables=[table]))

    assert observations[0].specialist is SpecialistRole.PHYSICAL_THERAPIST
    assert [value.display() for value in observations[0].ratings] == ["2", "1"]
    assert observations[0].note == ""
    assert observations[1].code == "Pf"
    assert observations[1].description == "ПЕРСОНАЛЬНЫЙ_ФАКТОР_ТЕСТ"
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
                    2: "КАБИНЕТ_А",
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
                    2: "КАБИНЕТ_Б",
                    5: "20 мин",
                },
                6,
            ),
        )
    )

    procedures = extract_procedures(_document(tables=[table]))

    assert [item.actual_count for item in procedures] == [2, 0]
    assert [item.frequency for item in procedures] == ["периодически", ""]
    assert [item.duration_minutes for item in procedures] == [30, 20]
    assert procedures[0].count_needs_review


def test_assignment_dates_infer_frequency_and_include_sis_without_code() -> None:
    header = {
        0: "Назначения",
        1: "время",
        2: "кабинет",
        **{column + 3: str(day) for column, day in enumerate(range(4, 18))},
        17: "время на процедуру",
    }
    daily_marks = {column + 3: "+" for column, day in enumerate(range(4, 18)) if day < 8 or 10 <= day <= 14 or day == 17}
    sis_marks = {column + 3: "+" for column, day in enumerate(range(4, 18)) if day in {7, 10, 11, 12, 13, 14}}
    irregular_marks = {column + 3: "+" for column, day in enumerate(range(4, 18)) if day in {4, 6, 7, 11, 14}}
    table = ParsedTable(
        (
            _row(header, 18),
            _row(
                {
                    0: "A19.23.002.014 Индивидуальное занятие ЛФК",
                    1: "11:00",
                    2: "КАБИНЕТ_А",
                    **daily_marks,
                    17: "30 мин",
                },
                18,
            ),
            _row(
                {0: "SiS терапия", 1: "15:50", 2: "КАБИНЕТ_Б", **sis_marks, 17: "30мин"},
                18,
            ),
            _row(
                {
                    0: "A13.23.011. Нейропсихологическая коррекция",
                    **irregular_marks,
                    17: "30 мин",
                },
                18,
            ),
        )
    )

    procedures = extract_procedures(
        _document(tables=[table]),
        reference_date=date(2026, 8, 1),
    )

    assert [item.name for item in procedures] == [
        "Индивидуальное занятие ЛФК",
        "SiS терапия",
        "Нейропсихологическая коррекция",
    ]
    assert [item.actual_count for item in procedures] == [10, 6, 5]
    assert [item.frequency for item in procedures] == [
        "ежедневно",
        "ежедневно",
        "периодически",
    ]
    assert procedures[1].code == ""
    assert procedures[1].specialist == "Медицинская сестра по физиотерапии"
    assert procedures[1].performed_dates[0] == date(2026, 8, 7)


def test_scale_short_date_headers_use_document_year_and_role() -> None:
    table = ParsedTable(
        (
            _row({0: "", 1: "05.06", 2: "19.06"}, 3),
            _row({0: "Шкала баланса Берга", 1: "44", 2: "48"}, 3),
        )
    )
    document = _document(tables=[table], path="fixtures/ft.docx")

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


def test_neuropsych_characteristic_table_without_date_becomes_scale_rows() -> None:
    table = ParsedTable(
        (
            _row({0: "Характеристика", 1: "Балл"}, 2),
            _row({0: "Критичность", 1: "2"}, 2),
            _row({0: "Понимание смысла рассказов", 1: "3"}, 2),
        )
    )

    values = extract_scale_measurements(
        _document(tables=[table]),
        SpecialistRole.NEUROPSYCHOLOGIST,
        datetime(2026, 8, 3, 16),
    )

    assert [(item.name, item.value) for item in values] == [
        ("Критичность", "2"),
        ("Понимание смысла рассказов", "3"),
    ]


def test_scheduled_mdrk_rows_prefer_explicit_execution_time() -> None:
    table = ParsedTable(
        (
            _row({0: "Медицинское вмешательство", 1: "Дата назначения", 2: "Исполнение"}, 3),
            _row({0: "Консилиум МДРК", 1: "19.06.2026", 2: "19.06.2026 15:30"}, 3),
        )
    )

    values = extract_mdrk_meeting_datetimes(_document(tables=[table]))

    assert values[-1] == datetime(2026, 6, 19, 15, 30)


def test_mdrk_datetime_accepts_dash_separated_time_only_after_full_date() -> None:
    document = _document(
        "Консилиум мультидисциплинарной реабилитационной команды\n"
        "14.05.2026 08-46"
    )

    assert extract_mdrk_document_datetime(document) == datetime(2026, 5, 14, 8, 46)


def test_mdrk_datetime_and_scale_roles_are_read_from_local_headings() -> None:
    physician = ParsedTable(
        (
            _row(
                {
                    0: "Дата и время расчета шкалы",
                    1: "Шкала/опросник",
                    2: "Результат расчета",
                },
                3,
            ),
            _row({0: "08.06.2026 07:30", 1: "Шкала Бартел", 2: "75"}, 3),
        )
    )
    physical = ParsedTable(
        (
            _row({0: "Шкала/опросник", 1: "Исходно 08.06.2026 07:45"}, 2),
            _row({0: "Шкала баланса Берга", 1: "32"}, 2),
        )
    )
    paragraphs = [
        "Консилиум мультидисциплинарной реабилитационной команды",
        '"08" июня 2026 г. время: 08 час. 00 мин.',
        "Результат осмотра врача физической и реабилитационной медицины:",
        "Результат осмотра специалиста по физической реабилитации:",
    ]
    document = ParsedDocument(
        source_path=Path("fixtures/mdrk.docx"),
        normalized_path=Path("fixtures/mdrk.docx"),
        paragraphs=paragraphs,
        tables=[physician, physical],
        body_items=[
            BodyItem("paragraph", 0),
            BodyItem("paragraph", 1),
            BodyItem("paragraph", 2),
            BodyItem("table", 0),
            BodyItem("paragraph", 3),
            BodyItem("table", 1),
        ],
    )

    meeting_at = extract_mdrk_document_datetime(document)
    scales = extract_mdrk_scale_measurements(document, meeting_at)

    assert meeting_at == datetime(2026, 6, 8, 8)
    assert [(item.specialist, item.name, item.value) for item in scales] == [
        (SpecialistRole.FRM, "Шкала Бартел", "75"),
        (SpecialistRole.PHYSICAL_THERAPIST, "Шкала баланса Берга", "32"),
    ]


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
                "СКФ: 63,73",
                "Шкала Бартел: 75",
                "Дополнительные сведения: ШРМ 4",
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
        ("СКФ", "63,73"),
        ("Шкала Бартел", "75"),
        ("Шкала реабилитационной маршрутизации (ШРМ)", "4"),
    ]


def test_conclusion_stops_before_specialist_signature_line() -> None:
    document = _document(
        "Заключение: ЗАКЛЮЧЕНИЕ_ФТ.\n"
        "АЛЬФА А.А., специалист по физической реабилитации /_______________/"
    )

    value = extract_conclusion(document, SpecialistRole.PHYSICAL_THERAPIST)

    assert value == "ЗАКЛЮЧЕНИЕ_ФТ."
