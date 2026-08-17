from datetime import datetime
from pathlib import Path

from mdrk_builder.application.final_mdrk import (
    apply_final_mdrk_document,
    select_final_mdrk_document,
    validate_final_scale_measurements,
)
from mdrk_builder.application.discharge_summary import _project_scale_rows
from mdrk_builder.application.episode_identity import DischargeEpisodeKey
from mdrk_builder.application.snapshot import build_snapshot
from mdrk_builder.application.source_scan import ScannedDocument, SourceScanResult
from mdrk_builder.domain import (
    Episode,
    IcfDomain,
    IcfQualifier,
    MdrkKind,
    ScaleMeasurement,
    SpecialistFinding,
    SpecialistRole,
)
from mdrk_builder.infrastructure.classifier import DocumentClassification
from mdrk_builder.infrastructure.ooxml_reader import (
    BodyItem,
    ParsedCell,
    ParsedDocument,
    ParsedRow,
    ParsedTable,
)


EPISODE_ROOT = Path("/episode")


def _mdrk_document(
    name: str,
    body: str,
    *,
    full_name: str = "ПАЦИЕНТ ТЕСТОВЫЙ ПРИМЕР",
    record: str = "СКП5906/26",
    admission: str = "10.08.2026 10:00",
    meeting: str | None = "17.08.2026 10:00",
    tables: tuple[ParsedTable, ...] = (),
) -> ScannedDocument:
    paragraphs = ["Консилиум мультидисциплинарной реабилитационной команды"]
    if meeting:
        paragraphs.append(meeting)
    if full_name:
        paragraphs.append(f"ФИО пациента: {full_name}")
    if record:
        paragraphs.append(f"Номер ИБ: {record}")
    if admission:
        paragraphs.append(f"Дата и время поступления: {admission}")
    paragraphs.extend(body.splitlines())
    path = EPISODE_ROOT / name
    document = ParsedDocument(
        source_path=path,
        normalized_path=path,
        paragraphs=paragraphs,
        tables=list(tables),
        body_items=[
            *(BodyItem("paragraph", index) for index in range(len(paragraphs))),
            *(BodyItem("table", index) for index in range(len(tables))),
        ],
    )
    return ScannedDocument(
        document=document,
        classification=DocumentClassification(
            SpecialistRole.OTHER,
            "mdrk",
            is_mdrk=True,
            mdrk_kind=MdrkKind.FINAL,
        ),
    )


def _episode_key() -> DischargeEpisodeKey:
    return DischargeEpisodeKey(
        normalized_full_name="пациент тестовый пример",
        medical_record_number="5906/26",
        admission_at=datetime(2026, 8, 10, 10),
        discharge_at=datetime(2026, 8, 17, 12),
        episode_root=EPISODE_ROOT,
    )


def _source_scan(*documents: ScannedDocument) -> SourceScanResult:
    return SourceScanResult(
        source_files=tuple(item.document.source_path for item in documents),
        documents=documents,
        failures=(),
        root=EPISODE_ROOT,
    )


def _row(values: dict[int, str], logical_cols: int) -> ParsedRow:
    return ParsedRow(
        tuple(
            ParsedCell(column, 1, value)
            for column, value in sorted(values.items())
        ),
        logical_cols,
    )


def test_final_mdrk_selection_requires_completed_program_structure() -> None:
    incomplete = _mdrk_document(
        "nominal-mdrk-2.docx",
        "12. Индивидуальный план медицинской реабилитации",
    )
    completed = _mdrk_document(
        "actual-final.docx",
        "12. Выполненная программа медицинской реабилитации",
    )

    selected = select_final_mdrk_document(
        _source_scan(incomplete, completed),
        episode_key=_episode_key(),
    )

    assert selected is completed


