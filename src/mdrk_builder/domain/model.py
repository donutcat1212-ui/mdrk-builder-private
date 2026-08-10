from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from enum import StrEnum
from pathlib import Path


class MdrkKind(StrEnum):
    INITIAL = "initial"
    FINAL = "final"


class SpecialistRole(StrEnum):
    FRM = "frm"
    NEUROLOGIST = "neurologist"
    PHYSICAL_THERAPIST = "physical_therapist"
    OCCUPATIONAL_THERAPIST = "occupational_therapist"
    LOGOPEDIST = "logopedist"
    NEUROPSYCHOLOGIST = "neuropsychologist"
    PATHOPSYCHOLOGIST = "pathopsychologist"
    OTHER = "other"

    @property
    def display_name(self) -> str:
        return {
            self.FRM: "Врач ФРМ",
            self.NEUROLOGIST: "Невролог",
            self.PHYSICAL_THERAPIST: "Специалист по физической реабилитации",
            self.OCCUPATIONAL_THERAPIST: "Специалист по эргореабилитации",
            self.LOGOPEDIST: "Медицинский логопед",
            self.NEUROPSYCHOLOGIST: "Медицинский психолог/нейропсихолог",
            self.PATHOPSYCHOLOGIST: "Медицинский психолог/патопсихолог",
            self.OTHER: "Другой специалист",
        }[self]


class ReviewSeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    BLOCKING = "blocking"


@dataclass(frozen=True, slots=True)
class SourceDocument:
    path: Path
    role: SpecialistRole = SpecialistRole.OTHER
    clinical_datetime: datetime | None = None
    document_type: str = "unknown"
    extraction_method: str = "docx"
    sha256: str = ""


@dataclass(slots=True)
class PatientIdentity:
    full_name: str = ""
    birth_date: date | None = None
    sex: str = ""
    medical_record_number: str = ""


@dataclass(slots=True)
class ClinicalSections:
    clinical_diagnosis: str = ""
    disease_history: str = ""
    life_history: str = ""
    laboratory_results: str = ""
    instrumental_results: str = ""
    rehabilitation_potential: str = ""
    limitations: str = ""
    risks: str = ""
    movement_regimen: str = ""
    diet: str = ""
    medication: str = ""
    goal: str = ""
    tasks: str = ""


@dataclass(frozen=True, slots=True)
class IcfQualifier:
    value: int
    facilitator: bool = False

    def __post_init__(self) -> None:
        if not 0 <= self.value <= 4:
            raise ValueError("ICF qualifier must be between 0 and 4")

    def display(self) -> str:
        return f"{self.value}{'+' if self.facilitator else ''}"


@dataclass(slots=True)
class IcfDomain:
    code: str
    description: str
    specialist: SpecialistRole
    initial: IcfQualifier | None = None
    final: IcfQualifier | None = None
    note: str = ""
    initial_source: Path | None = None
    final_source: Path | None = None

    @property
    def key(self) -> tuple[str, str, SpecialistRole]:
        normalized_description = " ".join(self.description.casefold().split())
        return (self.code.casefold().replace(" ", ""), normalized_description, self.specialist)

    @property
    def dynamic_marker(self) -> str | None:
        if self.initial is None or self.final is None:
            return None
        if self.final.value < self.initial.value:
            return "+"
        if self.final.value > self.initial.value:
            return "-"
        return ""


@dataclass(slots=True)
class ScaleMeasurement:
    name: str
    value: str
    measured_at: datetime | None
    specialist: SpecialistRole
    source: Path | None = None


@dataclass(slots=True)
class SpecialistFinding:
    role: SpecialistRole
    conclusion: str = ""
    source_datetime: datetime | None = None
    source: Path | None = None
    scales: list[ScaleMeasurement] = field(default_factory=list)


@dataclass(slots=True)
class Procedure:
    name: str
    specialist: str
    actual_count: int | None
    duration_minutes: int | None = None
    frequency: str = ""
    code: str = ""
    planned_count: int | None = None
    source: Path | None = None
    count_needs_review: bool = False
    performed_dates: tuple[date, ...] = ()


@dataclass(slots=True)
class ReviewIssue:
    code: str
    message: str
    severity: ReviewSeverity = ReviewSeverity.WARNING
    field: str = ""
    source: Path | None = None
    acknowledged: bool = False
    acknowledgement_key: str = ""


@dataclass(slots=True)
class Episode:
    folder: Path
    identity: PatientIdentity = field(default_factory=PatientIdentity)
    admission_datetime: datetime | None = None
    discharge_datetime: datetime | None = None
    department: str = "Отделение медицинской реабилитации для пациентов с нарушением функции ЦНС №2"
    stage: str = "2 этап"
    course_duration_days: int | None = None
    initial_sections: ClinicalSections = field(default_factory=ClinicalSections)
    sections: ClinicalSections = field(default_factory=ClinicalSections)
    sources: list[SourceDocument] = field(default_factory=list)
    findings: list[SpecialistFinding] = field(default_factory=list)
    icf_domains: list[IcfDomain] = field(default_factory=list)
    procedures: list[Procedure] = field(default_factory=list)
    issues: list[ReviewIssue] = field(default_factory=list)
    acknowledged_issues: set[str] = field(default_factory=set)
    # Kept for backward compatibility with saved UI state from versions that
    # allowed acknowledging only two source conflicts.
    acknowledged_conflicts: dict[str, str] = field(default_factory=dict)
    excluded_source_paths: set[Path] = field(default_factory=set)
    materialized_medical_record_number: str = ""
    materialized_admission_datetime: datetime | None = None
    initial_field_sources: dict[str, Path] = field(default_factory=dict)
    field_sources: dict[str, Path] = field(default_factory=dict)
    initial_meeting_at: datetime | None = None
    final_meeting_at: datetime | None = None

    def meeting_at(self, kind: MdrkKind) -> datetime | None:
        return self.initial_meeting_at if kind is MdrkKind.INITIAL else self.final_meeting_at

    def has_blocking_issues(self) -> bool:
        return any(issue.severity is ReviewSeverity.BLOCKING for issue in self.issues)

    def participating_roles(self) -> set[SpecialistRole]:
        return {finding.role for finding in self.findings if finding.conclusion or finding.scales}

    def source_is_active(self, source: SourceDocument | Path) -> bool:
        path = source.path if isinstance(source, SourceDocument) else source
        return path not in self.excluded_source_paths
