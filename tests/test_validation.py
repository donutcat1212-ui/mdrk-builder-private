from datetime import date, datetime
from pathlib import Path

import pytest

from mdrk_builder.application.validation import (
    acknowledge_conflict,
    acknowledge_issue,
    can_generate,
    clear_issue_acknowledgements,
    current_issues,
    has_issue_acknowledgements,
    is_conflict_acknowledged,
    is_issue_acknowledged,
)
from mdrk_builder.domain import (
    Episode,
    IcfDomain,
    IcfQualifier,
    MdrkKind,
    Procedure,
    ReviewIssue,
    ReviewSeverity,
    ScaleMeasurement,
    SourceDocument,
    SpecialistFinding,
    SpecialistRole,
)


def _valid_episode() -> Episode:
    episode = Episode(folder=Path("/episode"))
    episode.identity.full_name = "Тестов Тест Тестович"
    episode.identity.medical_record_number = "123/26"
    episode.admission_datetime = datetime(2026, 6, 5, 12)
    episode.initial_meeting_at = datetime(2026, 6, 6, 8)
    episode.final_meeting_at = datetime(2026, 6, 20, 11)
    episode.sections.clinical_diagnosis = "Тестовый диагноз"
    episode.initial_sections.clinical_diagnosis = "Исходный тестовый диагноз"
    episode.sources.append(
        SourceDocument(Path("/physician.docx"), role=SpecialistRole.NEUROLOGIST)
    )
    return episode


def test_manual_fill_removes_stale_required_issue() -> None:
    episode = _valid_episode()
    episode.issues.append(
        # This issue was emitted before the user filled the field in the UI.
        ReviewIssue("required_identity_full_name", "old", field="identity.full_name")
    )

    assert can_generate(episode, MdrkKind.INITIAL)
    assert not any(issue.code.startswith("required_") for issue in current_issues(episode, MdrkKind.INITIAL))


def test_daily_rehabilitation_minutes_below_180_are_warning() -> None:
    episode = _valid_episode()
    first_day = date(2026, 8, 4)
    second_day = date(2026, 8, 5)
    episode.procedures.extend(
        (
            Procedure(
                "ЛФК",
                "ФТ",
                2,
                120,
                "ежедневно",
                source=Path("/assignment.docx"),
                performed_dates=(first_day, second_day),
            ),
            Procedure(
                "Тренажёр",
                "ФТ",
                2,
                60,
                "ежедневно",
                source=Path("/assignment.docx"),
                performed_dates=(first_day,),
            ),
            Procedure(
                "Психокоррекция",
                "Нейропсихолог",
                1,
                30,
                "однократно",
                source=Path("/assignment.docx"),
                performed_dates=(second_day,),
            ),
        )
    )

    issues = current_issues(episode, MdrkKind.INITIAL)
    issue = next(item for item in issues if item.code == "rehab_daily_minutes_below_minimum")

    assert issue.severity is ReviewSeverity.WARNING
    assert "05.08.2026 — 150 мин" in issue.message
    assert "04.08.2026" not in issue.message
    assert can_generate(episode, MdrkKind.INITIAL)


def test_weekends_are_excluded_from_daily_rehabilitation_minimum() -> None:
    episode = _valid_episode()
    saturday = date(2026, 8, 8)
    sunday = date(2026, 8, 9)
    monday = date(2026, 8, 10)
    episode.procedures.extend(
        (
            Procedure(
                "ЛФК",
                "ФТ",
                3,
                120,
                "периодически",
                performed_dates=(saturday, sunday, monday),
            ),
            Procedure(
                "Занятие без длительности",
                "ФТ",
                1,
                None,
                "однократно",
                performed_dates=(saturday,),
            ),
        )
    )

    issues = current_issues(episode, MdrkKind.INITIAL)
    deficient = next(
        issue for issue in issues if issue.code == "rehab_daily_minutes_below_minimum"
    )

    assert "10.08.2026 — 120 мин" in deficient.message
    assert "08.08.2026" not in deficient.message
    assert "09.08.2026" not in deficient.message
    assert not any(
        issue.code == "rehab_daily_minutes_incomplete" for issue in issues
    )


