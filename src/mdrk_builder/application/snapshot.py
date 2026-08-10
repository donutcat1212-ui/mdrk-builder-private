from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from mdrk_builder.domain import (
    ClinicalSections,
    Episode,
    IcfDomain,
    MdrkKind,
    ScaleMeasurement,
    SpecialistFinding,
    SpecialistRole,
)


FINAL_GOAL = "Достигнута в полном объёме"
FINAL_TASKS = "Выполнены в полном объёме"


@dataclass(frozen=True, slots=True)
class ScaleRow:
    role: SpecialistRole
    name: str
    initial: ScaleMeasurement | None
    current: ScaleMeasurement | None


@dataclass(frozen=True, slots=True)
class Snapshot:
    kind: MdrkKind
    meeting_at: datetime | None
    sections: ClinicalSections
    findings: tuple[SpecialistFinding, ...]
    scale_rows: tuple[ScaleRow, ...]
    icf_domains: tuple[IcfDomain, ...]
    goal: str
    tasks: str


def _dated_not_after(value: datetime | None, boundary: datetime | None) -> bool:
    if value is None:
        return boundary is None
    return boundary is None or value <= boundary


def _latest_finding(
    findings: list[SpecialistFinding], boundary: datetime | None
) -> SpecialistFinding | None:
    eligible = [item for item in findings if _dated_not_after(item.source_datetime, boundary)]
    if not eligible:
        # An undated source is still shown when it is the only source for the role;
        # the scanner emits a review issue for the missing clinical date.
        undated = [item for item in findings if item.source_datetime is None]
        return undated[-1] if undated else None
    return max(eligible, key=lambda item: item.source_datetime or datetime.min)


def select_findings(episode: Episode, boundary: datetime | None) -> tuple[SpecialistFinding, ...]:
    by_role: dict[SpecialistRole, list[SpecialistFinding]] = {}
    for finding in episode.findings:
        by_role.setdefault(finding.role, []).append(finding)
    selected = [
        item
        for role in SpecialistRole
        if (item := _latest_finding(by_role.get(role, []), boundary)) is not None
    ]
    return tuple(selected)


def _latest_measurement(
    values: list[ScaleMeasurement], boundary: datetime | None
) -> ScaleMeasurement | None:
    eligible = [item for item in values if _dated_not_after(item.measured_at, boundary)]
    if not eligible:
        undated = [item for item in values if item.measured_at is None]
        return undated[-1] if undated else None
    return max(eligible, key=lambda item: item.measured_at or datetime.min)


def _latest_distinct_measurement(
    values: list[ScaleMeasurement],
    baseline: ScaleMeasurement | None,
    boundary: datetime | None,
) -> ScaleMeasurement | None:
    """Return a real repeat measurement, never the baseline carried forward."""

    eligible = [item for item in values if _dated_not_after(item.measured_at, boundary)]
    if baseline is None:
        return _latest_measurement(values, boundary)
    if baseline.measured_at is not None:
        repeats = [
            item
            for item in eligible
            if item.measured_at is not None and item.measured_at > baseline.measured_at
        ]
    else:
        repeats = [item for item in eligible if item is not baseline]
    if not repeats:
        return None
    return max(repeats, key=lambda item: item.measured_at or datetime.min)


def select_scale_rows(episode: Episode, kind: MdrkKind) -> tuple[ScaleRow, ...]:
    observations: dict[tuple[SpecialistRole, str], list[ScaleMeasurement]] = {}
    boundary = episode.meeting_at(kind)
    by_role: dict[SpecialistRole, list[SpecialistFinding]] = {}
    for finding in episode.findings:
        by_role.setdefault(finding.role, []).append(finding)
    eligible_findings: list[SpecialistFinding] = []
    for values in by_role.values():
        dated = [
            finding
            for finding in values
            if finding.source_datetime is not None
            and (boundary is None or finding.source_datetime <= boundary)
        ]
        eligible_findings.extend(dated or [finding for finding in values if finding.source_datetime is None])

    for finding in eligible_findings:
        for measurement in finding.scales:
            if not _dated_not_after(measurement.measured_at, boundary):
                continue
            key = (measurement.specialist, " ".join(measurement.name.casefold().split()))
            observations.setdefault(key, []).append(measurement)

    rows: list[ScaleRow] = []
    for (role, _normalized_name), values in observations.items():
        initial = _latest_measurement(values, episode.initial_meeting_at)
        current = (
            _latest_distinct_measurement(
                values,
                initial,
                episode.final_meeting_at,
            )
            if kind is MdrkKind.FINAL
            else None
        )
        sample = current or initial or values[-1]
        rows.append(ScaleRow(role, sample.name, initial, current))
    physician_order = (
        "ривермид",
        "рэнкин",
        "nrs 2002",
        "скф",
        "реабилитационной маршрутизации",
        "бартел",
    )

    def scale_sort_key(item: ScaleRow) -> tuple[str, int, str]:
        normalized = item.name.casefold()
        priority = next(
            (index for index, token in enumerate(physician_order) if token in normalized),
            len(physician_order),
        )
        if item.role not in {SpecialistRole.FRM, SpecialistRole.NEUROLOGIST}:
            priority = 0
        return item.role.value, priority, normalized

    rows.sort(key=scale_sort_key)
    return tuple(rows)


def build_snapshot(episode: Episode, kind: MdrkKind) -> Snapshot:
    boundary = episode.meeting_at(kind)
    findings = select_findings(episode, boundary)
    sections = episode.initial_sections if kind is MdrkKind.INITIAL else episode.sections
    goal = FINAL_GOAL if kind is MdrkKind.FINAL else sections.goal
    tasks = FINAL_TASKS if kind is MdrkKind.FINAL else sections.tasks
    icf_domains = (
        tuple(
            domain
            for domain in episode.icf_domains
            if domain.initial is not None or domain.initial_source is not None
        )
        if kind is MdrkKind.INITIAL
        else tuple(episode.icf_domains)
    )
    return Snapshot(
        kind=kind,
        meeting_at=boundary,
        sections=sections,
        findings=findings,
        scale_rows=select_scale_rows(episode, kind),
        icf_domains=icf_domains,
        goal=goal,
        tasks=tasks,
    )
