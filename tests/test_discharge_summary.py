from datetime import datetime
from pathlib import Path

import pytest
from docx import Document

from mdrk_builder.application.discharge_defaults import RECOMMENDATIONS_TEMPLATE
from mdrk_builder.application.discharge_source_selection import (
    source_scan_for_episode,
)
from mdrk_builder.application.discharge_summary import scan_discharge_summary
from mdrk_builder.application.episode_identity import DischargeEpisodeKey
from mdrk_builder.application.source_scan import ScannedDocument, SourceScanResult
from mdrk_builder.domain import SpecialistRole
from mdrk_builder.infrastructure.classifier import DocumentClassification
from mdrk_builder.infrastructure.ooxml_reader import (
    BodyItem,
    ParsedCell,
    ParsedDocument,
    ParsedRow,
    ParsedTable,
)
from mdrk_builder.infrastructure.discharge_summary_writer import (
    write_discharge_summary_docx,
)


EPISODE_ROOT = Path("/episode")


def _write_document(path, lines: tuple[str, ...]) -> None:
    document = Document()
    for line in lines:
        document.add_paragraph(line)
    document.save(path)


def _scanned_source(
    name: str,
    *lines: str,
    document_type: str = "follow_up",
    tables: tuple[ParsedTable, ...] = (),
) -> ScannedDocument:
    path = EPISODE_ROOT / name
    document = ParsedDocument(
        source_path=path,
        normalized_path=path,
        paragraphs=list(lines),
        tables=list(tables),
        body_items=[
            *(BodyItem("paragraph", index) for index in range(len(lines))),
            *(BodyItem("table", index) for index in range(len(tables))),
        ],
    )
    return ScannedDocument(
        document=document,
        classification=DocumentClassification(
            SpecialistRole.NEUROPSYCHOLOGIST,
            document_type,
        ),
    )


def _row(values: dict[int, str], logical_cols: int) -> ParsedRow:
    return ParsedRow(
        tuple(
            ParsedCell(column, 1, value)
            for column, value in sorted(values.items())
        ),
        logical_cols,
    )


def _episode_source_scan(*documents: ScannedDocument) -> SourceScanResult:
    return SourceScanResult(
        source_files=tuple(item.document.source_path for item in documents),
        documents=documents,
        failures=(),
        root=EPISODE_ROOT,
    )


def _episode_key() -> DischargeEpisodeKey:
    return DischargeEpisodeKey(
        normalized_full_name="альфа бета гамма",
        medical_record_number="5906/26",
        admission_at=datetime(2026, 8, 10, 10),
        discharge_at=datetime(2026, 8, 17, 12),
        episode_root=EPISODE_ROOT,
    )


def _primary_lines(
    record: str = "СКП5906/26",
    *,
    full_name: str = "Пациент Тестовый Пример",
    admission: str = "10.08.2026 10:00",
) -> tuple[str, ...]:
    return (
        "ПЕРВИЧНЫЙ ОСМОТР НЕВРОЛОГА",
        f"ФИО пациента: {full_name}",
        f"Номер ИБ: {record}",
        f"Дата и время поступления: {admission}",
        "Жалобы: PRIMARY COMPLAINTS",
        "Анамнез заболевания: PRIMARY DISEASE HISTORY",
        "Анамнез жизни: PRIMARY LIFE HISTORY",
        "Пациентом представлены необходимые для госпитализации документы: PRIMARY DOCS",
        "Физикальное обследование: PRIMARY PHYSICAL",
        "Неврологический статус: PRIMARY NEURO",
        "Локальный статус: PRIMARY LOCAL",
        "Шкалы при поступлении:",
        "Заключительный клинический диагноз: PRIMARY DIAGNOSIS",
        "Факторы риска проведения реабилитационных мероприятий: PRIMARY RISKS",
        "Факторы, ограничивающие проведение реабилитационных мероприятий: PRIMARY LIMITS",
        "Двигательный режим: палатный",
        "Диета: стол № 9",
        "Лечащий врач, врач-невролог",
    )


def _discharge_lines(
    record: str = "СКП5906/26",
    radiation: str | None = "4,2 мЗв",
    *,
    full_name: str = "Пациент Тестовый Пример",
    admission: str = "10.08.2026 10:00",
) -> tuple[str, ...]:
    values = [
        "Выписной эпикриз",
        "Отделение медицинской реабилитации",
        "Сведения о пациенте",
        f"ФИО пациента: {full_name}",
        f"Номер медицинской карты пациента №{record}",
        f"Дата и время поступления: {admission}",
        "Дата и время выписки: 17.08.2026 12:00",
        "Заключительный клинический диагноз: DISCHARGE DIAGNOSIS",
        "Лабораторные исследования: CURRENT LAB",
        "Инструментальные исследования: CURRENT INSTRUMENTAL",
        "Консультация оториноларинголога 13.08.2026 09:00",
        "CURRENT ENT CONCLUSION",
        "Консультация хирурга 29.07.2028 09:00",
        "IMPOSSIBLE FUTURE CONSULTATION",
    ]
    if radiation is not None:
        values.append(f"Лучевая нагрузка - {radiation}")
    values.append("Лечащий врач")
    return tuple(values)