def test_weekend_only_rehabilitation_does_not_emit_daily_minute_issues() -> None:
    episode = _valid_episode()
    episode.procedures.append(
        Procedure(
            "Занятие",
            "ФТ",
            2,
            None,
            "ежедневно",
            performed_dates=(date(2026, 8, 8), date(2026, 8, 9)),
        )
    )

    issues = current_issues(episode, MdrkKind.INITIAL)

    assert not any(issue.code.startswith("rehab_daily_minutes_") for issue in issues)


def test_any_generated_blocker_can_be_acknowledged_and_stays_visible() -> None:
    episode = _valid_episode()
    episode.sources.clear()
    issue = next(
        item
        for item in current_issues(episode, MdrkKind.INITIAL)
        if item.code == "required_physician_source"
    )

    acknowledge_issue(episode, issue, MdrkKind.INITIAL)
    refreshed = next(
        item
        for item in current_issues(episode, MdrkKind.INITIAL)
        if item.code == "required_physician_source"
    )

    assert refreshed.acknowledged
    assert refreshed.severity is ReviewSeverity.INFO
    assert "Игнорировано вручную (было: блокирующая проблема)" in refreshed.message
    assert is_issue_acknowledged(
        episode, refreshed, MdrkKind.INITIAL
    )
    assert can_generate(episode, MdrkKind.INITIAL)


def test_acknowledged_warning_stays_visible_as_info() -> None:
    episode = _valid_episode()
    issue = next(
        item
        for item in current_issues(episode, MdrkKind.INITIAL)
        if item.code == "review_disease_history_missing"
    )

    acknowledge_issue(episode, issue, MdrkKind.INITIAL)
    refreshed = next(
        item
        for item in current_issues(episode, MdrkKind.INITIAL)
        if item.code == "review_disease_history_missing"
    )

    assert refreshed.acknowledged
    assert refreshed.severity is ReviewSeverity.INFO
    assert has_issue_acknowledgements(episode)

    clear_issue_acknowledgements(episode)

    assert not has_issue_acknowledgements(episode)


def test_acknowledgement_is_bound_to_issue_state_and_kind() -> None:
    episode = _valid_episode()
    episode.initial_sections.clinical_diagnosis = ""
    episode.sections.clinical_diagnosis = ""
    initial_issue = next(
        item
        for item in current_issues(episode, MdrkKind.INITIAL)
        if item.code == "required_diagnosis"
    )
    acknowledge_issue(episode, initial_issue, MdrkKind.INITIAL)

    final_issue = next(
        item
        for item in current_issues(episode, MdrkKind.FINAL)
        if item.code == "required_diagnosis"
    )

    assert final_issue.severity is ReviewSeverity.BLOCKING
    assert not final_issue.acknowledged
    assert not can_generate(episode, MdrkKind.FINAL)
    assert can_generate(episode, MdrkKind.INITIAL)

    episode.initial_sections.clinical_diagnosis = "Временно заполнен"
    current_issues(episode, MdrkKind.INITIAL)
    episode.initial_sections.clinical_diagnosis = ""
    recreated = next(
        item
        for item in current_issues(episode, MdrkKind.INITIAL)
        if item.code == "required_diagnosis"
    )

    assert recreated.severity is ReviewSeverity.BLOCKING
    assert not recreated.acknowledged


def test_duplicate_issue_codes_can_be_acknowledged_individually() -> None:
    episode = _valid_episode()
    episode.procedures.extend(
        (
            Procedure("ЛФК", "", 1, 30, "однократно"),
            Procedure("Тренажёр", "", 1, 30, "однократно"),
        )
    )
    warnings = [
        item
        for item in current_issues(episode, MdrkKind.INITIAL)
        if item.code == "procedure_specialist_missing"
    ]

    acknowledge_issue(episode, warnings[0], MdrkKind.INITIAL)
    refreshed = [
        item
        for item in current_issues(episode, MdrkKind.INITIAL)
        if item.code == "procedure_specialist_missing"
    ]

    assert [(item.field, item.acknowledged) for item in refreshed] == [
        ("procedures.0", True),
        ("procedures.1", False),
    ]


