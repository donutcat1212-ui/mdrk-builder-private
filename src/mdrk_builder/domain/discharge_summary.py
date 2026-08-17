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


@dataclass(frozen=True, slots=True)
class DischargeScaleRow:
    role: SpecialistRole
    name: str
    value: str = ""


@dataclass(slots=True)
class DischargeSummaryDraft:
    """Editable discharge-summary projection with explicit source ownership."""

    folder: Path
    identity: PatientIdentity = field(default_factory=PatientIdentity)
    admission_datetime: datetime | None = None
    discharge_datetime: datetime | None = None
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

    field_sources: dict[str, Path] = field(default_factory=dict)
    issues: list[ReviewIssue] = field(default_factory=list)

    def blocking_issues(self) -> tuple[ReviewIssue, ...]:
        return tuple(
            issue
            for issue in self.issues
            if issue.severity is ReviewSeverity.BLOCKING
        )

    def immutable_sources(self) -> set[Path]:
        paths = {source.resolve() for source in self.source_paths}
        paths.update(source.resolve() for source in self.field_sources.values())
        for source in (
            self.discharge_source,
            self.primary_neurologist_source,
            self.final_mdrk_source,
        ):
            if source is not None:
                paths.add(source.resolve())
        return paths
