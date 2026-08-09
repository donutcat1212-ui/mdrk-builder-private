from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from mdrk_builder.application.snapshot import build_snapshot
from mdrk_builder.domain import ClinicalSections, Episode, IcfQualifier, MdrkKind, SpecialistRole


DATE_FORMAT = "%d.%m.%Y"
DATETIME_FORMAT = "%d.%m.%Y %H:%M"


@dataclass(frozen=True, slots=True)
class EpisodeFormData:
    full_name: str
    medical_record_number: str
    birth_date: date | None
    sex: str
    admission_datetime: datetime | None
    meeting_at: datetime | None
    department: str
    stage: str
    course_duration_days: int | None
    section_values: tuple[tuple[str, str], ...]


def format_date(value: date | None) -> str:
    return value.strftime(DATE_FORMAT) if value else ""


def format_datetime(value: datetime | None) -> str:
    return value.strftime(DATETIME_FORMAT) if value else ""


def parse_optional_date(value: str) -> date | None:
    cleaned = value.strip()
    if not cleaned:
        return None
    for pattern in (DATE_FORMAT, "%Y-%m-%d"):
        try:
            return datetime.strptime(cleaned, pattern).date()
        except ValueError:
            continue
    raise ValueError("Введите дату в формате ДД.ММ.ГГГГ")


def parse_optional_datetime(value: str) -> datetime | None:
    cleaned = value.strip()
    if not cleaned:
        return None
    for pattern in (DATETIME_FORMAT, DATE_FORMAT, "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            parsed = datetime.strptime(cleaned, pattern)
            return parsed
        except ValueError:
            continue
    raise ValueError("Введите дату и время в формате ДД.ММ.ГГГГ ЧЧ:ММ")


def parse_optional_meeting_datetime(value: str) -> datetime | None:
    cleaned = value.strip()
    if not cleaned:
        return None
    for pattern in (DATETIME_FORMAT, "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(cleaned, pattern)
        except ValueError:
            continue
    raise ValueError("Время заседания: введите ДД.ММ.ГГГГ ЧЧ:ММ")


def parse_optional_nonnegative_int(value: str, label: str) -> int | None:
    cleaned = value.strip()
    if not cleaned:
        return None
    try:
        parsed = int(cleaned)
    except ValueError as exc:
        raise ValueError(f"Поле «{label}» должно быть целым числом") from exc
    if parsed < 0:
        raise ValueError(f"Поле «{label}» не может быть отрицательным")
    return parsed


def format_qualifier(value: IcfQualifier | None) -> str:
    return value.display() if value else ""


def parse_qualifier(value: str) -> IcfQualifier | None:
    cleaned = value.strip()
    if not cleaned:
        return None
    facilitator = cleaned.endswith("+")
    raw_number = cleaned[:-1] if facilitator else cleaned
    if raw_number not in {"0", "1", "2", "3", "4"}:
        raise ValueError("Квалификатор МКФ: 0–4 или 0+–4+")
    return IcfQualifier(int(raw_number), facilitator)


def role_names() -> tuple[str, ...]:
    return tuple(role.display_name for role in SpecialistRole)


def role_from_name(value: str) -> SpecialistRole:
    for role in SpecialistRole:
        if value == role.display_name or value == role.value:
            return role
    raise ValueError("Выберите специалиста")


def parse_episode_folder(value: str) -> Path:
    cleaned = value.strip()
    if not cleaned:
        raise ValueError("Выберите папку эпизода")
    return Path(cleaned).expanduser().resolve()


def sections_for(episode: Episode, kind: MdrkKind) -> ClinicalSections:
    return episode.initial_sections if kind is MdrkKind.INITIAL else episode.sections


def parse_episode_form_data(
    entry_values: Mapping[str, str],
    section_values: Mapping[str, str],
) -> EpisodeFormData:
    """Parse the complete UI form without mutating an episode."""

    return EpisodeFormData(
        full_name=entry_values["full_name"].strip(),
        medical_record_number=entry_values["record_number"].strip(),
        birth_date=parse_optional_date(entry_values["birth_date"]),
        sex=entry_values["sex"].strip(),
        admission_datetime=parse_optional_datetime(entry_values["admission"]),
        meeting_at=parse_optional_meeting_datetime(entry_values["meeting"]),
        department=entry_values["department"].strip(),
        stage=entry_values["stage"].strip(),
        course_duration_days=parse_optional_nonnegative_int(
            entry_values["duration"], "Койко-дни"
        ),
        section_values=tuple(
            (key, value.strip()) for key, value in section_values.items()
        ),
    )


def apply_episode_form_data(
    episode: Episode,
    kind: MdrkKind,
    form: EpisodeFormData,
) -> None:
    episode.identity.full_name = form.full_name
    episode.identity.medical_record_number = form.medical_record_number
    episode.identity.birth_date = form.birth_date
    episode.identity.sex = form.sex
    episode.admission_datetime = form.admission_datetime
    if kind is MdrkKind.INITIAL:
        episode.initial_meeting_at = form.meeting_at
    else:
        episode.final_meeting_at = form.meeting_at
    episode.department = form.department
    episode.stage = form.stage
    episode.course_duration_days = form.course_duration_days
    target_sections = sections_for(episode, kind)
    for key, value in form.section_values:
        setattr(target_sections, key, value)


def procedure_specialist_role(value: str) -> SpecialistRole | None:
    cleaned = " ".join(value.casefold().replace("ё", "е").split())
    if not cleaned:
        return None
    for role in SpecialistRole:
        if cleaned in {role.value.casefold(), role.display_name.casefold().replace("ё", "е")}:
            return None if role is SpecialistRole.OTHER else role

    patterns = (
        (SpecialistRole.NEUROPSYCHOLOGIST, ("нейропсих",)),
        (SpecialistRole.PATHOPSYCHOLOGIST, ("патопсих",)),
        (SpecialistRole.OCCUPATIONAL_THERAPIST, ("эрго",)),
        (SpecialistRole.PHYSICAL_THERAPIST, ("физическ", "лфк", "фт")),
        (SpecialistRole.LOGOPEDIST, ("логопед", "афазиолог")),
        (SpecialistRole.NEUROLOGIST, ("невролог",)),
        (SpecialistRole.FRM, ("фрм",)),
    )
    for role, markers in patterns:
        if any(marker in cleaned for marker in markers):
            return role
    return None


def episode_signatory_roles(
    episode: Episode,
    kind: MdrkKind,
) -> tuple[SpecialistRole, ...]:
    boundary = episode.meeting_at(kind)
    roles = {
        source.role
        for source in episode.sources
        if source.role is not SpecialistRole.OTHER
        and (
            source.clinical_datetime is None
            or boundary is None
            or source.clinical_datetime <= boundary
        )
    }
    roles.update(
        finding.role
        for finding in episode.findings
        if (
            finding.source_datetime is None
            or boundary is None
            or finding.source_datetime <= boundary
        )
        if finding.role is not SpecialistRole.OTHER or finding.conclusion or finding.scales
    )
    roles.update(
        domain.specialist
        for domain in build_snapshot(episode, kind).icf_domains
        if domain.specialist is not SpecialistRole.OTHER
    )
    return tuple(role for role in SpecialistRole if role in roles and role is not SpecialistRole.OTHER)