def test_generated_final_mdrk_can_be_discharge_source() -> None:
    generated = _mdrk_document(
        "generated-final.docx",
        "12. Выполненная программа медицинской реабилитации",
    )
    generated = ScannedDocument(
        generated.document,
        DocumentClassification(
            SpecialistRole.OTHER,
            "mdrk",
            is_mdrk=True,
            mdrk_kind=MdrkKind.FINAL,
            is_generated_output=True,
        ),
    )

    selected = select_final_mdrk_document(
        _source_scan(generated),
        episode_key=_episode_key(),
    )

    assert selected is generated


def test_final_mdrk_selection_reports_equal_candidates_as_blocking() -> None:
    first = _mdrk_document(
        "first-final.docx",
        "12. Выполненная программа медицинской реабилитации",
    )
    second = _mdrk_document(
        "second-final.docx",
        "12. Выполненная программа медицинской реабилитации",
    )
    issues = []

    selected = select_final_mdrk_document(
        _source_scan(first, second),
        episode_key=_episode_key(),
        issues=issues,
    )

    assert selected is None
    assert [issue.code for issue in issues] == ["final_mdrk_source_ambiguous"]
    assert issues[0].severity.value == "blocking"


def test_final_mdrk_selection_rejects_document_after_discharge() -> None:
    valid = _mdrk_document(
        "valid-final.docx",
        "12. Выполненная программа медицинской реабилитации",
        meeting="17.08.2026 10:00",
    )
    future = _mdrk_document(
        "future-final.docx",
        "12. Выполненная программа медицинской реабилитации",
        meeting="18.08.2026 09:00",
    )

    selected = select_final_mdrk_document(
        _source_scan(valid, future),
        episode_key=_episode_key(),
    )

    assert selected is valid


def test_final_mdrk_selection_rejects_missing_mrn_for_different_patient() -> None:
    different_patient = _mdrk_document(
        "different-patient.docx",
        "12. Выполненная программа медицинской реабилитации",
        full_name="ДРУГОЙ ПАЦИЕНТ ТЕСТОВЫЙ",
        record="",
    )

    selected = select_final_mdrk_document(
        _source_scan(different_patient),
        episode_key=_episode_key(),
    )

    assert selected is None


def test_final_mdrk_selection_accepts_same_name_when_mrn_is_missing() -> None:
    same_patient = _mdrk_document(
        "mdrk/final-without-record.docx",
        "12. Выполненная программа медицинской реабилитации",
        record="",
    )

    selected = select_final_mdrk_document(
        _source_scan(same_patient),
        episode_key=_episode_key(),
    )

    assert selected is same_patient


def test_final_mdrk_selection_rejects_same_name_without_episode_anchor() -> None:
    same_name_only = _mdrk_document(
        "undated-final-without-record-or-admission.docx",
        "12. Выполненная программа медицинской реабилитации",
        record="",
        admission="",
        meeting=None,
    )

    selected = select_final_mdrk_document(
        _source_scan(same_name_only),
        episode_key=_episode_key(),
    )

    assert selected is None


def test_final_mdrk_selection_accepts_same_name_with_dated_episode_point() -> None:
    dated_same_name = _mdrk_document(
        "dated-final-without-record-or-admission.docx",
        "12. Выполненная программа медицинской реабилитации",
        record="",
        admission="",
        meeting="17.08.2026 23:59",
    )

    selected = select_final_mdrk_document(
        _source_scan(dated_same_name),
        episode_key=_episode_key(),
    )

    assert selected is dated_same_name


def test_final_mdrk_selection_accepts_discharge_boundary_day() -> None:
    same_day = _mdrk_document(
        "same-day-final.docx",
        "12. Выполненная программа медицинской реабилитации",
        meeting="17.08.2026 23:59",
    )

    selected = select_final_mdrk_document(
        _source_scan(same_day),
        episode_key=_episode_key(),
    )

    assert selected is same_day


def test_final_mdrk_selection_prefers_verified_dated_candidate() -> None:
    dated = _mdrk_document(
        "dated-final.docx",
        "12. Выполненная программа медицинской реабилитации",
    )
    undated = _mdrk_document(
        "undated-final.docx",
        "12. Выполненная программа медицинской реабилитации",
        meeting=None,
    )

    selected = select_final_mdrk_document(
        _source_scan(undated, dated),
        episode_key=_episode_key(),
    )

    assert selected is dated


