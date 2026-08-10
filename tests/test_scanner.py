from datetime import date, datetime
from pathlib import Path

from docx import Document

from mdrk_builder.application.scanner import (
    ScannedRecord,
    _initial_mdrk_day,
    _latest_clinical_sections,
    _merge_dates,
    _merge_icf,
    _merge_identity,
    _records_for_selected_medical_record,
    scan_patient_folder,
)
from mdrk_builder.domain import Episode, ReviewSeverity, SpecialistRole
from mdrk_builder.infrastructure.classifier import DocumentClassification
from mdrk_builder.infrastructure.ooxml_reader import (
    BodyItem,
    ParsedCell,
    ParsedDocument,
    ParsedRow,
    ParsedTable,
)


def _document(path: str, text: str = "", tables: list[ParsedTable] | None = None) -> ParsedDocument:
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


def _record(
    path: str,
    text: str,
    *,
    role: SpecialistRole = SpecialistRole.NEUROLOGIST,
    document_type: str = "initial",
    clinical_datetime: datetime | None = None,
    tables: list[ParsedTable] | None = None,
) -> ScannedRecord:
    return ScannedRecord(
        _document(path, text, tables),
        DocumentClassification(role, document_type),
        clinical_datetime,
    )


def _row(values: dict[int, str], logical_cols: int = 15) -> ParsedRow:
    return ParsedRow(
        tuple(ParsedCell(column, 1, value) for column, value in sorted(values.items())),
        logical_cols,
    )


def _icf_table(*rows: ParsedRow) -> ParsedTable:
    return ParsedTable((_row({0: "МКФ", 13: "Ответственный специалист"}), *rows))


def test_initial_mdrk_day_rule_for_each_admission_weekday() -> None:
    cases = {
        date(2026, 8, 3): date(2026, 8, 4),   # Monday -> next day
        date(2026, 8, 4): date(2026, 8, 5),
        date(2026, 8, 5): date(2026, 8, 6),
        date(2026, 8, 6): date(2026, 8, 7),
        date(2026, 8, 7): date(2026, 8, 10),  # Friday -> Monday
        date(2026, 8, 8): date(2026, 8, 11),  # weekend -> Tuesday
        date(2026, 8, 9): date(2026, 8, 11),
    }

    assert {value: _initial_mdrk_day(value) for value in cases} == cases


def test_meeting_dates_use_clinic_weekend_rule_and_actual_course_length() -> None:
    record = _record(
        "/patient/source.docx",
        "Дата и время поступления: 05.06.2026 12:12\n"
        "Дата и время выписки: 21.06.2026 12:00",
        role=SpecialistRole.PHYSICAL_THERAPIST,
        document_type="follow_up",
        clinical_datetime=datetime(2026, 6, 19, 11, 30),
    )
    episode = Episode(Path("/patient"))

    _merge_dates(episode, [record])

    assert episode.admission_datetime == datetime(2026, 6, 5, 12, 12)
    assert episode.initial_meeting_at == datetime(2026, 6, 8, 8, 0)
    assert episode.final_meeting_at == datetime(2026, 6, 19, 11, 30)
    assert episode.course_duration_days == 16


def test_explicit_final_mdrk_schedule_overrides_latest_specialist_time() -> None:
    clinical = _record(
        "/patient/ft.docx",
        "Дата и время поступления: 05.06.2026 12:12\n"
        "Дата и время выписки: 21.06.2026 12:00",
        role=SpecialistRole.PHYSICAL_THERAPIST,
        document_type="follow_up",
        clinical_datetime=datetime(2026, 6, 19, 11, 30),
    )
    schedule = _record(
        "/patient/turnaround.docx",
        "",
        role=SpecialistRole.OTHER,
        document_type="administrative",
        tables=[
            ParsedTable(
                (
                    _row({0: "Медицинское вмешательство", 1: "Дата", 2: "Исполнение"}, 3),
                    _row({0: "Консилиум МДРК", 1: "19.06.2026", 2: "19.06.2026 15:30"}, 3),
                )
            )
        ],
    )
    episode = Episode(Path("/patient"))

    _merge_dates(episode, [clinical, schedule])

    assert episode.initial_meeting_at == datetime(2026, 6, 8, 8)
    assert episode.final_meeting_at == datetime(2026, 6, 19, 15, 30)