def test_discharge_scan_applies_explicit_field_authority_and_chronology(tmp_path) -> None:
    current = tmp_path / "невролог"
    current.mkdir()
    primary_path = current / "первичный осмотр невролога.docx"
    discharge_path = current / "выписной эпикриз.docx"
    old_discharge_path = tmp_path / "старый выписной эпикриз.docx"
    _write_document(primary_path, _primary_lines())
    _write_document(discharge_path, _discharge_lines())
    _write_document(
        old_discharge_path,
        _discharge_lines("СКП5799/26", "12,7 мЗв"),
    )

    draft = scan_discharge_summary(tmp_path)

    assert draft.discharge_source == discharge_path
    assert draft.primary_neurologist_source == primary_path
    assert draft.identity.medical_record_number == "СКП5906/26"
    assert draft.admission_datetime == datetime(2026, 8, 10, 10, 0)
    assert draft.discharge_datetime == datetime(2026, 8, 17, 12, 0)
    assert not hasattr(draft, "episode")
    assert set(draft.source_paths) == {
        primary_path.resolve(),
        discharge_path.resolve(),
        old_discharge_path.resolve(),
    }
    assert old_discharge_path.resolve() in draft.immutable_sources()
    assert draft.clinical_diagnosis.startswith("Основное заболевание:\nPRIMARY DIAGNOSIS")
    assert draft.complaints == "PRIMARY COMPLAINTS"
    assert draft.disease_history == "PRIMARY DISEASE HISTORY"
    assert draft.laboratory_results == "CURRENT LAB"
    assert draft.instrumental_results == "CURRENT INSTRUMENTAL"
    assert "CURRENT ENT CONCLUSION" in draft.other_consultations
    assert "IMPOSSIBLE FUTURE CONSULTATION" not in draft.other_consultations
    assert draft.radiation_exposure == "4,2 мЗв"
    assert draft.medications == ""
    assert draft.transfusions == ""
    assert draft.final_mdrk_source is None
    assert draft.rehabilitation_potential == "средний"
    assert draft.goal_result == "достигнут в полном объёме"
    assert draft.recommendations == ""
    assert draft.field_sources["clinical_diagnosis"] == primary_path
    assert draft.field_sources["radiation_exposure"] == discharge_path
    assert any(issue.code == "consultation_outside_episode" for issue in draft.issues)
    assert not any(
        issue.code == "identity_conflict_medical_record_number"
        for issue in draft.issues
    )


def test_discharge_scan_writes_reopens_and_ignores_its_output(tmp_path) -> None:
    primary_path = tmp_path / "первичный осмотр невролога.docx"
    discharge_path = tmp_path / "выписной эпикриз.docx"
    output_path = tmp_path / "готовый выписной эпикриз.docx"
    _write_document(primary_path, _primary_lines())
    _write_document(discharge_path, _discharge_lines())

    draft = scan_discharge_summary(tmp_path)
    created = write_discharge_summary_docx(draft, output_path)

    reopened = Document(created)
    assert "ВЫПИСНОЙ ЭПИКРИЗ" in "\n".join(
        paragraph.text for paragraph in reopened.paragraphs
    )
    rescanned = scan_discharge_summary(tmp_path)
    assert rescanned.discharge_source == discharge_path
    assert rescanned.primary_neurologist_source == primary_path
    assert rescanned.clinical_diagnosis == draft.clinical_diagnosis
    assert output_path.resolve() not in rescanned.immutable_sources()
    rewritten = write_discharge_summary_docx(rescanned, output_path)
    assert rewritten == output_path.resolve()
    assert Document(rewritten).paragraphs


def test_discharge_scan_uses_profile_primary_not_admission_department(
    tmp_path,
) -> None:
    primary_path = tmp_path / "первичный осмотр невролога.docx"
    admission_path = tmp_path / "первичный осмотр в приёмном.docx"
    _write_document(primary_path, _primary_lines())
    _write_document(
        admission_path,
        (
            "ПЕРВИЧНЫЙ ОСМОТР НЕВРОЛОГА ПРИЁМНОГО ОТДЕЛЕНИЯ",
            *_primary_lines()[1:],
        ),
    )
    _write_document(tmp_path / "выписной эпикриз.docx", _discharge_lines())

    draft = scan_discharge_summary(tmp_path)

    assert draft.primary_neurologist_source == primary_path
    assert draft.clinical_diagnosis.startswith("Основное заболевание:\nPRIMARY DIAGNOSIS")
    assert not any(
        issue.code == "episode_source_selection_ambiguous"
        for issue in draft.blocking_issues()
    )