def test_source_conflicts_require_explicit_value_bound_acknowledgement() -> None:
    episode = _valid_episode()
    episode.identity.medical_record_number = "РУЧНОЙ 9004/99"
    episode.admission_datetime = datetime(2026, 6, 5, 12, 30)
    episode.issues.extend(
        (
            ReviewIssue(
                "identity_conflict_medical_record_number",
                "В источниках разные номера ИБ",
                ReviewSeverity.BLOCKING,
                "identity.medical_record_number",
                Path("/cardiology.docx"),
            ),
            ReviewIssue(
                "mixed_hospitalizations_admission_date",
                "В источниках разные даты поступления",
                ReviewSeverity.BLOCKING,
                "admission_datetime",
                Path("/cardiology.docx"),
            ),
        )
    )

    assert not can_generate(episode, MdrkKind.INITIAL)

    acknowledge_conflict(episode, "identity_conflict_medical_record_number")
    acknowledge_conflict(episode, "mixed_hospitalizations_admission_date")

    issues = current_issues(episode, MdrkKind.INITIAL)
    acknowledged = [
        issue
        for issue in issues
        if issue.code
        in {
            "identity_conflict_medical_record_number",
            "mixed_hospitalizations_admission_date",
        }
    ]
    assert all(issue.severity is ReviewSeverity.INFO for issue in acknowledged)
    assert all("Исходный конфликт сохранён" in issue.message for issue in acknowledged)
    assert any("РУЧНОЙ 9004/99" in issue.message for issue in acknowledged)
    assert any("05.06.2026 12:30" in issue.message for issue in acknowledged)
    assert can_generate(episode, MdrkKind.INITIAL)
    assert all(
        issue.severity is ReviewSeverity.BLOCKING for issue in episode.issues
    )


def test_edit_after_acknowledgement_restores_source_conflict_block() -> None:
    episode = _valid_episode()
    code = "identity_conflict_medical_record_number"
    episode.issues.append(
        ReviewIssue(code, "Разные номера ИБ", ReviewSeverity.BLOCKING)
    )
    acknowledge_conflict(episode, code)

    assert is_conflict_acknowledged(episode, code)
    assert can_generate(episode, MdrkKind.INITIAL)

    episode.identity.medical_record_number = "другой номер"

    assert not is_conflict_acknowledged(episode, code)
    assert not can_generate(episode, MdrkKind.INITIAL)
    assert any(
        issue.code == code and issue.severity is ReviewSeverity.BLOCKING
        for issue in current_issues(episode, MdrkKind.INITIAL)
    )


def test_equivalent_record_number_format_keeps_materialization_current() -> None:
    episode = _valid_episode()
    episode.identity.medical_record_number = "9002 / 99"
    episode.materialized_medical_record_number = "СКП9002/99"
    episode.issues.append(
        ReviewIssue(
            "identity_conflict_medical_record_number",
            "Разные номера ИБ",
            ReviewSeverity.BLOCKING,
        )
    )

    acknowledge_conflict(episode, "identity_conflict_medical_record_number")
    issues = current_issues(episode, MdrkKind.INITIAL)

    assert is_conflict_acknowledged(
        episode, "identity_conflict_medical_record_number"
    )
    assert not any(
        issue.code == "source_selection_stale_record_number" for issue in issues
    )


def test_changed_materialized_record_number_blocks_until_rescan() -> None:
    episode = _valid_episode()
    episode.materialized_medical_record_number = "123/26"
    episode.identity.medical_record_number = "456/26"

    issues = current_issues(episode, MdrkKind.INITIAL)

    assert any(
        issue.code == "source_selection_stale_record_number"
        and issue.severity is ReviewSeverity.BLOCKING
        for issue in issues
    )
    assert not can_generate(episode, MdrkKind.INITIAL)


def test_changed_materialized_admission_blocks_until_rescan() -> None:
    episode = _valid_episode()
    episode.materialized_admission_datetime = episode.admission_datetime
    episode.admission_datetime = datetime(2026, 6, 7, 12)

    issues = current_issues(episode, MdrkKind.INITIAL)

    assert any(
        issue.code == "source_selection_stale_admission"
        and issue.severity is ReviewSeverity.BLOCKING
        for issue in issues
    )
    assert not can_generate(episode, MdrkKind.INITIAL)


def test_non_whitelisted_blocker_cannot_be_acknowledged() -> None:
    episode = _valid_episode()

    with pytest.raises(ValueError, match="нельзя подтвердить"):
        acknowledge_conflict(episode, "required_physician_source")

    episode.sources.clear()
    episode.acknowledged_conflicts["required_physician_source"] = "forced"

    assert not can_generate(episode, MdrkKind.INITIAL)
    assert any(
        issue.code == "required_physician_source"
        and issue.severity is ReviewSeverity.BLOCKING
        for issue in current_issues(episode, MdrkKind.INITIAL)
    )


