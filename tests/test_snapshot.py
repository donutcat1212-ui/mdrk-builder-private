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
        source=Path(f"/{conclusion}.docx"),
    )


def test_snapshot_selects_latest_finding_not_after_meeting() -> None:
    episode = Episode(folder=Path("/episode"))
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


def test_final_snapshot_uses_fixed_goal_and_tasks() -> None:
    episode = Episode(folder=Path("/episode"))
    episode.sections.goal = "Исходная цель"
    episode.sections.tasks = "Исходные задачи"

    snapshot = build_snapshot(episode, MdrkKind.FINAL)

    assert snapshot.goal == FINAL_GOAL
    assert snapshot.tasks == FINAL_TASKS


def test_snapshot_selects_clinical_sections_for_kind() -> None:
    episode = Episode(folder=Path("/episode"))
    episode.initial_sections.clinical_diagnosis = "исходный диагноз"
    episode.initial_sections.goal = "исходная цель"
    episode.sections.clinical_diagnosis = "итоговый диагноз"
    episode.sections.goal = "поздняя формулировка цели"

    initial = build_snapshot(episode, MdrkKind.INITIAL)
    final = build_snapshot(episode, MdrkKind.FINAL)

    assert initial.sections is episode.initial_sections
    assert initial.sections.clinical_diagnosis == "исходный диагноз"
    assert initial.goal == "исходная цель"
    assert final.sections is episode.sections
    assert final.sections.clinical_diagnosis == "итоговый диагноз"
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
