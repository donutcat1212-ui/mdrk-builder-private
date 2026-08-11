from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from difflib import SequenceMatcher
import re

from mdrk_builder.application.clinical_text import is_empty_clinical_update
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
SCALE_NAME_DUPLICATE_THRESHOLD = 0.94


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
    # Conclusions are rendered independently from the scale history.  A later
    # diary that contains only a copied scale table must not erase the latest
    # actual conclusion for the role.
    with_conclusion = [
        item
        for item in eligible
        if item.conclusion.strip()
        and not is_empty_clinical_update(item.conclusion)
    ]
    candidates = with_conclusion or eligible
    return max(candidates, key=lambda item: item.source_datetime or datetime.min)


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


def _normalized_scale_name(value: str) -> str:
    normalized = " ".join(value.casefold().replace("ё", "е").split())
    normalized = re.sub(r"[^0-9a-zа-я]+", " ", normalized)
    normalized = " ".join(normalized.split())
    if "ренкин" in normalized:
        return "модифицированная шкала ренкина"
    if "берг" in normalized:
        return "шкала баланса берга"
    if "бартел" in normalized:
        return "индекс бартел"
    if "ривермид" in normalized:
        return "индекс мобильности ривермид"
    if "тинетт" in normalized:
        return "шкала тинетти"
    if "moca" in normalized:
        return "moca"
    return normalized


def _normalized_scale_value(value: str) -> str:
    return " ".join(value.casefold().replace("ё", "е").split())


def _scale_name_tokens_match(left: str, right: str) -> bool:
    left_tokens = left.split()
    right_tokens = right.split()
    if not left_tokens or not right_tokens:
        return False

    def equivalent(left_token: str, right_token: str) -> bool:
        return left_token == right_token or (
            min(len(left_token), len(right_token)) >= 4
            and (
                left_token.startswith(right_token)
                or right_token.startswith(left_token)
            )
        )

    matched = sum(
        any(equivalent(left_token, right_token) for right_token in right_tokens)
        for left_token in left_tokens
    )
    return matched / max(len(left_tokens), len(right_tokens)) >= 0.75


def _matching_scale_key(
    role: SpecialistRole,
    normalized_name: str,
    existing: dict[tuple[SpecialistRole, str], list[_ScaleCandidate]],
) -> tuple[SpecialistRole, str]:
    exact = (role, normalized_name)
    if exact in existing:
        return exact
    for candidate_role, candidate_name in existing:
        if candidate_role is not role:
            continue
        if not _scale_name_tokens_match(candidate_name, normalized_name):
            continue
        if (
            SequenceMatcher(None, candidate_name, normalized_name).ratio()
            >= SCALE_NAME_DUPLICATE_THRESHOLD
        ):
            return (candidate_role, candidate_name)
    return exact


@dataclass(frozen=True, slots=True)
class _ScaleCandidate:
    measurement: ScaleMeasurement
    source_datetime: datetime | None

    @property
    def effective_datetime(self) -> datetime | None:
        return self.measurement.measured_at or self.source_datetime


def _prefer_scale_candidate(values: list[_ScaleCandidate]) -> _ScaleCandidate:
    """Resolve exact copied facts while retaining deterministic provenance."""

    distinct_values = {
        _normalized_scale_value(item.measurement.value) for item in values
    }
    if len(distinct_values) == 1:
        # The same dated fact copied into later diaries belongs to the earliest
        # source that contains it, not to the last copy.
        return min(
            values,
            key=lambda item: (
                item.source_datetime or datetime.max,
                str(item.measurement.source or "").casefold(),
            ),
        )
    # Conflicting values for one assessment timestamp are treated as a later
    # correction, never as two separate temporal points.
    return max(
        values,
        key=lambda item: (
            item.source_datetime or datetime.min,
            str(item.measurement.source or "").casefold(),
        ),
    )


def _scale_points(values: list[_ScaleCandidate]) -> list[ScaleMeasurement]:
    dated = [item for item in values if item.effective_datetime is not None]
    eligible = dated or values
    by_datetime: dict[datetime | None, list[_ScaleCandidate]] = {}
    for item in eligible:
        by_datetime.setdefault(item.effective_datetime, []).append(item)
    selected = [_prefer_scale_candidate(items) for items in by_datetime.values()]
    selected.sort(key=lambda item: item.effective_datetime or datetime.min)
    return [item.measurement for item in selected]


