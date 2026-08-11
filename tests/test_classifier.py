from pathlib import Path

from mdrk_builder.domain import SpecialistRole
from mdrk_builder.infrastructure.classifier import classify_document
from mdrk_builder.infrastructure.ooxml_reader import (
    BodyItem,
    ParsedCell,
    ParsedDocument,
    ParsedRow,
    ParsedTable,
)


def _document(
    path: str,
    text: str,
    tables: list[ParsedTable] | None = None,
) -> ParsedDocument:
    parsed_tables = tables or []
    return ParsedDocument(
        source_path=Path(path),
        normalized_path=Path(path),
        paragraphs=[text],
        tables=parsed_tables,
        body_items=[
            BodyItem("paragraph", 0),
            *(BodyItem("table", index) for index in range(len(parsed_tables))),
        ],
    )


def _row(values: dict[int, str], logical_cols: int = 15) -> ParsedRow:
    return ParsedRow(
        tuple(ParsedCell(column, 1, value) for column, value in sorted(values.items())),
        logical_cols,
    )


def test_administrative_reverse_sheet_does_not_become_neuropsychology() -> None:
    classification = classify_document(
        _document(
            "/patient/оборотная сторона раздела.docx",
            "Консультация медицинского психолога (нейропсихолога)",
        )
    )

    assert classification.role is SpecialistRole.OTHER
    assert classification.document_type == "administrative"


def test_gastrostomy_consilium_is_excluded_from_physician_sources() -> None:
    classification = classify_document(
        _document(
            "/patient/невролог/консилиум гастростома.docx",
            "Консилиум по вопросу установки гастростомы (ПЭГ)",
        )
    )

    assert classification.role is SpecialistRole.OTHER
    assert classification.document_type == "other_consilium"
    assert not classification.is_mdrk


def test_mdrk_is_recognized_before_specialist_mentions() -> None:
    classification = classify_document(
        _document(
            "/patient/Консилиум 2.docx",
            "Консилиум мультидисциплинарной реабилитационной команды: невролог, логопед, ФТ",
        )
    )

    assert classification.is_mdrk
    assert classification.document_type == "mdrk"
    assert classification.mdrk_kind == ""


def test_mdrk_kind_uses_repeat_table_state_not_filename() -> None:
    title = "Консилиум мультидисциплинарной реабилитационной команды"
    initial_table = ParsedTable(
        (
            _row({0: "МКФ категориальный профиль"}),
            _row({0: "b730", 1: "Сила", 11: "2"}),
        )
    )
    final_table = ParsedTable(
        (
            _row({0: "МКФ категориальный профиль"}),
            _row({0: "b730", 1: "Сила", 11: "2", 12: "1"}),
        )
    )

    initial = classify_document(_document("/patient/broken-2.docx", title, [initial_table]))
    final = classify_document(_document("/patient/broken-1.docx", title, [final_table]))

    assert initial.mdrk_kind == "initial"
    assert final.mdrk_kind == "final"


def test_final_outcome_text_prevents_empty_repeat_mdrk2_from_becoming_baseline() -> None:
    classification = classify_document(
        _document(
            "/patient/ambiguous.docx",
            "Консилиум мультидисциплинарной реабилитационной команды\n"
            "Достигнута в полном объёме",
            [
                ParsedTable(
                    (
                        _row({0: "МКФ категориальный профиль"}),
                        _row({0: "b730", 1: "Сила", 11: "2"}),
                    )
                )
            ],
        )
    )

    assert classification.mdrk_kind == "final"


def test_explicit_frm_job_title_overrides_neurology_folder_hint() -> None:
    classification = classify_document(
        _document(
            "/patient/невролог/первичный осмотр.docx",
            "Первичный осмотр. Лечащий врач, врач физической и реабилитационной медицины.",
        )
    )

    assert classification.role is SpecialistRole.FRM


def test_incidental_frm_mention_does_not_override_profile_specialist() -> None:
    classification = classify_document(
        _document(
            "/patient/лого/первичная консультация логопеда.docx",
            "Первичная консультация логопеда. Согласовано: врач ФРМ.",
        )
    )

    assert classification.role is SpecialistRole.LOGOPEDIST


def test_treating_neurologist_and_primary_heading_beat_incidental_plan_mentions() -> None:
    classification = classify_document(
        _document(
            "/patient/corrupted-name.docx",
            "\n".join(
                (
                    "ПЕРВИЧНЫЙ ОСМОТР",
                    "(лечащим врачом совместно с заведующим отделением)",
                    "План: консультация нейропсихолога и логопеда.",
                    "Повторная консультация хирурга через 14 дней.",
                    "Тестова Т.Т., лечащий врач, врач-невролог /___/",
                    "Примеров П.П., заведующий, врач ФРМ /___/",
                )
            ),
        )
    )

    assert classification.role is SpecialistRole.NEUROLOGIST
    assert classification.document_type == "initial"


def test_treating_neurologist_beats_standalone_recommended_specialist_line() -> None:
    classification = classify_document(
        _document(
            "/patient/corrupted-name.docx",
            "\n".join(
                (
                    "ПЕРВИЧНЫЙ ОСМОТР",
                    "(лечащим врачом совместно с заведующим отделением)",
                    "Показана консультация нейропсихолога.",
                    "Тестова Т.Т., лечащий врач, врач-невролог /___/",
                )
            ),
        )
    )

    assert classification.role is SpecialistRole.NEUROLOGIST
    assert classification.document_type == "initial"


def test_primary_neuropsychology_heading_beats_incidental_dynamics_word() -> None:
    classification = classify_document(
        _document(
            "/patient/neuropsychology.docx",
            "Первичное обследование медицинского психолога (нейропсихолога). "
            "В пробе отсутствует динамика запоминания.",
        )
    )

    assert classification.role is SpecialistRole.NEUROPSYCHOLOGIST
    assert classification.document_type == "initial"


def test_profile_specialist_is_not_overridden_by_copied_treating_doctor_signature() -> None:
    classification = classify_document(
        _document(
            "/patient/лого/первичная консультация.docx",
            "Первичная консультация медицинского логопеда. "
            "Из выписки: Тестова Т.Т., лечащий врач, врач-невролог.",
        )
    )

    assert classification.role is SpecialistRole.LOGOPEDIST


def test_discharge_heading_precedes_historical_primary_exam() -> None:
    classification = classify_document(
        _document(
            "/patient/discharge.docx",
            "Выписной эпикриз. В истории: Первичный осмотр выполнен 01.01.2026.",
        )
    )

    assert classification.document_type == "final"


def test_primary_heading_in_fifth_paragraph_is_still_a_heading() -> None:
    paragraphs = [
        "ГБУЗ Тестовая больница",
        "Отделение медицинской реабилитации",
        "Пациент: Тестов Тест Тестович",
        "История болезни: 123/26",
        "Первичный осмотр невролога",
        "Повторная консультация хирурга через 14 дней.",
    ]
    document = ParsedDocument(
        source_path=Path("/patient/neutral.docx"),
        normalized_path=Path("/patient/neutral.docx"),
        paragraphs=paragraphs,
        body_items=[BodyItem("paragraph", index) for index in range(len(paragraphs))],
    )

    classification = classify_document(document)

    assert classification.role is SpecialistRole.NEUROLOGIST
    assert classification.document_type == "initial"