def test_profile_primary_can_mention_admission_department_in_history(
    tmp_path,
) -> None:
    primary_lines = tuple(
        (
            "Анамнез заболевания: Пациент переведён из приёмного "
            "отделения в профильное."
            if line.startswith("Анамнез заболевания")
            else line
        )
        for line in _primary_lines()
    )
    primary_path = tmp_path / "первичный осмотр невролога.docx"
    _write_document(primary_path, primary_lines)
    _write_document(tmp_path / "выписной эпикриз.docx", _discharge_lines())

    draft = scan_discharge_summary(tmp_path)

    assert draft.primary_neurologist_source == primary_path
    assert not any(
        issue.code == "primary_neurologist_source_missing"
        for issue in draft.blocking_issues()
    )


def test_discharge_scan_defaults_radiation_to_zero(tmp_path) -> None:
    _write_document(tmp_path / "первичный осмотр невролога.docx", _primary_lines())
    _write_document(
        tmp_path / "выписной эпикриз.docx",
        _discharge_lines(radiation=None),
    )

    draft = scan_discharge_summary(tmp_path)

    assert draft.radiation_exposure == "0 мЗв"
    assert "radiation_exposure" not in draft.field_sources


def test_discharge_scan_builds_project_without_current_discharge_source(tmp_path) -> None:
    _write_document(tmp_path / "первичный осмотр невролога.docx", _primary_lines())

    draft = scan_discharge_summary(tmp_path)

    assert any(
        issue.code == "discharge_summary_source_missing"
        for issue in draft.issues
    )


def test_discharge_scan_fails_closed_when_header_boundary_is_missing(tmp_path) -> None:
    _write_document(tmp_path / "первичный осмотр невролога.docx", _primary_lines())
    without_diagnosis = tuple(
        line
        for line in _discharge_lines()
        if not line.startswith("Заключительный клинический диагноз")
    )
    _write_document(tmp_path / "выписной эпикриз.docx", without_diagnosis)

    draft = scan_discharge_summary(tmp_path)

    assert draft.header_text == ""
    assert "header_text" not in draft.field_sources
    assert any(
        issue.code == "discharge_header_missing"
        for issue in draft.blocking_issues()
    )


def test_discharge_scan_blocks_without_discharge_datetime(tmp_path) -> None:
    _write_document(tmp_path / "первичный осмотр невролога.docx", _primary_lines())
    without_discharge_datetime = tuple(
        line
        for line in _discharge_lines()
        if not line.startswith("Дата и время выписки")
    )
    _write_document(
        tmp_path / "выписной эпикриз.docx",
        without_discharge_datetime,
    )

    draft = scan_discharge_summary(tmp_path)

    assert any(
        issue.code == "discharge_datetime_missing"
        for issue in draft.blocking_issues()
    )


def test_discharge_scan_never_pairs_conflicting_patients(tmp_path) -> None:
    _write_document(
        tmp_path / "первичный осмотр невролога.docx",
        _primary_lines(
            "СКП7777/26",
            full_name="Другой Пациент Тестовый",
            admission="11.08.2026 10:00",
        ),
    )
    discharge_path = tmp_path / "выписной эпикриз.docx"
    _write_document(discharge_path, _discharge_lines())

    draft = scan_discharge_summary(tmp_path)

    assert draft.discharge_source is None
    assert draft.primary_neurologist_source is not None
    assert "PRIMARY DIAGNOSIS" in draft.clinical_diagnosis
    assert discharge_path not in draft.field_sources.values()
    assert draft.identity.medical_record_number == "СКП7777/26"
    assert draft.discharge_datetime is None


def test_discharge_scan_does_not_pair_sources_without_patient_identity(tmp_path) -> None:
    _write_document(
        tmp_path / "первичный осмотр невролога.docx",
        _primary_lines("", full_name="нет данных"),
    )
    _write_document(
        tmp_path / "выписной эпикриз.docx",
        _discharge_lines("", full_name="нет данных"),
    )

    draft = scan_discharge_summary(tmp_path)

    assert draft.discharge_source is None
    blocking_codes = {issue.code for issue in draft.blocking_issues()}
    assert "episode_source_identity_insufficient" in blocking_codes
    assert "required_identity_full_name" in blocking_codes


def test_discharge_scan_does_not_pair_same_name_without_episode_anchor(
    tmp_path,
) -> None:
    _write_document(
        tmp_path / "первичный осмотр невролога.docx",
        _primary_lines("", admission=""),
    )
    _write_document(
        tmp_path / "выписной эпикриз.docx",
        _discharge_lines("", admission=""),
    )

    draft = scan_discharge_summary(tmp_path)

    assert draft.discharge_source is None
    blocking_codes = {issue.code for issue in draft.blocking_issues()}
    assert "episode_source_identity_insufficient" in blocking_codes
    assert "required_identity_medical_record_number" in blocking_codes


