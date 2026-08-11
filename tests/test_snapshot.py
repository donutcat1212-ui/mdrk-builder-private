from datetime import datetime
from pathlib import Path

from mdrk_builder.application.snapshot import FINAL_GOAL, FINAL_TASKS, build_snapshot
from mdrk_builder.domain import (
    Episode,
    IcfDomain,
    IcfQualifier,
    MdrkKind,
    ScaleMeasurement,
    SpecialistFinding,
    SpecialistRole,
)


def _finding(role: SpecialistRole, when: datetime, conclusion: str) -> SpecialistFinding:
    return SpecialistFinding(
        role=role,
        conclusion=conclusion,
        source_datetime=when,
        source=Path(f"fixtures/{conclusion}.docx"),
    )


def test_snapshot_selects_latest_finding_not_after_meeting() -> None:
    episode = Episode(folder=Path("fixtures/episode"))
    episode.initial_meeting_at = datetime(2026, 6, 6, 8)
    episode.final_meeting_at = datetime(2026, 6, 20, 11)
    episode.findings = [
        _finding(SpecialistRole.LOGOPEDIST, datetime(2026, 6, 5, 16), "initial"),
        _finding(SpecialistRole.LOGOPEDIST, datetime(2026, 6, 19, 12), "final"),
        _finding(SpecialistRole.LOGOPEDIST, datetime(2026, 6, 21, 9), "too-late"),
    ]

    initial = build_snapshot(episode, MdrkKind.INITIAL)
    final = build_snapshot(episode, MdrkKind.FINAL)

    assert initial.findings[0].conclusion == "initial"
    assert final.findings[0].conclusion == "final"


def test_latest_scale_only_diary_does_not_replace_latest_conclusion() -> None:
    episode = Episode(folder=Path("fixtures/episode"))
    episode.final_meeting_at = datetime(2026, 6, 20, 11)
    role = SpecialistRole.LOGOPEDIST
    episode.findings = [
        _finding(role, datetime(2026, 6, 18, 10), "ЗАКЛЮЧЕНИЕ_ТЕСТ"),
        SpecialistFinding(
            role=role,
            source_datetime=datetime(2026, 6, 19, 10),
            source=Path("fixtures/copied-scale-diary.docx"),
            scales=[
                ScaleMeasurement(
                    "MASA",
                    "180",
                    datetime(2026, 6, 19, 10),
                    role,
                    Path("fixtures/copied-scale-diary.docx"),
                )
            ],
        ),
    ]

    snapshot = build_snapshot(episode, MdrkKind.FINAL)

    assert snapshot.findings[0].conclusion == "ЗАКЛЮЧЕНИЕ_ТЕСТ"


def test_empty_status_diary_does_not_replace_latest_conclusion() -> None:
    episode = Episode(folder=Path("fixtures/episode"))
    episode.final_meeting_at = datetime(2026, 6, 20, 11)
    role = SpecialistRole.LOGOPEDIST
    episode.findings = [
        _finding(role, datetime(2026, 6, 18, 10), "ЗАКЛЮЧЕНИЕ_ТЕСТ"),
        _finding(role, datetime(2026, 6, 19, 10), "без изменений"),
    ]

    snapshot = build_snapshot(episode, MdrkKind.FINAL)

    assert snapshot.findings[0].conclusion == "ЗАКЛЮЧЕНИЕ_ТЕСТ"


def test_final_snapshot_uses_fixed_goal_and_tasks() -> None:
    episode = Episode(folder=Path("fixtures/episode"))
    episode.sections.goal = "ЦЕЛЬ_ТЕСТ"
    episode.sections.tasks = "ЗАДАЧИ_ТЕСТ"

    snapshot = build_snapshot(episode, MdrkKind.FINAL)

    assert snapshot.goal == FINAL_GOAL
    assert snapshot.tasks == FINAL_TASKS


def test_snapshot_selects_clinical_sections_for_kind() -> None:
    episode = Episode(folder=Path("fixtures/episode"))
    episode.initial_sections.clinical_diagnosis = "ДИАГНОЗ_ИСХОДНЫЙ"
    episode.initial_sections.goal = "ЦЕЛЬ_ИСХОДНАЯ"
    episode.sections.clinical_diagnosis = "ДИАГНОЗ_ИТОГОВЫЙ"
    episode.sections.goal = "ЦЕЛЬ_ИТОГОВАЯ"

    initial = build_snapshot(episode, MdrkKind.INITIAL)
    final = build_snapshot(episode, MdrkKind.FINAL)

    assert initial.sections is episode.initial_sections
    assert initial.sections.clinical_diagnosis == "ДИАГНОЗ_ИСХОДНЫЙ"
    assert initial.goal == "ЦЕЛЬ_ИСХОДНАЯ"
    assert final.sections is episode.sections
    assert final.sections.clinical_diagnosis == "ДИАГНОЗ_ИТОГОВЫЙ"
    assert final.goal == FINAL_GOAL