def test_scan_meeting_override_is_applied_before_sections_and_icf_materialize(
    tmp_path,
) -> None:
    def write_physician_source(
        path: Path,
        *,
        heading: str,
        examined_at: str,
        diagnosis: str,
        icf_code: str,
    ) -> None:
        document = Document()
        for value in (
            heading,
            f"Дата осмотра: {examined_at}",
            "ФИО пациента: Тестов Тест Тестович",
            "Номер ИБ: 123/26",
            "Дата и время поступления: 05.06.2026 12:00",
            f"Клинический диагноз: {diagnosis}",
        ):
            document.add_paragraph(value)
        table = document.add_table(rows=2, cols=15)
        table.cell(0, 0).text = "МКФ"
        table.cell(0, 13).text = "Ответственный специалист"
        table.cell(1, 0).text = icf_code
        table.cell(1, 1).text = "Тестовый домен"
        table.cell(1, 11).text = "3"
        table.cell(1, 13).text = "невролог"
        document.save(path)

    write_physician_source(
        tmp_path / "невролог первичный.docx",
        heading="Первичный осмотр невролога",
        examined_at="05.06.2026 13:00",
        diagnosis="исходный",
        icf_code="s110",
    )
    write_physician_source(
        tmp_path / "невролог повторный.docx",
        heading="Повторный осмотр невролога",
        examined_at="19.06.2026 16:00",
        diagnosis="слишком поздний",
        icf_code="s999",
    )

    episode = scan_patient_folder(
        tmp_path,
        final_meeting_at=datetime(2026, 6, 19, 15, 30),
    )

    assert episode.final_meeting_at == datetime(2026, 6, 19, 15, 30)
    assert episode.sections.clinical_diagnosis == "исходный"
    assert {domain.code for domain in episode.icf_domains} == {"s110"}


def test_mixed_record_numbers_and_admission_dates_are_blocking() -> None:
    records = [
        _record(
            "/patient/stay-1.docx",
            "ФИО пациента: Тестов Тест Тестович\nНомер ИБ: СКП100/26\n"
            "Дата и время поступления: 05.06.2026 12:00",
        ),
        _record(
            "/patient/stay-2.docx",
            "ФИО пациента: Тестов Тест Тестович\nНомер ИБ: СКП200/26\n"
            "Дата и время поступления: 01.07.2026 09:00",
        ),
    ]
    episode = Episode(Path("/patient"))

    _merge_identity(episode, records)
    _merge_dates(episode, records)

    blocking_codes = {
        issue.code for issue in episode.issues if issue.severity is ReviewSeverity.BLOCKING
    }
    assert "identity_conflict_medical_record_number" in blocking_codes
    assert "mixed_hospitalizations_admission_date" in blocking_codes
    admission_conflict = next(
        issue
        for issue in episode.issues
        if issue.code == "mixed_hospitalizations_admission_date"
    )
    assert admission_conflict.source == Path("/patient/stay-2.docx")


def test_equivalent_record_number_formats_do_not_create_false_conflict() -> None:
    records = [
        _record(
            "/patient/a.docx",
            "Номер ИБ: СКП9002/99",
            clinical_datetime=datetime(2026, 8, 3, 9),
        ),
        _record(
            "/patient/b.docx",
            "Номер ИБ: 9002 / 99",
            clinical_datetime=datetime(2026, 8, 3, 8),
        ),
    ]
    episode = Episode(Path("/patient"))

    _merge_identity(episode, records)

    assert not any(
        issue.code == "identity_conflict_medical_record_number"
        for issue in episode.issues
    )


