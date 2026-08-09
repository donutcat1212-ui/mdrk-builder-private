from datetime import datetime
from pathlib import Path

from mdrk_builder.application.validation import can_generate, current_issues
from mdrk_builder.domain import (
    Episode,
    IcfDomain,
    IcfQualifier,
    MdrkKind,
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