def test_episode_source_projection_excludes_pre_admission_document() -> None:
    old_source = _scanned_source(
        "old-consultation.docx",
        "Осмотр нейропсихолога 09.08.2026 09:00",
        "ФИО пациента: АЛЬФА БЕТА ГАММА",
        "Номер ИБ: СКП5906/26",
    )
    current_source = _scanned_source(
        "current-consultation.docx",
        "Осмотр нейропсихолога 11.08.2026 09:00",
        "ФИО пациента: АЛЬФА БЕТА ГАММА",
        "Номер ИБ: СКП5906/26",
    )
    issues = []

    projected = source_scan_for_episode(
        _episode_source_scan(old_source, current_source),
        _episode_key(),
        issues=issues,
    )

    assert projected.documents == (current_source,)
    assert [issue.code for issue in issues] == [
        "episode_source_before_admission_excluded"
    ]
    assert issues[0].source == old_source.document.source_path


def test_episode_source_projection_rejects_undated_unidentified_root_match() -> None:
    unsupported_source = _scanned_source(
        "unsupported.docx",
        "Заключение специалиста без идентификаторов и даты",
    )
    dated_source = _scanned_source(
        "dated.docx",
        "Осмотр нейропсихолога 11.08.2026 09:00",
    )
    issues = []

    projected = source_scan_for_episode(
        _episode_source_scan(unsupported_source, dated_source),
        _episode_key(),
        issues=issues,
    )

    assert projected.documents == (dated_source,)
    assert [issue.code for issue in issues] == [
        "episode_source_identity_and_date_missing"
    ]
    assert issues[0].source == unsupported_source.document.source_path


def test_episode_source_projection_rejects_same_name_without_dated_anchor() -> None:
    same_name_only = _scanned_source(
        "same-name-only.docx",
        "Заключение специалиста без даты",
        "ФИО пациента: АЛЬФА БЕТА ГАММА",
    )
    issues = []

    projected = source_scan_for_episode(
        _episode_source_scan(same_name_only),
        _episode_key(),
        issues=issues,
    )

    assert projected.documents == ()
    assert [issue.code for issue in issues] == [
        "episode_source_identity_and_date_missing"
    ]
    assert issues[0].source == same_name_only.document.source_path


def test_episode_source_projection_excludes_post_discharge_document() -> None:
    future_source = _scanned_source(
        "future-consultation.docx",
        "Осмотр нейропсихолога 18.08.2026 09:00",
        "ФИО пациента: АЛЬФА БЕТА ГАММА",
        "Номер ИБ: СКП5906/26",
    )
    current_source = _scanned_source(
        "current-consultation.docx",
        "Осмотр нейропсихолога 17.08.2026 09:00",
        "ФИО пациента: АЛЬФА БЕТА ГАММА",
        "Номер ИБ: СКП5906/26",
    )
    issues = []

    projected = source_scan_for_episode(
        _episode_source_scan(future_source, current_source),
        _episode_key(),
        issues=issues,
    )

    assert projected.documents == (current_source,)
    assert [issue.code for issue in issues] == [
        "episode_source_after_discharge_excluded"
    ]
    assert issues[0].source == future_source.document.source_path


def test_episode_source_projection_retains_identified_undated_assignment_sheet() -> None:
    identity_lines = (
        "ФИО пациента: АЛЬФА БЕТА ГАММА",
        "Номер ИБ: СКП5906/26",
        "Дата и время поступления: 10.08.2026 10:00",
    )
    undated_table = ParsedTable(
        (
            _row({0: "Назначения", 1: "время", 2: "кабинет", 3: "выполнено"}, 4),
            _row({0: "A13.23.011 Нейропсихологическая коррекция", 3: "+"}, 4),
        )
    )
    dated_table = ParsedTable(
        (
            _row({0: "Назначения", 1: "время", 2: "кабинет", 3: "11"}, 4),
            _row({0: "A13.23.011 Нейропсихологическая коррекция", 3: "+"}, 4),
        )
    )
    undated_source = _scanned_source(
        "undated-assignment.docx",
        *identity_lines,
        document_type="assignment_sheet",
        tables=(undated_table,),
    )
    dated_source = _scanned_source(
        "dated-assignment.docx",
        *identity_lines,
        document_type="assignment_sheet",
        tables=(dated_table,),
    )
    issues = []

    projected = source_scan_for_episode(
        _episode_source_scan(undated_source, dated_source),
        _episode_key(),
        issues=issues,
    )

    assert projected.documents == (undated_source, dated_source)
    assert [issue.code for issue in issues] == [
        "episode_assignment_sheet_date_missing"
    ]
    assert issues[0].source == undated_source.document.source_path