def test_invalid_rivermead_value_uses_corroborated_discharge_value() -> None:
    source = Path("final-mdrk.docx")
    issues = []
    measurement = ScaleMeasurement(
        "Индекс мобильности Ривермид",
        "79",
        None,
        SpecialistRole.FRM,
        source,
    )

    result = validate_final_scale_measurements(
        [measurement],
        {"Индекс мобильности Ривермид": "9"},
        source=source,
        issues=issues,
    )

    assert result[0].value == "9"
    assert [issue.code for issue in issues] == ["scale_value_out_of_range"]
    assert "использовано подтверждающее значение" in issues[0].message


def test_rankin_alias_out_of_range_is_reported() -> None:
    source = Path("final-mdrk.docx")
    issues = []

    result = validate_final_scale_measurements(
        [
            ScaleMeasurement(
                "Модифицированная шкала Rankin",
                "9",
                None,
                SpecialistRole.FRM,
                source,
            )
        ],
        {},
        source=source,
        issues=issues,
    )

    assert result[0].value == "9"
    assert [issue.code for issue in issues] == ["scale_value_out_of_range"]
    assert "0–6" in issues[0].message


def test_conflicting_confirmation_aliases_do_not_replace_invalid_scale() -> None:
    source = Path("final-mdrk.docx")
    issues = []
    measurement = ScaleMeasurement(
        "Модифицированная шкала Rankin",
        "9",
        None,
        SpecialistRole.FRM,
        source,
    )

    result = validate_final_scale_measurements(
        [measurement],
        {
            "Модифицированная шкала Рэнкина": "4",
            "Шкала Rankin": "5",
        },
        source=source,
        issues=issues,
    )

    assert result[0].value == "9"
    assert [issue.code for issue in issues] == ["scale_value_out_of_range"]
    assert "конфликтующие подтверждения" in issues[0].message


def test_matching_confirmation_aliases_count_as_one_value() -> None:
    source = Path("final-mdrk.docx")
    measurement = ScaleMeasurement(
        "Модифицированная шкала Rankin",
        "9",
        None,
        SpecialistRole.FRM,
        source,
    )

    result = validate_final_scale_measurements(
        [measurement],
        {
            "Модифицированная шкала Рэнкина": "4",
            "Шкала Rankin": "4 балла",
        },
        source=source,
        issues=[],
    )

    assert result[0].value == "4"