def test_missing_physician_source_is_blocking() -> None:
    episode = _valid_episode()
    episode.sources.clear()

    assert not can_generate(episode, MdrkKind.FINAL)
    assert any(issue.code == "required_physician_source" for issue in current_issues(episode, MdrkKind.FINAL))


def test_personal_factor_is_descriptive_and_needs_no_numeric_pair() -> None:
    episode = _valid_episode()
    episode.icf_domains.append(
        IcfDomain(
            code="Pf",
            description="Мужчина 58 лет, мотивирован",
            specialist=SpecialistRole.NEUROPSYCHOLOGIST,
        )
    )

    issues = current_issues(episode, MdrkKind.FINAL)

    assert not any(issue.code.startswith("icf_") for issue in issues)


def test_diagnosis_is_validated_for_selected_snapshot_only() -> None:
    episode = _valid_episode()
    episode.initial_sections.clinical_diagnosis = ""

    assert not can_generate(episode, MdrkKind.INITIAL)
    assert can_generate(episode, MdrkKind.FINAL)


def test_meeting_before_admission_is_blocking() -> None:
    episode = _valid_episode()
    episode.initial_meeting_at = datetime(2026, 6, 5, 11)

    issues = current_issues(episode, MdrkKind.INITIAL)

    assert any(
        issue.code == "meeting_before_admission"
        and issue.severity is ReviewSeverity.BLOCKING
        for issue in issues
    )


def test_discharge_before_manual_admission_is_blocking() -> None:
    episode = _valid_episode()
    episode.admission_datetime = datetime(2026, 8, 20, 9)
    episode.discharge_datetime = datetime(2026, 8, 19, 12)
    episode.initial_meeting_at = datetime(2026, 8, 20, 10)

    issues = current_issues(episode, MdrkKind.INITIAL)

    assert any(
        issue.code == "discharge_before_admission"
        and issue.severity is ReviewSeverity.BLOCKING
        for issue in issues
    )
    assert not can_generate(episode, MdrkKind.INITIAL)


def test_meeting_after_discharge_is_blocking() -> None:
    episode = _valid_episode()
    episode.discharge_datetime = datetime(2026, 6, 20, 10)
    episode.initial_meeting_at = datetime(2026, 6, 20, 10, 1)

    issues = current_issues(episode, MdrkKind.INITIAL)

    assert any(
        issue.code == "meeting_after_discharge"
        and issue.severity is ReviewSeverity.BLOCKING
        for issue in issues
    )


def test_meeting_at_discharge_time_is_allowed() -> None:
    episode = _valid_episode()
    episode.discharge_datetime = datetime(2026, 6, 20, 10)
    episode.initial_meeting_at = episode.discharge_datetime

    issues = current_issues(episode, MdrkKind.INITIAL)

    assert not any(issue.code == "meeting_after_discharge" for issue in issues)


def test_final_meeting_must_be_after_initial_meeting() -> None:
    episode = _valid_episode()
    episode.final_meeting_at = episode.initial_meeting_at

    issues = current_issues(episode, MdrkKind.FINAL)

    assert any(
        issue.code == "final_meeting_not_after_initial"
        and issue.severity is ReviewSeverity.BLOCKING
        for issue in issues
    )


def test_participant_source_without_finding_warns_but_absent_role_does_not() -> None:
    episode = _valid_episode()
    episode.sources.append(
        SourceDocument(
            Path("/ft.docx"),
            role=SpecialistRole.PHYSICAL_THERAPIST,
            clinical_datetime=datetime(2026, 6, 5, 14, 30),
        )
    )
    episode.findings.append(
        SpecialistFinding(
            SpecialistRole.NEUROLOGIST,
            conclusion="Показан курс медицинской реабилитации",
            source_datetime=datetime(2026, 6, 5, 13),
        )
    )

    issues = current_issues(episode, MdrkKind.INITIAL)

    participant_warnings = [issue for issue in issues if issue.code == "participant_finding_missing"]
    assert [issue.field for issue in participant_warnings] == ["findings.physical_therapist"]
    assert not any("логопед" in issue.message.casefold() for issue in participant_warnings)