@pytest.mark.parametrize("heading", ["Структура", "Функции", "МКФ категориальный профиль", ""])
def test_discharge_narrative_stops_before_unlabelled_icf_table(tmp_path, heading) -> None:
    primary_path = tmp_path / "первичный осмотр невролога.docx"
    document = Document()
    for line in _primary_lines():
        document.add_paragraph(line)
        if line == "Локальный статус: PRIMARY LOCAL":
            table = document.add_table(rows=2, cols=15)
            table.cell(0, 0).text = heading
            if not heading:
                table.cell(0, 1).text = "Структура"
            for column, value in enumerate(range(5), start=6):
                table.cell(0, column).text = str(value)
            table.cell(1, 0).text = "s110"
            table.cell(1, 1).text = "Структура головного мозга"
            table.cell(1, 11).text = "2"
    document.save(primary_path)
    _write_document(tmp_path / "выписной эпикриз.docx", _discharge_lines())

    draft = scan_discharge_summary(tmp_path)

    assert draft.local_status == "PRIMARY LOCAL"
    output = write_discharge_summary_docx(draft, tmp_path / "result.docx")
    assert not any("|" in p.text for p in Document(output).paragraphs)


@pytest.mark.parametrize('reason_heading', [
    'Обоснование:', 'Обоснование диагноза:',
    'Обоснование необходимости госпитализации:',
    '12. Обоснование выбора методов реабилитации:',
])
def test_discharge_excludes_source_planning_and_preserves_template_fields(tmp_path, reason_heading):
    lines = []
    for line in _primary_lines():
        lines.append(line)
        if line == 'Локальный статус: PRIMARY LOCAL':
            lines.extend((
                f'{reason_heading} REASON_NOT_FOR_DISCHARGE',
                'REASON_CONTINUATION_NOT_FOR_DISCHARGE',
                'План обследования: PLAN_NOT_FOR_DISCHARGE',
                'План лечения: TREATMENT_PLAN_NOT_FOR_DISCHARGE',
            ))
        if line == 'Заключительный клинический диагноз: PRIMARY DIAGNOSIS':
            lines.extend(('Основное заболевание: PRIMARY MAIN DIAGNOSIS',
                          'Сопутствующие заболевания: PRIMARY COMORBIDITY'))
    _write_document(tmp_path / 'первичный осмотр невролога.docx', tuple(lines))
    _write_document(tmp_path / 'выписной эпикриз.docx', _discharge_lines())
    draft = scan_discharge_summary(tmp_path)
    assert draft.life_history == 'PRIMARY LIFE HISTORY'
    assert draft.provided_documents == 'PRIMARY DOCS'
    assert draft.physical_exam == 'PRIMARY PHYSICAL'
    assert draft.local_status == 'PRIMARY LOCAL'
    assert draft.diet == 'стол № 9'
    assert 'PRIMARY MAIN DIAGNOSIS' in draft.clinical_diagnosis
    assert 'PRIMARY COMORBIDITY' in draft.clinical_diagnosis
    output = write_discharge_summary_docx(draft, tmp_path / 'result.docx')
    text = '\n'.join(p.text for p in Document(output).paragraphs)
    assert 'NOT_FOR_DISCHARGE' not in text
    assert 'обоснование' not in text.casefold()
    assert 'CURRENT LAB' in text and 'CURRENT ENT CONCLUSION' in text
    assert text.index('Медицинские вмешательства') < text.index('Двигательный режим')
    assert text.index('Результаты медицинского обследования') < text.index('CURRENT ENT CONCLUSION')