def test_scale_rows_keep_initial_and_latest_current() -> None:
    episode = Episode(folder=Path("/episode"))
    episode.initial_meeting_at = datetime(2026, 6, 6, 8)
    episode.final_meeting_at = datetime(2026, 6, 20, 11)
    role = SpecialistRole.PHYSICAL_THERAPIST
    values = [
        ScaleMeasurement("Berg", "20", datetime(2026, 6, 5, 15), role),
        ScaleMeasurement("Berg", "32", datetime(2026, 6, 19, 15), role),
        ScaleMeasurement("Berg", "40", datetime(2026, 6, 21, 15), role),
    ]
    episode.findings = [SpecialistFinding(role=role, scales=values)]

    rows = build_snapshot(episode, MdrkKind.FINAL).scale_rows

    assert len(rows) == 1
    assert rows[0].initial and rows[0].initial.value == "20"
    assert rows[0].current and rows[0].current.value == "32"


def test_final_scale_row_does_not_copy_only_baseline_into_current() -> None:
    episode = Episode(folder=Path("/episode"))
    episode.initial_meeting_at = datetime(2026, 6, 6, 8)
    episode.final_meeting_at = datetime(2026, 6, 20, 11)
    role = SpecialistRole.NEUROPSYCHOLOGIST
    baseline = ScaleMeasurement(
        "MoCA",
        "24",
        datetime(2026, 6, 5, 15),
        role,
    )
    episode.findings = [SpecialistFinding(role=role, scales=[baseline])]

    rows = build_snapshot(episode, MdrkKind.FINAL).scale_rows

    assert len(rows) == 1
    assert rows[0].initial is baseline
    assert rows[0].current is None


def test_final_scale_new_after_mdrk1_uses_first_and_last_course_points() -> None:
    episode = Episode(folder=Path("/episode"))
    episode.initial_meeting_at = datetime(2026, 6, 6, 8)
    episode.final_meeting_at = datetime(2026, 6, 20, 11)
    role = SpecialistRole.OCCUPATIONAL_THERAPIST
    first = ScaleMeasurement(
        "COPM",
        "3",
        datetime(2026, 6, 10, 9),
        role,
        Path("/ot-first.docx"),
    )
    last = ScaleMeasurement(
        "COPM",
        "6",
        datetime(2026, 6, 19, 9),
        role,
        Path("/ot-last.docx"),
    )
    episode.findings = [
        SpecialistFinding(
            role=role,
            source_datetime=datetime(2026, 6, 10, 9),
            scales=[first],
        ),
        SpecialistFinding(
            role=role,
            source_datetime=datetime(2026, 6, 19, 9),
            scales=[last],
        ),
    ]

    assert build_snapshot(episode, MdrkKind.INITIAL).scale_rows == ()
    rows = build_snapshot(episode, MdrkKind.FINAL).scale_rows
    assert len(rows) == 1
    assert rows[0].initial is first
    assert rows[0].current is last


def test_scale_history_deduplicates_same_dated_fact_copied_to_later_diary() -> None:
    episode = Episode(folder=Path("/episode"))
    episode.initial_meeting_at = datetime(2026, 6, 6, 8)
    episode.final_meeting_at = datetime(2026, 6, 20, 11)
    role = SpecialistRole.NEUROPSYCHOLOGIST
    original_path = Path("/neuropsych-primary.docx")
    copied_path = Path("/neuropsych-diary.docx")
    measured_at = datetime(2026, 6, 5, 15)
    episode.findings = [
        SpecialistFinding(
            role=role,
            source_datetime=measured_at,
            scales=[ScaleMeasurement("MoCA", "24", measured_at, role, original_path)],
        ),
        SpecialistFinding(
            role=role,
            source_datetime=datetime(2026, 6, 18, 12),
            scales=[ScaleMeasurement("MoCA", "24", measured_at, role, copied_path)],
        ),
    ]

    row = build_snapshot(episode, MdrkKind.FINAL).scale_rows[0]

    assert row.initial and row.initial.source == original_path
    assert row.current is None