def test_record_number_conflict_points_to_genuinely_different_source() -> None:
    equivalent_path = Path("/patient/equivalent.docx")
    foreign_path = Path("/patient/foreign.docx")
    records = [
        _record(
            "/patient/chosen.docx",
            "Номер ИБ: СКП9002/99",
            clinical_datetime=datetime(2026, 8, 3, 10),
        ),
        _record(
            str(equivalent_path),
            "Номер ИБ: 9002 / 99",
            clinical_datetime=datetime(2026, 8, 3, 9),
        ),
        _record(
            str(foreign_path),
            "Номер ИБ: СКП9003/99",
            clinical_datetime=datetime(2026, 8, 3, 8),
        ),
    ]
    episode = Episode(Path("/patient"))

    _merge_identity(episode, records)

    conflict = next(
        issue
        for issue in episode.issues
        if issue.code == "identity_conflict_medical_record_number"
    )
    assert conflict.source == foreign_path
    assert conflict.source != equivalent_path


def test_scan_overrides_select_episode_before_materialization(tmp_path) -> None:
    def write_source(
        path: Path,
        *,
        record_number: str,
        admission: str,
        examined_at: str,
        diagnosis: str,
    ) -> None:
        document = Document()
        for value in (
            "Первичный осмотр невролога",
            f"Дата осмотра: {examined_at}",
            "ФИО пациента: Тестов Тест Тестович",
            f"Номер ИБ: {record_number}",
            f"Дата и время поступления: {admission}",
            f"Клинический диагноз: {diagnosis}",
        ):
            document.add_paragraph(value)
        document.save(path)

    selected_path = tmp_path / "невролог stay-a.docx"
    excluded_path = tmp_path / "невролог stay-b.docx"
    write_source(
        selected_path,
        record_number="СКП100/26",
        admission="05.06.2026 12:00",
        examined_at="05.06.2026 13:00",
        diagnosis="диагноз выбранного эпизода",
    )
    write_source(
        excluded_path,
        record_number="СКП200/26",
        admission="01.07.2026 09:00",
        examined_at="01.07.2026 10:00",
        diagnosis="диагноз другого эпизода",
    )

    episode = scan_patient_folder(
        tmp_path,
        medical_record_number_override="СКП 100 / 26",
        admission_datetime_override=datetime(2026, 6, 5, 12, 34),
    )

    assert episode.identity.medical_record_number == "СКП 100 / 26"
    assert episode.materialized_medical_record_number == "СКП 100 / 26"
    assert episode.admission_datetime == datetime(2026, 6, 5, 12, 34)
    assert episode.materialized_admission_datetime == datetime(2026, 6, 5, 12, 34)
    assert episode.initial_meeting_at == datetime(2026, 6, 8, 8)
    assert episode.initial_sections.clinical_diagnosis == "диагноз выбранного эпизода"
    assert excluded_path.resolve() in episode.excluded_source_paths
    record_conflict = next(
        issue
        for issue in episode.issues
        if issue.code == "identity_conflict_medical_record_number"
    )
    assert record_conflict.source == excluded_path.resolve()
    assert not any(
        issue.code == "record_number_override_without_source"
        for issue in episode.issues
    )


def test_unknown_record_number_override_is_blocking(tmp_path) -> None:
    document = Document()
    for value in (
        "Первичный осмотр невролога",
        "Дата осмотра: 05.06.2026 13:00",
        "ФИО пациента: Тестов Тест Тестович",
        "Номер ИБ: СКП100/26",
        "Дата и время поступления: 05.06.2026 12:00",
        "Клинический диагноз: тестовый диагноз",
    ):
        document.add_paragraph(value)
    document.save(tmp_path / "невролог.docx")

    episode = scan_patient_folder(
        tmp_path,
        medical_record_number_override="СКП999/26",
    )

    assert any(
        issue.code == "record_number_override_without_source"
        and issue.severity is ReviewSeverity.BLOCKING
        for issue in episode.issues
    )


