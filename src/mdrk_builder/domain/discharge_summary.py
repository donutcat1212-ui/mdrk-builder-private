from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .model import (
    IcfDomain,
    PatientIdentity,
    Procedure,
    ReviewIssue,
    ReviewSeverity,
    SpecialistRole,
)


@dataclass(frozen=True, slots=True)
class DischargeTeamFinding:
    role: SpecialistRole
    conclusion: str
    source: Path | None = None
    specialist_name: str = ""
    occurred_at: datetime | None = None
    scales: tuple[DischargeScaleRow, ...] = ()
    manual_fields: set[str] = field(default_factory=set)
    origin_key: tuple[str, ...] | None = None
    specialist_title: str = ""


@dataclass(frozen=True, slots=True)
class DischargeScaleRow:
    role: SpecialistRole
    name: str
    value: str = ""
    source: Path | None = None
    initial_value: str = ""
    initial_source: Path | None = None
    initial_at: datetime | None = None
    current_at: datetime | None = None
    manual_fields: set[str] = field(default_factory=set)
    origin_key: tuple[str, ...] | None = None


@dataclass(slots=True)
class DischargeSummaryDraft:
    """Editable discharge-summary projection with explicit source ownership."""

    folder: Path
    identity: PatientIdentity = field(default_factory=PatientIdentity)
    admission_datetime: datetime | None = None
    discharge_datetime: datetime | None = None
    projection_period: tuple[datetime | None, datetime | None] | None = None
    initial_assessment_datetime: datetime | None = None
    source_paths: tuple[Path, ...] = ()
    discharge_source: Path | None = None
    primary_neurologist_source: Path | None = None
    final_mdrk_source: Path | None = None

    team_findings: tuple[DischargeTeamFinding, ...] = ()
    icf_domains: tuple[IcfDomain, ...] = ()
    completed_procedures: tuple[Procedure, ...] = ()
    admission_scale_rows: tuple[DischargeScaleRow, ...] = ()
    discharge_scale_rows: tuple[DischargeScaleRow, ...] = ()

    header_text: str = ""
    clinical_diagnosis: str = ""
    complaints: str = ""
    disease_history: str = ""
    life_history: str = ""
    provided_documents: str = ""
    physical_exam: str = ""
    neurological_status: str = ""
    local_status: str = ""

    laboratory_results: str = ""
    instrumental_results: str = ""
    other_consultations: str = ""

    medications: str = ""
    movement_regimen: str = ""
    diet: str = ""
    transfusions: str = ""
    operations: str = ""
    additional_information: str = ""

    discharge_condition: str = ""
    discharge_neurological_status: str = ""
    risks: str = ""
    limitations: str = ""
    rehabilitation_potential: str = ""
    goal_result: str = ""
    work_capacity: str = ""
    radiation_exposure: str = ""
    recommendations: str = ""
    signatures: str = ""

    conflict_choices: dict[str, list[tuple[str, Path | None]]] = field(default_factory=dict)
    manual_fields: set[str] = field(default_factory=set)
    field_sources: dict[str, Path] = field(default_factory=dict)
    issues: list[ReviewIssue] = field(default_factory=list)
    combine_admission_statuses: bool = False

    def requires_period_rescan(self) -> bool:
        return self.projection_period is not None and self.projection_period != (
            self.admission_datetime, self.discharge_datetime
        )

    def blocking_issues(self) -> tuple[ReviewIssue, ...]:
        return tuple(
            issue
            for issue in self.issues
            if issue.severity is ReviewSeverity.BLOCKING
        )

    def immutable_sources(self) -> set[Path]:
        paths = {source.resolve() for source in self.source_paths}
        paths.update(source.resolve() for source in self.field_sources.values())
        paths.update(
            row.source.resolve()
            for row in (*self.admission_scale_rows, *self.discharge_scale_rows)
            if row.source is not None
        )
        paths.update(path.resolve() for finding in self.team_findings for scale in finding.scales
                     for path in (scale.initial_source, scale.source) if path is not None)
        paths.update(row.source.resolve() for row in (*self.team_findings, *self.completed_procedures)
                     if row.source is not None)
        paths.update(source.resolve() for row in self.icf_domains
                     for source in (row.source, row.initial_source, row.final_source) if source is not None)
        for source in (
            self.discharge_source,
            self.primary_neurologist_source,
            self.final_mdrk_source,
        ):
            if source is not None:
                paths.add(source.resolve())
        return paths