@pytest.mark.parametrize('date_headers', [('11', '12'), ('11.08.2026', '12.08.2026'), ('выполнено', '')])
def test_discharge_uses_final_snapshot_without_existing_mdrk2(tmp_path, date_headers):
    from mdrk_builder.application.scanner import scan_patient_folder
    from mdrk_builder.application.snapshot import build_snapshot
    from mdrk_builder.domain import MdrkKind

    primary_path = tmp_path / 'первичный осмотр невролога.docx'
    document = Document()
    for line in _primary_lines():
        document.add_paragraph(line)
    document.add_paragraph('Дата осмотра: 10.08.2026 11:00')
    table = document.add_table(rows=2, cols=15)
    table.cell(0, 0).text = 'МКФ'
    table.cell(0, 13).text = 'Ответственный специалист'
    table.cell(1, 0).text = 's110'
    table.cell(1, 1).text = 'Структура головного мозга'
    table.cell(1, 11).text = '3'
    table.cell(1, 12).text = '2'
    document.add_paragraph('Индекс мобильности Ривермид: 4')
    document.save(primary_path)
    follow_up = tmp_path / 'повторный осмотр невролога.docx'
    _write_document(follow_up, (
        'Повторный осмотр невролога 16.08.2026 10:00',
        'ФИО пациента: ПАЦИЕНТ ТЕСТОВЫЙ ПРИМЕР',
        'Номер ИБ: СКП5906/26',
        'Индекс мобильности Ривермид: 9',
    ))
    _write_document(tmp_path / 'выписной эпикриз.docx', _discharge_lines())
    assignment_path = tmp_path / 'лист назначений.docx'
    document = Document()
    document.add_paragraph('Лист назначений')
    document.add_paragraph('ФИО пациента: ПАЦИЕНТ ТЕСТОВЫЙ ПРИМЕР')
    document.add_paragraph('Номер ИБ: СКП5906/26')
    table = document.add_table(rows=2, cols=5)
    for cell, value in zip(table.rows[0].cells, ('Назначения', 'время', 'кабинет', *date_headers)):
        cell.text = value
    table.cell(1, 0).text = 'A19.23.002.014 Индивидуальное занятие ЛФК'
    table.cell(1, 3).text = '+'
    table.cell(1, 4).text = '+'
    document.save(assignment_path)

    boundary = datetime(2026, 8, 17, 23, 59, 59)
    episode = scan_patient_folder(tmp_path, final_meeting_at=boundary)
    snapshot = build_snapshot(episode, MdrkKind.FINAL)
    draft = scan_discharge_summary(tmp_path)
    assert draft.final_mdrk_source is None
    assert draft.icf_domains and draft.icf_domains == snapshot.icf_domains
    assert draft.completed_procedures == tuple(episode.procedures)
    assert draft.completed_procedures[0].actual_count == 2
    assert draft.completed_procedures[0].specialist == SpecialistRole.PHYSICAL_THERAPIST.display_name
    assert draft.completed_procedures[0].source == assignment_path
    assert [(row.name, row.value, row.source) for row in draft.admission_scale_rows] == [
        (row.name, row.initial.value, row.initial.source) for row in snapshot.scale_rows
    ]
    assert [(row.name, row.value, row.source) for row in draft.discharge_scale_rows] == [
        (row.name, row.current.value, row.current.source) for row in snapshot.scale_rows
    ]
    assert draft.discharge_scale_rows[0].value == '9'
    output = write_discharge_summary_docx(draft, tmp_path / 'result.docx')
    tables = '\n'.join(cell.text for table in Document(output).tables for row in table.rows for cell in row.cells)
    assert 's110' in tables
    assert 'Индивидуальное занятие ЛФК' in tables
    assert 'Ривермид' in tables and '9' in tables