def test_different_medical_record_stays_visible_but_cannot_supply_episode_data() -> None:
    physician_path = "/patient/rehab-primary.docx"
    cardiology_path = "/patient/cardiology-discharge.docx"
    records = [
        _record(
            physician_path,
            "ФИО пациента: Тестов Тест Тестович\n"
            "Номер ИБ: СКП9002/99\n"
            "Дата и время поступления: 01.08.2026 09:11\n"
            "Клинический диагноз: реабилитационный диагноз\n"
            "Лабораторные исследования: анализы эпизода",
            role=SpecialistRole.NEUROLOGIST,
            document_type="initial",
            clinical_datetime=datetime(2026, 8, 3, 8, 39),
        ),
        _record(
            cardiology_path,
            "ФИО пациента: Тестов Тест Тестович\n"
            "Номер ИБ: СКП9003/99\n"
            "Дата и время поступления: 28.07.2026 14:17\n"
            "Дата и время выписки: 01.08.2026 12:00\n"
            "Клинический диагноз: чужой кардиологический диагноз\n"
            "Лабораторные исседования: чужие анализы",
            role=SpecialistRole.OTHER,
            document_type="final",
            clinical_datetime=datetime(2026, 8, 1, 12),
        ),
    ]
    episode = Episode(Path("/patient"))

    _merge_identity(episode, records)
    active = _records_for_selected_medical_record(episode, records)
    _merge_dates(episode, active)
    _latest_clinical_sections(episode, active)

    assert episode.identity.medical_record_number == "СКП9002/99"
    assert [item.document.source_path for item in active] == [Path(physician_path)]
    assert episode.excluded_source_paths == {Path(cardiology_path)}
    assert episode.admission_datetime == datetime(2026, 8, 1, 9, 11)
    assert episode.discharge_datetime is None
    assert episode.initial_meeting_at == datetime(2026, 8, 4, 8)
    assert episode.initial_sections.clinical_diagnosis == "реабилитационный диагноз"
    assert episode.initial_sections.laboratory_results == "анализы эпизода"
    assert any(issue.code == "source_medical_record_mismatch" for issue in episode.issues)
    assert not any(
        issue.code == "physician_source_after_meeting" for issue in episode.issues
    )
    assert episode.initial_sections.rehabilitation_potential == "средний"
    assert episode.sections.rehabilitation_potential == "средний"


def test_future_physician_previews_missing_fields_even_when_older_physician_is_empty() -> None:
    older_empty = _record(
        "/patient/older.docx",
        "Клинический диагноз:",
        clinical_datetime=datetime(2026, 8, 1, 12),
    )
    future_primary = _record(
        "/patient/primary.docx",
        "Клинический диагноз: диагноз для проверки\n"
        "Анамнез заболевания: анамнез для проверки",
        clinical_datetime=datetime(2026, 8, 3, 8, 39),
    )
    episode = Episode(Path("/patient"))
    episode.initial_meeting_at = datetime(2026, 8, 2, 8)
    episode.final_meeting_at = datetime(2026, 8, 4, 8)

    _latest_clinical_sections(episode, [older_empty, future_primary])

    assert episode.initial_sections.clinical_diagnosis == "диагноз для проверки"
    assert episode.initial_sections.disease_history == "анамнез для проверки"
    assert any(
        issue.code == "physician_source_after_meeting"
        and issue.severity is ReviewSeverity.BLOCKING
        and issue.field == "initial_meeting_at"
        for issue in episode.issues
    )


def test_other_consilium_cannot_supply_latest_physician_sections() -> None:
    physician = _record(
        "/patient/initial.docx",
        "Клинический диагноз: верный диагноз",
        clinical_datetime=datetime(2026, 6, 5, 13),
    )
    peg = _record(
        "/patient/консилиум гастростома.docx",
        "Клинический диагноз: не брать из PEG-протокола",
        role=SpecialistRole.OTHER,
        document_type="other_consilium",
        clinical_datetime=datetime(2026, 6, 20, 13),
    )
    episode = Episode(Path("/patient"))

    _latest_clinical_sections(episode, [physician, peg])

    assert episode.sections.clinical_diagnosis == "верный диагноз"


