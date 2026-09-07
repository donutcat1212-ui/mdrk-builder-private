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


class IcfSection(StrEnum):
    BODY_FUNCTIONS = "body_functions"
    BODY_STRUCTURES = "body_structures"
    ACTIVITIES_PARTICIPATION = "activities_participation"
    ENVIRONMENTAL_FACTORS = "environmental_factors"
    PERSONAL_FACTORS = "personal_factors"

    @property
    def display_name(self) -> str:
        return {
            self.BODY_FUNCTIONS: "Функции организма",
            self.BODY_STRUCTURES: "Структуры организма",
            self.ACTIVITIES_PARTICIPATION: "Активность и участие",
            self.ENVIRONMENTAL_FACTORS: "Факторы окружающей среды",
            self.PERSONAL_FACTORS: "Персональные факторы",
        }[self]


def infer_icf_section(code: str) -> IcfSection:
    normalized = "".join(code.casefold().replace("ё", "е").split()).replace("е", "e")
    if normalized.startswith("pf"):
        return IcfSection.PERSONAL_FACTORS
    if normalized.startswith("b"):
        return IcfSection.BODY_FUNCTIONS
    if normalized.startswith("s"):
        return IcfSection.BODY_STRUCTURES
    if normalized.startswith("d"):
        return IcfSection.ACTIVITIES_PARTICIPATION
    if normalized.startswith("e"):
        return IcfSection.ENVIRONMENTAL_FACTORS
    return IcfSection.PERSONAL_FACTORS


@dataclass(frozen=True, slots=True)
class SourceDocument:
    path: Path
    role: SpecialistRole = SpecialistRole.OTHER
    clinical_datetime: datetime | None = None
    document_type: str = "unknown"
    extraction_method: str = "docx"
    sha256: str = ""
    specialist_name: str = ""


@dataclass(slots=True)
class PatientIdentity:
    full_name: str = ""
    birth_date: date | None = None
    sex: str = ""
    medical_record_number: str = ""


@dataclass(slots=True)
class ReverseSheetRow:
    intervention: str
    appointment_date: date | None = None
    performed_at: datetime | None = None
    performer: str = ""
    source: Path | None = None
    manual_fields: set[str] = field(default_factory=set)
    field_sources: dict[str, Path] = field(default_factory=dict)



@dataclass(slots=True)
class ReverseSheetDraft:
    folder: Path
    identity: PatientIdentity = field(default_factory=PatientIdentity)
    admission_datetime: datetime | None = None
    discharge_datetime: datetime | None = None
    header_source: Path | None = None
    field_sources: dict[str, Path] = field(default_factory=dict)
    source_paths: tuple[Path, ...] = ()
    manual_fields: set[str] = field(default_factory=set)
    rows: list[ReverseSheetRow] = field(default_factory=list)
    issues: list[ReviewIssue] = field(default_factory=list)


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
    initial_measured_at: datetime | None = None
    final_measured_at: datetime | None = None
    section_override: IcfSection | None = None
    source: Path | None = None
    origin_note: str = ""
    conflict_choices: dict[str, list[tuple[str, Path | None]]] = field(default_factory=dict)

    manual_fields: set[str] = field(default_factory=set)

    @property
    def section(self) -> IcfSection:
        return self.section_override or infer_icf_section(self.code)

    @property
    def key(self) -> tuple[str, str, SpecialistRole]:
        normalized_description = " ".join(self.description.casefold().split())
        return (self.code.casefold().replace(" ", ""), normalized_description, self.specialist)

    @property
    def dynamic_marker(self) -> str | None:
        if self.initial is None or self.final is None:
            return None
        initial = -self.initial.value if self.initial.facilitator else self.initial.value
        final = -self.final.value if self.final.facilitator else self.final.value
        if final < initial:
            return "+"
        if final > initial:
            return "-"
        return ""


def move_icf_domain(
    domains: list[IcfDomain],
    source_index: int,
    section: IcfSection,
    *,
    before_index: int | None = None,
) -> int:
    """Move one domain into a section and return its new list index."""
    if not 0 <= source_index < len(domains):
        raise IndexError("ICF source index is outside the domain list")
    if before_index is not None and not 0 <= before_index < len(domains):
        raise IndexError("ICF target index is outside the domain list")

    domain = domains.pop(source_index)
    domain.section_override = section
    if before_index is None:
        insert_at = len(domains)
        for index, item in enumerate(domains):
            if item.section is section:
                insert_at = index + 1
    else:
        insert_at = before_index - (1 if source_index < before_index else 0)
    domains.insert(max(0, insert_at), domain)
    return domains.index(domain)


@dataclass(slots=True)
class ScaleMeasurement:
    name: str
    value: str
    measured_at: datetime | None
    specialist: SpecialistRole
    source: Path | None = None
    manual_fields: set[str] = field(default_factory=set)



@dataclass(slots=True)
class SpecialistFinding:
    role: SpecialistRole
    conclusion: str = ""
    source_datetime: datetime | None = None
    source: Path | None = None
    scales: list[ScaleMeasurement] = field(default_factory=list)
    manual_fields: set[str] = field(default_factory=set)



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
    source_paths: tuple[Path, ...] = ()
    planned_frequency: str = ""
    manual_fields: set[str] = field(default_factory=set)



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
    course_duration_manual: bool = False
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