def test_final_mdrk_is_only_source_of_discharge_icf_and_scales() -> None:
    final_source = _mdrk_document(
        "partial-final.docx",
        "\n".join(
            (
                "12. Выполненная программа медицинской реабилитации",
                "Результат осмотра врача ФРМ",
            )
        ),
        tables=(
            ParsedTable(
                (
                    _row(
                        {
                            0: "Дата и время расчета шкалы",
                            1: "Шкала/опросник",
                            2: "Результат расчета",
                        },
                        3,
                    ),
                    _row(
                        {
                            0: "17.08.2026 10:00",
                            1: "Индекс мобильности Ривермид",
                            2: "9",
                        },
                        3,
                    ),
                )
            ),
            ParsedTable(
                (
                    _row({0: "МКФ", 13: "Ответственный специалист МДРК"}, 15),
                    _row(
                        {
                            0: "d450",
                            1: "Ходьба",
                            11: "2",
                            12: "1",
                            13: "ФТ",
                        },
                        15,
                    ),
                )
            ),
        ),
    )
    baseline_source = Path("/episode/primary.docx")
    follow_up_source = Path("/episode/follow-up.docx")
    episode = Episode(EPISODE_ROOT)
    episode.final_meeting_at = datetime(2026, 8, 17, 10)
    episode.findings = [
        SpecialistFinding(
            SpecialistRole.NEUROLOGIST,
            source_datetime=datetime(2026, 8, 10, 10),
            source=baseline_source,
            scales=[
                ScaleMeasurement(
                    "Индекс мобильности Ривермид",
                    "4",
                    datetime(2026, 8, 10, 10),
                    SpecialistRole.NEUROLOGIST,
                    baseline_source,
                ),
                ScaleMeasurement(
                    "Модифицированная шкала Рэнкина",
                    "3",
                    datetime(2026, 8, 10, 10),
                    SpecialistRole.NEUROLOGIST,
                    baseline_source,
                ),
            ],
        ),
        SpecialistFinding(
            SpecialistRole.NEUROLOGIST,
            source_datetime=datetime(2026, 8, 16, 10),
            source=follow_up_source,
            scales=[
                ScaleMeasurement(
                    "Индекс мобильности Ривермид",
                    "7",
                    datetime(2026, 8, 16, 10),
                    SpecialistRole.NEUROLOGIST,
                    follow_up_source,
                ),
                ScaleMeasurement(
                    "Модифицированная шкала Рэнкина",
                    "2",
                    datetime(2026, 8, 16, 10),
                    SpecialistRole.NEUROLOGIST,
                    follow_up_source,
                ),
            ],
        ),
    ]
    episode.icf_domains = [
        IcfDomain(
            "d450",
            "Ходьба",
            SpecialistRole.PHYSICAL_THERAPIST,
            initial=IcfQualifier(3),
            final=IcfQualifier(2),
            initial_source=baseline_source,
            final_source=follow_up_source,
        ),
        IcfDomain(
            "d640",
            "Ведение домашнего хозяйства",
            SpecialistRole.OCCUPATIONAL_THERAPIST,
            initial=IcfQualifier(3),
            final=IcfQualifier(1),
            initial_source=baseline_source,
            final_source=follow_up_source,
        ),
    ]
    issues = []

    apply_final_mdrk_document(
        episode,
        final_source,
        discharge_scale_values={},
        issues=issues,
    )
    snapshot = build_snapshot(episode, MdrkKind.FINAL)
    admission_rows, discharge_rows = _project_scale_rows(
        snapshot,
        final_mdrk_source=final_source.document.source_path,
    )

    admission = {row.name: row.value for row in admission_rows}
    discharge = {row.name: row.value for row in discharge_rows}
    assert admission["Индекс мобильности Ривермид"] == "4"
    assert discharge["Индекс мобильности Ривермид"] == "9"
    assert admission["Модифицированная шкала Рэнкина"] == "3"
    assert discharge["Модифицированная шкала Рэнкина"] == ""

    by_code = {domain.code: domain for domain in snapshot.icf_domains}
    assert by_code["d450"].final == IcfQualifier(1)
    assert by_code["d450"].final_source == final_source.document.source_path
    assert by_code["d640"].initial == IcfQualifier(3)
    assert by_code["d640"].final is None
    assert by_code["d640"].final_source is None
    assert {issue.code for issue in issues} >= {
        "final_mdrk_scale_rows_missing",
        "final_mdrk_icf_rows_missing",
    }


def test_final_mdrk_owns_potential_and_goal_provenance() -> None:
    final_mdrk = _mdrk_document(
        "final-clinical-sections.docx",
        "\n".join(
            (
                "12. Выполненная программа медицинской реабилитации",
                "Реабилитационный потенциал: высокий",
                "Цель на этап медицинской реабилитации: достигнута",
            )
        ),
    )
    discharge_source = Path("/episode/discharge.docx")
    episode = Episode(EPISODE_ROOT)
    episode.sections.rehabilitation_potential = "из выписки"
    episode.sections.goal = "из выписки"
    episode.field_sources = {
        "sections.rehabilitation_potential": discharge_source,
        "sections.goal": discharge_source,
    }

    apply_final_mdrk_document(
        episode,
        final_mdrk,
        discharge_scale_values={},
        issues=[],
    )

    assert episode.sections.rehabilitation_potential == "высокий"
    assert episode.sections.goal == "достигнута"
    assert episode.field_sources["sections.rehabilitation_potential"] == (
        final_mdrk.document.source_path
    )
    assert episode.field_sources["sections.goal"] == final_mdrk.document.source_path