def select_scale_rows(episode: Episode, kind: MdrkKind) -> tuple[ScaleRow, ...]:
    observations: dict[tuple[SpecialistRole, str], list[_ScaleCandidate]] = {}
    boundary = episode.meeting_at(kind)
    for finding in episode.findings:
        # Do not leak measurements copied retrospectively into a document that
        # itself was written after the selected MDRK meeting.
        if (
            finding.source_datetime is not None
            and boundary is not None
            and finding.source_datetime > boundary
        ):
            continue
        for measurement in finding.scales:
            normalized_measurement = (
                measurement
                if measurement.measured_at is not None
                else replace(measurement, measured_at=finding.source_datetime)
            )
            measurement_at = normalized_measurement.measured_at
            if (
                measurement_at is not None
                and boundary is not None
                and measurement_at > boundary
            ):
                continue
            key = _matching_scale_key(
                normalized_measurement.specialist,
                _normalized_scale_name(normalized_measurement.name),
                observations,
            )
            observations.setdefault(key, []).append(
                _ScaleCandidate(normalized_measurement, finding.source_datetime)
            )

    rows: list[ScaleRow] = []
    for (role, _normalized_name), candidates in observations.items():
        points = _scale_points(candidates)
        if not points:
            continue
        initial = points[0]
        current = points[-1] if kind is MdrkKind.FINAL and len(points) > 1 else None
        sample = current or initial
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


def _icf_source_datetime(episode: Episode, source_path) -> datetime | None:
    if source_path is None:
        return None
    return next(
        (
            source.clinical_datetime
            for source in episode.sources
            if source.path == source_path and episode.source_is_active(source)
        ),
        None,
    )


def _icf_initial_datetime(episode: Episode, domain: IcfDomain) -> datetime | None:
    return domain.initial_measured_at or _icf_source_datetime(
        episode, domain.initial_source
    )


def _icf_final_datetime(episode: Episode, domain: IcfDomain) -> datetime | None:
    return domain.final_measured_at or _icf_source_datetime(
        episode, domain.final_source
    )


def select_icf_domains(episode: Episode, kind: MdrkKind) -> tuple[IcfDomain, ...]:
    boundary = episode.meeting_at(kind)
    selected: list[IcfDomain] = []
    for domain in episode.icf_domains:
        initial_present = domain.initial is not None or domain.initial_source is not None
        final_present = domain.final is not None or domain.final_source is not None
        initial_at = _icf_initial_datetime(episode, domain)
        final_at = _icf_final_datetime(episode, domain)

        if kind is MdrkKind.INITIAL:
            if not initial_present or (
                initial_at is not None
                and boundary is not None
                and initial_at > boundary
            ):
                continue
            # MDRK-1 uses the same table geometry, but carries no repeat point or
            # dynamics in its data snapshot.
            selected.append(
                replace(
                    domain,
                    final=None,
                    final_source=None,
                    final_measured_at=None,
                )
            )
            continue

        if not initial_present and not final_present:
            # Descriptive Pf rows have source provenance but no numeric point.
            if domain.code.strip().casefold().startswith("pf"):
                selected.append(domain)
            continue
        if initial_at is not None and not _dated_not_after(initial_at, boundary):
            continue
        if final_at is not None and not _dated_not_after(final_at, boundary):
            selected.append(
                replace(
                    domain,
                    final=None,
                    final_source=None,
                    final_measured_at=None,
                )
            )
        else:
            selected.append(domain)
    return tuple(selected)


def build_snapshot(episode: Episode, kind: MdrkKind) -> Snapshot:
    boundary = episode.meeting_at(kind)
    findings = select_findings(episode, boundary)
    sections = episode.initial_sections if kind is MdrkKind.INITIAL else episode.sections
    goal = FINAL_GOAL if kind is MdrkKind.FINAL else sections.goal
    tasks = FINAL_TASKS if kind is MdrkKind.FINAL else sections.tasks
    icf_domains = select_icf_domains(episode, kind)
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