def test_clinical_sections_are_selected_per_field_as_of_each_meeting() -> None:
    initial = _record(
        "/patient/initial.docx",
        "Клинический диагноз: исходный\nАнамнез заболевания: первичный анамнез",
        clinical_datetime=datetime(2026, 6, 5, 13),
    )
    follow_up = _record(
        "/patient/follow-up.docx",
        "Клинический диагноз: уточнённый",
        document_type="follow_up",
        clinical_datetime=datetime(2026, 6, 15, 9),
    )
    after_final_meeting = _record(
        "/patient/discharge.docx",
        "Клинический диагноз: слишком поздний",
        document_type="final",
        clinical_datetime=datetime(2026, 6, 21, 12),
    )
    episode = Episode(Path("/patient"))
    episode.initial_meeting_at = datetime(2026, 6, 6, 8)
    episode.final_meeting_at = datetime(2026, 6, 19, 11, 30)

    _latest_clinical_sections(episode, [initial, follow_up, after_final_meeting])

    assert episode.initial_sections.clinical_diagnosis == "исходный"
    assert episode.initial_sections.disease_history == "первичный анамнез"
    assert episode.sections.clinical_diagnosis == "уточнённый"
    assert episode.sections.disease_history == "первичный анамнез"
    assert episode.initial_field_sources["sections.clinical_diagnosis"] == Path(
        "/patient/initial.docx"
    )
    assert episode.field_sources["sections.clinical_diagnosis"] == Path(
        "/patient/follow-up.docx"
    )


def test_specialist_copy_cannot_fill_physician_owned_clinical_fields() -> None:
    physician = _record(
        "/patient/physician.docx",
        "Клинический диагноз:",
        clinical_datetime=datetime(2026, 6, 5, 13),
    )
    physical_therapist = _record(
        "/patient/ft.docx",
        "Клинический диагноз: копия диагноза\n"
        "Медикаментозная терапия: копия лечения",
        role=SpecialistRole.PHYSICAL_THERAPIST,
        clinical_datetime=datetime(2026, 6, 5, 14),
    )
    episode = Episode(Path("/patient"))
    episode.initial_meeting_at = datetime(2026, 6, 6, 8)
    episode.final_meeting_at = datetime(2026, 6, 19, 11)

    _latest_clinical_sections(episode, [physician, physical_therapist])

    assert episode.initial_sections.clinical_diagnosis == ""
    assert episode.initial_sections.medication == ""
    assert episode.sections.clinical_diagnosis == ""
    assert episode.sections.medication == ""