def test_discharge_requested_header_specialists_icf_and_primary_format(tmp_path):
    from docx.oxml.ns import qn
    primary = Document()
    lines = list(_primary_lines())
    lines[lines.index('Анамнез жизни: PRIMARY LIFE HISTORY')] = 'Анамнез жизни: Первая строка\nВторая строка\nТретья строка'
    lines[lines.index('Заключительный клинический диагноз: PRIMARY DIAGNOSIS')] = (
        'Заключительный клинический диагноз:\nОсновное заболевание: MAIN\n'
        'Сопутствующие заболевания: RELATED\nДополнительные сведения: DETAILS')
    for line in lines:
        primary.add_paragraph(line)
    primary.add_paragraph('Дата осмотра: 10.08.2026 11:00')
    table = primary.add_table(rows=2, cols=15)
    table.cell(0, 0).text = 'МКФ'
    for col, value in {0: 's110', 1: 'Структура головного мозга', 11: '3', 12: '1'}.items():
        table.cell(1, col).text = value
    primary.save(tmp_path / 'primary.docx')
    header_lines = [
        'Поступил: в стационар - 1, в дневной стационар - 2',
        'Период нахождения в стационаре в дневном стационаре',
        'Исход госпитализации: выписан - 1, в том числе в дневной стационар',
        'Результат госпитализации: выздоровление - 1, улучшение - 2, без перемен - 3',
        'Форма оказания медицинской помощи: плановая - 1, экстренная - 2',
    ]
    discharge_lines = list(_discharge_lines())
    idx = next(i for i, line in enumerate(discharge_lines) if line.startswith('Заключительный клинический диагноз'))
    discharge_lines[idx:idx] = header_lines
    _write_document(tmp_path / 'discharge-source.docx', tuple(discharge_lines))
    therapist = Document()
    for line in ('Первичный осмотр специалиста по физической реабилитации 10.08.2026 12:00',
                 'Номер ИБ: СКП5906/26', 'Заключение', 'THERAPY CONCLUSION',
                 'Специалист по физической реабилитации ТЕСТОВЫЙ А.А.'):
        therapist.add_paragraph(line)
    scales = therapist.add_table(rows=2, cols=3)
    for cell, text in zip(scales.rows[0].cells, ('Шкала', '10.08.2026', '16.08.2026')):
        cell.text = text
    for cell, text in zip(scales.rows[1].cells, ('Шкала Берга', '12', '24')):
        cell.text = text
    therapist.save(tmp_path / 'ft.docx')
    _write_document(tmp_path / 'logopedist.docx', (
        'Первичный осмотр логопеда 11.08.2026 10:00', 'Номер ИБ: СКП5906/26',
        'Шкала дизартрии - 3 балла',
    ))
    draft = scan_discharge_summary(tmp_path)
    assert any(item.role is SpecialistRole.LOGOPEDIST for item in draft.team_findings)
    assert draft.life_history == 'Первая строка\nВторая строка\nТретья строка'
    assert draft.clinical_diagnosis == 'Основное заболевание:\nMAIN\n\nСопутствующие заболевания:\nRELATED\n\nДополнительные сведения о заболевании:\nDETAILS'
    ft = next(item for item in draft.team_findings if item.role is SpecialistRole.PHYSICAL_THERAPIST)
    assert ft.conclusion == 'THERAPY CONCLUSION'
    assert ft.specialist_name == 'ТЕСТОВЫЙ А.А.'
    assert [(row.initial_value, row.value) for row in ft.scales] == [('12', '24')]
    output = write_discharge_summary_docx(draft, tmp_path / 'requested.docx', ignore_issues=True)
    document = Document(output)
    paragraphs = [p.text for p in document.paragraphs]
    for text in ('в стационар - 1', 'в стационаре', 'выписан - 1', 'улучшение - 2', 'плановая - 1'):
        assert any(run.text == text and run.bold for p in document.paragraphs for run in p.runs)
    assert 'Результат осмотра специалиста по физической реабилитации ТЕСТОВЫЙ А.А. (10.08.2026, 12:00)' in paragraphs
    icf = next(table for table in document.tables if 'МКФ категориальный профиль' in table.cell(0, 0).text)
    assert '10.08.' in icf.cell(1, 11).text and '17.08.' in icf.cell(1, 12).text
    row = next(row for row in icf.rows if row.cells[0].text == 's110')
    assert row.cells[8]._tc.xpath('./w:tcPr/w:shd') == []  # Severity 2 is no longer shaded (latest is 1).
    assert row.cells[7]._tc.xpath('./w:tcPr/w:shd')


def test_discharge_motor_scale_prefers_ft_over_neurologist_copy(tmp_path):
    _write_document(tmp_path / 'primary.docx', (*_primary_lines(),
        'Дата осмотра: 10.08.2026 11:00', 'Индекс мобильности Ривермид: 99'))
    _write_document(tmp_path / 'discharge.docx', _discharge_lines())
    doc = Document()
    doc.add_paragraph('Первичный осмотр специалиста по физической реабилитации 10.08.2026 12:00')
    doc.add_paragraph('Номер ИБ: СКП5906/26')
    table = doc.add_table(rows=2, cols=3)
    for cells, values in ((table.rows[0].cells, ('Шкала', '10.08.2026', '16.08.2026')),
                          (table.rows[1].cells, ('Индекс мобильности Ривермид', '4', '9'))):
        for cell, value in zip(cells, values):
            cell.text = value
    doc.save(tmp_path / 'ft.docx')
    draft = scan_discharge_summary(tmp_path)
    assert [(row.role, row.value) for row in draft.admission_scale_rows] == [(SpecialistRole.PHYSICAL_THERAPIST, '4')]
    assert [(row.role, row.value) for row in draft.discharge_scale_rows] == [(SpecialistRole.PHYSICAL_THERAPIST, '9')]
    finding = next(item for item in draft.team_findings if item.role is SpecialistRole.PHYSICAL_THERAPIST)
    assert finding.scales[0].initial_source == tmp_path / 'ft.docx'
    assert finding.scales[0].source == tmp_path / 'ft.docx'