def test_scale_history_fuzzy_merges_minor_name_variant_into_one_row() -> None:
    episode = Episode(folder=Path("/episode"))
    episode.initial_meeting_at = datetime(2026, 6, 6, 8)
    episode.final_meeting_at = datetime(2026, 6, 20, 11)
    role = SpecialistRole.PHYSICAL_THERAPIST
    episode.findings = [
        SpecialistFinding(
            role=role,
            source_datetime=datetime(2026, 6, 5, 15),
            scales=[
                ScaleMeasurement(
                    "Шкала баланса Берга",
                    "20",
                    datetime(2026, 6, 5, 15),
                    role,
                )
            ],
        ),
        SpecialistFinding(
            role=role,
            source_datetime=datetime(2026, 6, 19, 15),
            scales=[
                ScaleMeasurement(
                    "Шкала баланса Берг",
                    "40",
                    datetime(2026, 6, 19, 15),
                    role,
                )
            ],
        ),
    ]

    rows = build_snapshot(episode, MdrkKind.FINAL).scale_rows

    assert len(rows) == 1
    assert rows[0].initial and rows[0].initial.value == "20"
    assert rows[0].current and rows[0].current.value == "40"


def test_scale_history_merges_common_short_and_full_name_aliases() -> None:
    episode = Episode(folder=Path("/episode"))
    episode.final_meeting_at = datetime(2026, 6, 20, 11)
    role = SpecialistRole.PHYSICAL_THERAPIST
    episode.findings = [
        SpecialistFinding(
            role=role,
            source_datetime=datetime(2026, 6, 5, 15),
            scales=[
                ScaleMeasurement(
                    "Шкала баланса Берга",
                    "20",
                    datetime(2026, 6, 5, 15),
                    role,
                )
            ],
        ),
        SpecialistFinding(
            role=role,
            source_datetime=datetime(2026, 6, 19, 15),
            scales=[
                ScaleMeasurement(
                    "Шкала Берга",
                    "40",
                    datetime(2026, 6, 19, 15),
                    role,
                )
            ],
        ),
    ]

    rows = build_snapshot(episode, MdrkKind.FINAL).scale_rows

    assert len(rows) == 1
    assert rows[0].initial and rows[0].initial.value == "20"
    assert rows[0].current and rows[0].current.value == "40"


def test_scale_rows_do_not_leak_retrospective_values_from_late_source() -> None:
    episode = Episode(folder=Path("/episode"))
    episode.initial_meeting_at = datetime(2026, 6, 6, 8)
    episode.final_meeting_at = datetime(2026, 6, 19, 15, 30)
    role = SpecialistRole.NEUROLOGIST
    late_finding = SpecialistFinding(
        role=role,
        source_datetime=datetime(2026, 6, 21, 12),
        scales=[
            ScaleMeasurement(
                "Скопированная шкала",
                "30",
                datetime(2026, 6, 5, 15, 30),
                role,
            )
        ],
    )
    episode.findings = [late_finding]

    assert build_snapshot(episode, MdrkKind.INITIAL).scale_rows == ()
    assert build_snapshot(episode, MdrkKind.FINAL).scale_rows == ()


def test_follow_up_only_icf_domain_is_absent_initial_and_present_final() -> None:
    episode = Episode(folder=Path("/episode"))
    episode.icf_domains.append(
        IcfDomain(
            code="b999",
            description="Новый домен",
            specialist=SpecialistRole.PHYSICAL_THERAPIST,
            initial=None,
            final=IcfQualifier(1),
            final_source=Path("/episode/ft-follow-up.docx"),
        )
    )

    initial = build_snapshot(episode, MdrkKind.INITIAL)
    final = build_snapshot(episode, MdrkKind.FINAL)

    assert initial.icf_domains == ()
    assert [domain.code for domain in final.icf_domains] == ["b999"]


def test_initial_icf_snapshot_strips_repeat_data_without_mutating_episode() -> None:
    episode = Episode(folder=Path("/episode"))
    episode.initial_meeting_at = datetime(2026, 6, 6, 8)
    domain = IcfDomain(
        code="d450",
        description="Ходьба",
        specialist=SpecialistRole.PHYSICAL_THERAPIST,
        initial=IcfQualifier(3),
        final=IcfQualifier(1),
        initial_source=Path("/ft-primary.docx"),
        final_source=Path("/ft-final.docx"),
        initial_measured_at=datetime(2026, 6, 5, 14),
        final_measured_at=datetime(2026, 6, 19, 14),
    )
    episode.icf_domains.append(domain)

    selected = build_snapshot(episode, MdrkKind.INITIAL).icf_domains[0]

    assert selected.initial == IcfQualifier(3)
    assert selected.final is None
    assert selected.final_source is None
    assert selected.final_measured_at is None
    assert domain.final == IcfQualifier(1)