def test_icf_ownership_filters_physician_copy_and_keeps_dynamics_and_pf() -> None:
    physician_initial = _record(
        "/patient/physician-initial.docx",
        "",
        clinical_datetime=datetime(2026, 6, 5, 13),
        tables=[
            _icf_table(
                _row({0: "b2351", 1: "Равновесие", 11: "2", 13: "ФТ"}),
                _row({0: "s110", 1: "Структура головного мозга", 11: "3", 13: "МРТ"}),
                ParsedRow(
                    (ParsedCell(0, 1, "Pf"), ParsedCell(1, 14, "Мужчина 58 лет")),
                    15,
                ),
            )
        ],
    )
    physician_final = _record(
        "/patient/physician-final.docx",
        "",
        document_type="final",
        clinical_datetime=datetime(2026, 6, 21, 12),
        tables=[
            _icf_table(
                _row({0: "b2351", 1: "Равновесие", 11: "2", 12: "1", 13: "ФТ"}),
                _row({0: "s110", 1: "Структура головного мозга", 11: "3", 12: "4", 13: "МРТ"}),
                _row({0: "s999", 1: "Слишком поздний домен", 11: "4", 12: "4", 13: "МРТ"}),
                ParsedRow(
                    (ParsedCell(0, 1, "Pf"), ParsedCell(1, 14, "Мужчина 58 лет")),
                    15,
                ),
            )
        ],
    )
    physician_follow_up = _record(
        "/patient/physician-follow-up.docx",
        "",
        document_type="follow_up",
        clinical_datetime=datetime(2026, 6, 15, 9),
        tables=[
            _icf_table(
                _row({0: "s110", 1: "Структура головного мозга", 11: "3", 12: "3", 13: "МРТ"})
            )
        ],
    )
    psychologist = _record(
        "/patient/pathopsychologist.docx",
        "",
        role=SpecialistRole.PATHOPSYCHOLOGIST,
        document_type="consultation",
        clinical_datetime=datetime(2026, 6, 5, 15),
        tables=[
            _icf_table(
                ParsedRow(
                    (ParsedCell(0, 1, "Pf"), ParsedCell(1, 14, "Мужской, 58 лет")),
                    15,
                ),
                _row({0: "e310", 1: "Семья и близкие родственники", 11: "0", 13: "патопсихолог"}),
            )
        ],
    )
    ft_initial = _record(
        "/patient/ft-initial.docx",
        "",
        role=SpecialistRole.PHYSICAL_THERAPIST,
        clinical_datetime=datetime(2026, 6, 5, 14, 30),
        tables=[_icf_table(_row({0: "b2351", 1: "Равновесие", 11: "2"}))],
    )
    ft_final = _record(
        "/patient/ft-final.docx",
        "",
        role=SpecialistRole.PHYSICAL_THERAPIST,
        document_type="follow_up",
        clinical_datetime=datetime(2026, 6, 19, 11, 30),
        tables=[_icf_table(_row({0: "b2351", 1: "Равновесие", 11: "2", 12: "1"}))],
    )
    episode = Episode(Path("/patient"))
    episode.initial_meeting_at = datetime(2026, 6, 6, 8)
    episode.final_meeting_at = datetime(2026, 6, 19, 15, 30)
    episode.initial_sections.medication = "Лозартан 25 мг"
    episode.sections.medication = "Лозартан 25 мг"
    episode.initial_field_sources["sections.medication"] = Path(
        "/patient/physician-initial.docx"
    )
    episode.field_sources["sections.medication"] = Path(
        "/patient/physician-initial.docx"
    )

    _merge_icf(
        episode,
        [
            physician_initial,
            physician_follow_up,
            physician_final,
            psychologist,
            ft_initial,
            ft_final,
        ],
    )

    b_domains = [item for item in episode.icf_domains if item.code == "b2351"]
    assert len(b_domains) == 1
    assert b_domains[0].specialist is SpecialistRole.PHYSICAL_THERAPIST
    assert b_domains[0].initial and b_domains[0].initial.value == 2
    assert b_domains[0].final and b_domains[0].final.value == 1
    assert b_domains[0].dynamic_marker == "+"

    structure = next(item for item in episode.icf_domains if item.code == "s110")
    assert structure.specialist is SpecialistRole.NEUROLOGIST
    assert structure.initial and structure.final
    assert structure.initial.value == structure.final.value == 3
    assert structure.final_source == Path("/patient/physician-follow-up.docx")
    assert not any(item.code == "s999" for item in episode.icf_domains)

    personal = next(item for item in episode.icf_domains if item.code == "Pf")
    assert personal.description == "Мужской, 58 лет"
    assert personal.specialist is SpecialistRole.PATHOPSYCHOLOGIST
    assert personal.initial is None and personal.final is None
    assert not any(issue.field == "icf.Pf" for issue in episode.issues)

    zero_domain = next(item for item in episode.icf_domains if item.code == "e310")
    assert zero_domain.initial and zero_domain.initial.value == 0

    medication = next(item for item in episode.icf_domains if item.code == "e1101")
    assert medication.initial and medication.initial.display() == "4+"
    assert medication.final and medication.final.display() == "4+"
    assert medication.initial_source == Path("/patient/physician-initial.docx")
    assert medication.final_source == Path("/patient/physician-initial.docx")


def test_follow_up_only_domain_does_not_backfill_initial_point() -> None:
    follow_up = _record(
        "/patient/ft-follow-up.docx",
        "",
        role=SpecialistRole.PHYSICAL_THERAPIST,
        document_type="follow_up",
        clinical_datetime=datetime(2026, 6, 11, 13),
        tables=[
            _icf_table(
                _row({0: "b999", 1: "Новый домен", 11: "2", 12: "1"})
            )
        ],
    )
    episode = Episode(Path("/patient"))
    episode.initial_meeting_at = datetime(2026, 6, 6, 8)
    episode.final_meeting_at = datetime(2026, 6, 19, 15, 30)

    _merge_icf(episode, [follow_up])

    domain = next(item for item in episode.icf_domains if item.code == "b999")
    assert domain.initial is None
    assert domain.initial_source is None
    assert domain.final and domain.final.value == 1