def test_medwork_actual_final_fields_replace_clinical_templates(tmp_path):
    _write_document(tmp_path / 'primary.docx', _primary_lines())
    source = tmp_path / 'mis.docx'
    lines = list(_discharge_lines())
    lines += [
        'Проведенные обследования, лечение, медицинская реабилитация:',
        'Применение лекарственных препаратов (включая химиотерапию, вакцинацию):',
        'ЛЕКАРСТВО ИЗ МИС', 'Трансфузии (переливания) донорской крови:', 'нет.',
        'Медицинские вмешательства: согласно ИПМР', 'Шкалы при выписке:',
        'Состояние при выписке, трудоспособность, листок нетрудоспособности:',
        'Соматический статус: СОСТОЯНИЕ ИЗ МИС', 'Температура: 36.4',
        'Неврологический статус:', 'НЕВРОЛОГИЯ ИЗ МИС', '| |',
        '«17» августа 2026 г.', 'Я, ПАЦИЕНТ, получил выписной эпикриз',
    ]
    _write_document(source, tuple(lines))
    draft = scan_discharge_summary(tmp_path)
    assert draft.medications == 'ЛЕКАРСТВО ИЗ МИС'
    assert draft.transfusions == 'нет.'
    assert draft.discharge_condition == 'Соматический статус: СОСТОЯНИЕ ИЗ МИС\nТемпература: 36.4'
    assert draft.discharge_neurological_status == 'НЕВРОЛОГИЯ ИЗ МИС'
    assert draft.recommendations == ''
    assert draft.work_capacity == ''
    for name in ('medications', 'transfusions', 'discharge_condition', 'discharge_neurological_status'):
        assert draft.field_sources[name] == source


def test_primary_repeated_diagnoses_and_life_subsections_survive(tmp_path):
    _write_document(tmp_path / 'primary.docx', (
        *_primary_lines()[:5],
        'Анамнез жизни: ИСТОРИЯ', 'Оперативные вмешательства: ОПЕРАЦИЯ В АНАМНЕЗЕ',
        'Эпидемиологический анамнез', 'АНАМНЕЗ ИНФЕКЦИЙ', 'Экспертный анамнез',
        'Место работы: РАБОТА', 'Физикальное исследование, локальный статус',
        'ОСМОТР', 'Основное заболевание: MAIN', 'Сопутствующие заболевания: ONE',
        'Осложнения основного заболевания: COMPLICATION', 'Сопутствующие заболевания: TWO',
        'ЕЩЁ ЗАБОЛЕВАНИЕ', 'Дополнительные сведения о заболевании', 'ДОПОЛНЕНИЕ',
        'Обоснование: НЕ ПЕРЕНОСИТЬ',
    ))
    _write_document(tmp_path / 'mis.docx', _discharge_lines())
    draft = scan_discharge_summary(tmp_path)
    assert 'ОПЕРАЦИЯ В АНАМНЕЗЕ' in draft.life_history
    assert 'АНАМНЕЗ ИНФЕКЦИЙ' in draft.life_history and 'РАБОТА' in draft.life_history
    assert 'ОСМОТР' not in draft.life_history
    assert 'ONE' in draft.clinical_diagnosis and 'TWO' in draft.clinical_diagnosis
    assert 'ЕЩЁ ЗАБОЛЕВАНИЕ' in draft.clinical_diagnosis
    assert 'ДОПОЛНЕНИЕ' in draft.clinical_diagnosis
    assert 'НЕ ПЕРЕНОСИТЬ' not in draft.clinical_diagnosis


def test_assignment_outside_discharge_keeps_inside_executions(tmp_path):
    _write_document(tmp_path / 'primary.docx', _primary_lines())
    _write_document(tmp_path / 'mis.docx', _discharge_lines())
    doc = Document()
    doc.add_paragraph('Лист назначений')
    doc.add_paragraph('Номер ИБ: СКП5906/26')
    table = doc.add_table(rows=2, cols=6)
    for cell, value in zip(table.rows[0].cells, ('Назначения', 'время', 'кабинет', '11.08.2026', '12.08.2026', '18.08.2026')):
        cell.text = value
    table.cell(1, 0).text = 'A19.23.002.014 Индивидуальное занятие ЛФК'
    for col in (3, 4, 5):
        table.cell(1, col).text = '+'
    doc.save(tmp_path / 'assignment.docx')
    draft = scan_discharge_summary(tmp_path)
    assert len(draft.completed_procedures) == 1
    assert draft.completed_procedures[0].actual_count == 2
    assert all(d.day <= 17 for d in draft.completed_procedures[0].performed_dates)
    assert any(issue.code == 'assignment_dates_outside_episode' for issue in draft.issues)


def test_confirmed_discharge_date_reprojects_procedures(tmp_path):
    test_assignment_outside_discharge_keeps_inside_executions(tmp_path)
    source = tmp_path / "mis.docx"
    original = source.read_bytes()
    draft = scan_discharge_summary(
        tmp_path, discharge_datetime_override=datetime(2026, 8, 18, 12),
    )
    assert draft.discharge_datetime == datetime(2026, 8, 18, 12)
    assert draft.completed_procedures[0].actual_count == 3
    assert draft.completed_procedures[0].performed_dates[-1].day == 18
    assert "18.08.2026 12:00" in draft.header_text
    assert "discharge_datetime" in draft.manual_fields
    assert not draft.requires_period_rescan()
    assert not any(issue.code == "assignment_dates_outside_episode" for issue in draft.issues)
    assert source.read_bytes() == original