def test_newer_unextracted_participant_source_warns_with_latest_path() -> None:
    episode = _valid_episode()
    role = SpecialistRole.LOGOPEDIST
    old_source = Path("/logopedist-old.docx")
    latest_source = Path("/logopedist-latest.docx")
    episode.sources.extend(
        (
            SourceDocument(
                old_source,
                role=role,
                clinical_datetime=datetime(2026, 6, 5, 14),
            ),
            SourceDocument(
                latest_source,
                role=role,
                clinical_datetime=datetime(2026, 6, 19, 14),
            ),
        )
    )
    episode.findings.append(
        SpecialistFinding(
            role=role,
            conclusion="Старое заключение",
            source_datetime=datetime(2026, 6, 5, 14),
            source=old_source,
        )
    )

    issues = current_issues(episode, MdrkKind.FINAL)

    warning = next(
        issue
        for issue in issues
        if issue.code == "participant_latest_source_not_extracted"
    )
    assert warning.field == "findings.logopedist"
    assert warning.source == latest_source
    assert "более ранние данные" in warning.message


def test_scale_only_participant_warns_about_missing_conclusion() -> None:
    episode = _valid_episode()
    episode.sources.append(
        SourceDocument(
            Path("/ft.docx"),
            role=SpecialistRole.PHYSICAL_THERAPIST,
            clinical_datetime=datetime(2026, 6, 5, 14, 30),
        )
    )
    episode.findings.extend(
        (
            SpecialistFinding(
                SpecialistRole.NEUROLOGIST,
                conclusion="Показан курс медицинской реабилитации",
                source_datetime=datetime(2026, 6, 5, 13),
            ),
            SpecialistFinding(
                SpecialistRole.PHYSICAL_THERAPIST,
                source=Path("/ft.docx"),
                source_datetime=datetime(2026, 6, 5, 14, 30),
                scales=[
                    ScaleMeasurement(
                        "Шкала баланса Берга",
                        "44",
                        datetime(2026, 6, 5, 14, 30),
                        SpecialistRole.PHYSICAL_THERAPIST,
                        Path("/ft.docx"),
                    )
                ],
            ),
        )
    )

    issues = current_issues(episode, MdrkKind.INITIAL)

    assert any(
        issue.code == "participant_conclusion_missing"
        and issue.field == "findings.physical_therapist.conclusion"
        for issue in issues
    )


def test_final_only_scale_value_produces_visible_initial_warning() -> None:
    episode = _valid_episode()
    role = SpecialistRole.LOGOPEDIST
    source = Path("/logopedist-final.docx")
    episode.sources.append(
        SourceDocument(
            source,
            role=role,
            clinical_datetime=datetime(2026, 6, 19, 8, 30),
        )
    )
    episode.findings.append(
        SpecialistFinding(
            role=role,
            conclusion="Глотание не нарушено",
            source=source,
            source_datetime=datetime(2026, 6, 19, 8, 30),
            scales=[
                ScaleMeasurement(
                    "Оценка MASA",
                    "патологии не выявлено",
                    datetime(2026, 6, 19, 8, 30),
                    role,
                    source,
                )
            ],
        )
    )

    issues = current_issues(episode, MdrkKind.FINAL)

    assert any(
        issue.code == "scale_initial_missing" and "MASA" in issue.message
        for issue in issues
    )


def test_single_baseline_scale_produces_visible_final_warning() -> None:
    episode = _valid_episode()
    role = SpecialistRole.NEUROPSYCHOLOGIST
    episode.findings.append(
        SpecialistFinding(
            role=role,
            source_datetime=datetime(2026, 6, 5, 15),
            scales=[
                ScaleMeasurement(
                    "MoCA",
                    "24",
                    datetime(2026, 6, 5, 15),
                    role,
                )
            ],
        )
    )

    issues = current_issues(episode, MdrkKind.FINAL)

    assert any(
        issue.code == "scale_final_missing" and "MoCA" in issue.message
        for issue in issues
    )


def test_follow_up_only_icf_domain_is_hidden_from_initial_review() -> None:
    episode = _valid_episode()
    episode.icf_domains.append(
        IcfDomain(
            code="b999",
            description="Новый домен",
            specialist=SpecialistRole.PHYSICAL_THERAPIST,
            final=IcfQualifier(1),
            final_source=Path("/ft-follow-up.docx"),
        )
    )

    initial_issues = current_issues(episode, MdrkKind.INITIAL)
    final_issues = current_issues(episode, MdrkKind.FINAL)

    assert not any(issue.field.startswith("icf.") for issue in initial_issues)
    assert any(
        issue.code == "icf_initial_missing" and "b999" in issue.message
        for issue in final_issues
    )
