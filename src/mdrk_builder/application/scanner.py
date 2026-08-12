from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, fields
from datetime import date, datetime, time, timedelta
from difflib import SequenceMatcher
from pathlib import Path
from typing import Iterable

from mdrk_builder.application.clinical_text import (
    DIAGNOSTIC_DUPLICATE_THRESHOLD,
    ClinicalTextObservation,
    compose_clinical_timeline,
    is_empty_clinical_update,
)
from mdrk_builder.application.extractors import (
    IcfObservation,
    extract_admission_datetime,
    extract_clinical_datetime,
    extract_clinical_sections,
    extract_conclusion,
    extract_icf_observations,
    extract_mdrk_document_datetime,
    extract_mdrk_meeting_datetimes,
    extract_mdrk_scale_measurements,
    extract_patient_identity,
    extract_procedures,
    extract_scale_measurements,
)
from mdrk_builder.domain import (
    Episode,
    IcfDomain,
    IcfQualifier,
    ReviewIssue,
    ReviewSeverity,
    SourceDocument,
    SpecialistFinding,
    SpecialistRole,
)
from mdrk_builder.infrastructure.classifier import DocumentClassification, classify_document
from mdrk_builder.infrastructure.converter import ConversionError, DocumentNormalizer
from mdrk_builder.infrastructure.ooxml_reader import ParsedDocument, read_docx


@dataclass(slots=True)
class ScannedRecord:
    document: ParsedDocument
    classification: DocumentClassification
    clinical_datetime: datetime | None


def discover_source_files(folder: Path) -> list[Path]:
    if not folder.is_dir():
        raise NotADirectoryError(folder)
    return sorted(
        (
            path
            for path in folder.rglob("*")
            if path.is_file()
            and path.suffix.casefold() in DocumentNormalizer.SUPPORTED
            and not path.name.startswith("~$")
        ),
        key=lambda path: str(path).casefold(),
    )


def _initial_mdrk_day(value: date) -> date:
    """Return the clinic's MDRK-1 day for the admission weekday."""

    days_ahead = {
        0: 1,  # Monday -> Tuesday
        1: 1,
        2: 1,
        3: 1,  # Thursday -> Friday
        4: 3,  # Friday -> Monday
        5: 3,  # Saturday -> Tuesday
        6: 2,  # Sunday -> Tuesday
    }[value.weekday()]
    return value + timedelta(days=days_ahead)


def _most_common_datetime(values: Iterable[datetime | None]) -> tuple[datetime | None, set[datetime]]:
    present = [value for value in values if value is not None]
    if not present:
        return None, set()
    counts = Counter(present)
    return counts.most_common(1)[0][0], set(present)


def _merge_identity(episode: Episode, records: list[ScannedRecord]) -> None:
    identities = [(extract_patient_identity(item.document), item) for item in records]
    priority = {
        SpecialistRole.NEUROLOGIST: 3,
        SpecialistRole.FRM: 3,
        SpecialistRole.OTHER: 1,
    }
    identities.sort(
        key=lambda pair: (
            priority.get(pair[1].classification.role, 2),
            pair[1].clinical_datetime or datetime.min,
        ),
        reverse=True,
    )
    fields = ("full_name", "birth_date", "sex", "medical_record_number")
    for field_name in fields:
        choices = [getattr(identity, field_name) for identity, _ in identities if getattr(identity, field_name)]
        if not choices:
            continue
        chosen = choices[0]
        setattr(episode.identity, field_name, chosen)
        distinct = {
            (
                _normalized_record_number(str(value))
                if field_name == "medical_record_number"
                else str(value).casefold()
            )
            for value in choices
        }
        source = next(item.document.source_path for identity, item in identities if getattr(identity, field_name) == chosen)
        episode.field_sources[f"identity.{field_name}"] = source
        if len(distinct) > 1:
            severity = (
                ReviewSeverity.BLOCKING
                if field_name == "medical_record_number"
                else ReviewSeverity.WARNING
            )
            message = (
                "В папке найдены разные номера ИБ. Проверьте номер эпизода "
                "в форме и подтвердите ручное значение."
                if field_name == "medical_record_number"
                else f"В источниках различаются значения поля «{field_name}». Выбрано: {chosen}"
            )
            issue_source = next(
                (
                    item.document.source_path
                    for identity, item in identities
                    if getattr(identity, field_name)
                    and (
                        _normalized_record_number(
                            str(getattr(identity, field_name))
                        )
                        != _normalized_record_number(str(chosen))
                        if field_name == "medical_record_number"
                        else getattr(identity, field_name) != chosen
                    )
                ),
                source,
            )
            episode.issues.append(
                ReviewIssue(
                    f"identity_conflict_{field_name}",
                    message,
                    severity,
                    f"identity.{field_name}",
                    issue_source,
                )
            )


def _normalized_record_number(value: str) -> str:
    normalized = "".join(
        character
        for character in value.casefold().replace("№", "")
        if character.isalnum() or character == "/"
    )
    while normalized.startswith("скп"):
        normalized = normalized.removeprefix("скп")
    return normalized


def _records_for_selected_medical_record(
    episode: Episode,
    records: list[ScannedRecord],
) -> list[ScannedRecord]:
    """Exclude explicitly different medical records from episode-derived data.

    Related discharge summaries may legitimately live in the patient folder.
    They remain visible as sources, but must not supply this rehabilitation
    episode's dates, sections, findings, ICF or procedures.
    """

    selected = _normalized_record_number(episode.identity.medical_record_number)
    if not selected:
        return records
    active: list[ScannedRecord] = []
    for record in records:
        candidate = extract_patient_identity(record.document).medical_record_number
        if candidate and _normalized_record_number(candidate) != selected:
            source = record.document.source_path
            episode.excluded_source_paths.add(source)
            episode.issues.append(
                ReviewIssue(
                    "source_medical_record_mismatch",
                    (
                        f"Источник относится к другой ИБ ({candidate}) и не используется "
                        f"для эпизода {episode.identity.medical_record_number}."
                    ),
                    ReviewSeverity.INFO,
                    "sources",
                    source,
                )
            )
            continue
        active.append(record)
    return active


def _refresh_record_number_conflict_source(
    episode: Episode,
    records: list[ScannedRecord],
) -> None:
    selected = _normalized_record_number(episode.identity.medical_record_number)
    if not selected:
        return
    conflicting_source = next(
        (
            record.document.source_path
            for record in records
            if (
                candidate := extract_patient_identity(
                    record.document
                ).medical_record_number
            )
            and _normalized_record_number(candidate) != selected
        ),
        None,
    )
    if conflicting_source is None:
        return
    for issue in episode.issues:
        if issue.code == "identity_conflict_medical_record_number":
            issue.source = conflicting_source


def _merge_dates(
    episode: Episode,
    records: list[ScannedRecord],
    *,
    admission_datetime_override: datetime | None = None,
) -> None:
    admission_pairs = [
        (extract_admission_datetime(item.document), item)
        for item in records
    ]
    admission, admission_values = _most_common_datetime(
        value for value, _ in admission_pairs
    )
    episode.admission_datetime = admission_datetime_override or admission
    admission_dates = {value.date() for value in admission_values}
    if len(admission_dates) > 1:
        selected_date = (
            episode.admission_datetime.date()
            if episode.admission_datetime is not None
            else None
        )
        issue_source = next(
            (
                item.document.source_path
                for value, item in admission_pairs
                if value is not None
                and (selected_date is None or value.date() != selected_date)
            ),
            next(
                (
                    item.document.source_path
                    for value, item in admission_pairs
                    if value is not None
                ),
                None,
            ),
        )
        episode.issues.append(
            ReviewIssue(
                "mixed_hospitalizations_admission_date",
                (
                    "В источниках найдены разные даты поступления. Проверьте дату эпизода "
                    "в форме, повторите сканирование и подтвердите ручное значение."
                ),
                ReviewSeverity.BLOCKING,
                "admission_datetime",
                issue_source,
            )
        )
    elif len(admission_values) > 1:
        episode.issues.append(
            ReviewIssue(
                "admission_time_conflict",
                "В источниках различается время поступления в рамках одной даты",
                ReviewSeverity.WARNING,
                "admission_datetime",
            )
        )
    # MDRK is prepared before discharge.  A discharge date found in a source is
    # therefore neither a meeting boundary nor an episode validation boundary.
    # Keep it out of the materialized episode entirely so a discharge summary
    # cannot silently move or block an MDRK snapshot.
    episode.discharge_datetime = None
    if episode.admission_datetime:
        episode.initial_meeting_at = datetime.combine(
            _initial_mdrk_day(episode.admission_datetime.date()), time(8, 0)
        )
    scheduled_final_candidates = [
        meeting
        for item in records
        for meeting in extract_mdrk_meeting_datetimes(item.document)
        if (episode.initial_meeting_at is None or meeting > episode.initial_meeting_at)
    ]
    final_candidates = [
        item.clinical_datetime
        for item in records
        if item.clinical_datetime is not None
        and item.classification.document_type not in {
            "administrative",
            "assignment_sheet",
            "other_consilium",
            "final",
        }
    ]
    latest_source = max(
        (
            value
            for value in final_candidates
            if episode.initial_meeting_at is None or value > episode.initial_meeting_at
        ),
        default=None,
    )
    if scheduled_final_candidates:
        episode.final_meeting_at = max(scheduled_final_candidates)
    elif latest_source:
        episode.final_meeting_at = latest_source
    if episode.admission_datetime and episode.final_meeting_at:
        duration_days = (
            episode.final_meeting_at.date() - episode.admission_datetime.date()
        ).days
        if duration_days > 0:
            episode.course_duration_days = duration_days
        elif duration_days == 0:
            episode.course_duration_days = 1
        else:
            episode.course_duration_days = None
    else:
        episode.course_duration_days = None


def _latest_clinical_sections(episode: Episode, records: list[ScannedRecord]) -> None:
    clinical_records = [
        item
        for item in records
        if item.classification.document_type
        not in {"administrative", "assignment_sheet", "other_consilium"}
    ]
    physician_records = [
        item
        for item in clinical_records
        if item.classification.role in {SpecialistRole.NEUROLOGIST, SpecialistRole.FRM}
    ]
    extracted = {
        id(record): extract_clinical_sections(record.document) for record in clinical_records
    }

    def eligible_as_of(
        values: list[ScannedRecord],
        boundary: datetime | None,
    ) -> list[ScannedRecord]:
        dated = [
            item
            for item in values
            if item.clinical_datetime is not None
            and (boundary is None or item.clinical_datetime <= boundary)
        ]
        eligible = dated or [item for item in values if item.clinical_datetime is None]
        return sorted(
            eligible,
            key=lambda item: (
                item.clinical_datetime or datetime.min,
                str(item.document.source_path).casefold(),
            ),
        )

    def fill_as_of(
        target,
        provenance: dict[str, Path],
        boundary: datetime | None,
        meeting_field: str,
        *,
        include_updates: bool,
    ) -> None:
        physician_eligible = eligible_as_of(physician_records, boundary)
        future_physician_records = (
            sorted(
                (
                    item
                    for item in physician_records
                    if item.clinical_datetime is not None
                    and item.clinical_datetime > boundary
                    and item.classification.document_type != "final"
                ),
                key=lambda item: item.clinical_datetime or datetime.max,
            )
            if boundary is not None
            else []
        )
        all_eligible = eligible_as_of(clinical_records, boundary)
        specialist_fallback_fields = {"laboratory_results", "instrumental_results"}
        timeline_fields = {
            "clinical_diagnosis",
            "disease_history",
            "life_history",
            "laboratory_results",
            "instrumental_results",
        }
        for field_info in fields(target):
            field_name = field_info.name
            candidates = [
                (record, extracted[id(record)][field_name])
                for record in physician_eligible
                if extracted[id(record)][field_name]
                and not is_empty_clinical_update(extracted[id(record)][field_name])
            ]
            if not candidates and future_physician_records:
                future_candidates = [
                    (record, extracted[id(record)][field_name])
                    for record in future_physician_records
                    if extracted[id(record)][field_name]
                    and not is_empty_clinical_update(extracted[id(record)][field_name])
                ]
                if future_candidates:
                    candidates = [future_candidates[0]]
                    preview = future_candidates[0][0]
                    if not any(
                        issue.code == "physician_source_after_meeting"
                        and issue.field == meeting_field
                        for issue in episode.issues
                    ):
                        episode.issues.append(
                            ReviewIssue(
                                "physician_source_after_meeting",
                                (
                                    "Врачебный источник с клиническими данными датирован "
                                    "позже заседания. Поля показаны в форме только для проверки; "
                                    "измените время заседания и повторите сканирование."
                                ),
                                ReviewSeverity.BLOCKING,
                                meeting_field,
                                preview.document.source_path,
                            )
                        )
            if not candidates and field_name in specialist_fallback_fields:
                candidates = [
                    (record, extracted[id(record)][field_name])
                    for record in all_eligible
                    if extracted[id(record)][field_name]
                    and not is_empty_clinical_update(extracted[id(record)][field_name])
                ]
            if not candidates:
                continue
            if field_name in timeline_fields:
                composed = compose_clinical_timeline(
                    [
                        ClinicalTextObservation(
                            text=value,
                            occurred_at=record.clinical_datetime,
                            document_type=record.classification.document_type,
                            source=record.document.source_path,
                        )
                        for record, value in candidates
                    ],
                    include_updates=include_updates,
                    duplicate_threshold=(
                        DIAGNOSTIC_DUPLICATE_THRESHOLD
                        if field_name in {"laboratory_results", "instrumental_results"}
                        else None
                    ),
                )
                if not composed.text or composed.source is None:
                    continue
                setattr(target, field_name, composed.text)
                provenance[f"sections.{field_name}"] = composed.source
                continue

            # Plans and compact current-state fields are values, not narratives:
            # keep the primary/first substantive value for MDRK-1 and the last
            # substantive value for MDRK-2 without turning them into a diary.
            if include_updates:
                record, value = candidates[-1]
            else:
                primary = [
                    item for item in candidates if item[0].classification.document_type == "initial"
                ]
                record, value = (primary or candidates)[0]
            setattr(target, field_name, value)
            provenance[f"sections.{field_name}"] = record.document.source_path

    fill_as_of(
        episode.initial_sections,
        episode.initial_field_sources,
        episode.initial_meeting_at,
        "initial_meeting_at",
        include_updates=False,
    )
    fill_as_of(
        episode.sections,
        episode.field_sources,
        episode.final_meeting_at,
        "final_meeting_at",
        include_updates=True,
    )
    if not episode.initial_sections.rehabilitation_potential.strip():
        episode.initial_sections.rehabilitation_potential = "средний"
    if not episode.sections.rehabilitation_potential.strip():
        episode.sections.rehabilitation_potential = "средний"


def _collect_findings(episode: Episode, records: list[ScannedRecord]) -> None:
    allowed = {
        SpecialistRole.FRM,
        SpecialistRole.NEUROLOGIST,
        SpecialistRole.PHYSICAL_THERAPIST,
        SpecialistRole.OCCUPATIONAL_THERAPIST,
        SpecialistRole.LOGOPEDIST,
        SpecialistRole.NEUROPSYCHOLOGIST,
        SpecialistRole.PATHOPSYCHOLOGIST,
    }
    for record in records:
        role = record.classification.role
        if role not in allowed:
            continue
        conclusion = extract_conclusion(record.document, role)
        scales = extract_scale_measurements(record.document, role, record.clinical_datetime)
        if conclusion or scales:
            episode.findings.append(
                SpecialistFinding(
                    role=role,
                    conclusion=conclusion,
                    source_datetime=record.clinical_datetime,
                    source=record.document.source_path,
                    scales=scales,
                )
            )


ICF_DESCRIPTION_DUPLICATE_THRESHOLD = 0.94


def _normalized_icf_description(value: str) -> str:
    return " ".join(
        "".join(
            character if character.isalnum() else " "
            for character in value.casefold().replace("ё", "е")
        ).split()
    )


def _observation_map(
    observations: list[IcfObservation],
) -> dict[tuple[str, str], IcfObservation]:
    """Keep distinct same-code domains; wording is part of ICF identity."""

    return {
        (
            _normalized_icf_code(item.code),
            _normalized_icf_description(item.description),
        ): item
        for item in observations
    }


def _description_markers(value: str) -> frozenset[str]:
    prefixes = {
        "left": "лев",
        "right": "прав",
        "arm": "рук",
        "leg": "ног",
        "upper": "верх",
        "lower": "ниж",
    }
    markers = {
        marker
        for token in value.split()
        for marker, prefix in prefixes.items()
        if token.startswith(prefix)
    }
    return frozenset(markers)


def _near_duplicate_icf_description(left: str, right: str) -> bool:
    if left == right:
        return True
    if not left or not right:
        return False
    left_markers = _description_markers(left)
    right_markers = _description_markers(right)
    for mutually_exclusive in ({"left", "right"}, {"arm", "leg"}, {"upper", "lower"}):
        if (
            left_markers & mutually_exclusive
            and right_markers & mutually_exclusive
            and left_markers & mutually_exclusive != right_markers & mutually_exclusive
        ):
            return False
    return (
        SequenceMatcher(None, left, right).ratio()
        >= ICF_DESCRIPTION_DUPLICATE_THRESHOLD
    )


_PERSONAL_FACTOR_ROLE_PRIORITY = {
    SpecialistRole.PATHOPSYCHOLOGIST: 70,
    SpecialistRole.NEUROPSYCHOLOGIST: 65,
    SpecialistRole.FRM: 60,
    SpecialistRole.NEUROLOGIST: 55,
    SpecialistRole.LOGOPEDIST: 30,
    SpecialistRole.OCCUPATIONAL_THERAPIST: 25,
    SpecialistRole.PHYSICAL_THERAPIST: 20,
}


def _normalized_icf_code(value: str) -> str:
    return value.casefold().replace(" ", "")


def _allowed_from_initial_neurologist(
    record: ScannedRecord,
    observation: IcfObservation,
) -> bool:
    """Keep only the neurologist-owned slice of a copied primary SHRM table."""

    if not (
        record.classification.role is SpecialistRole.NEUROLOGIST
        and record.classification.document_type == "initial"
    ):
        return True
    code = _normalized_icf_code(observation.code)
    return code.startswith(("b", "s", "pf")) or code == "e1101"


def _normalized_personal_factor_description(value: str) -> str:
    return " ".join(value.casefold().replace("ё", "е").split()).strip(" .,:;")


def _personal_factor_records(
    records: list[ScannedRecord],
) -> dict[str, list[tuple[ScannedRecord, IcfObservation]]]:
    """Collect descriptive Pf rows globally instead of assigning them to one profile."""

    grouped: dict[str, list[tuple[ScannedRecord, IcfObservation]]] = defaultdict(list)
    physician_roles = {SpecialistRole.FRM, SpecialistRole.NEUROLOGIST}
    for record in records:
        source_role = record.classification.role
        if source_role not in _PERSONAL_FACTOR_ROLE_PRIORITY:
            continue
        for observation in extract_icf_observations(record.document):
            if not _allowed_from_initial_neurologist(record, observation):
                continue
            code = _normalized_icf_code(observation.code)
            if not code.startswith("pf") or not observation.description.strip():
                continue
            owner = observation.specialist
            compatible_owner = owner is None or owner is source_role or (
                source_role in physician_roles and owner in physician_roles
            )
            if not compatible_owner:
                # A physician's table may contain a copied domain owned by another
                # specialist. It is not an authoritative personal-factor source.
                continue
            grouped[code].append((record, observation))
    return grouped


def _select_personal_factor(
    occurrences: list[tuple[ScannedRecord, IcfObservation]],
    boundary: datetime | None,
) -> tuple[ScannedRecord, IcfObservation] | None:
    dated = [
        item
        for item in occurrences
        if item[0].clinical_datetime is not None
        and (boundary is None or item[0].clinical_datetime <= boundary)
    ]
    eligible = dated or [item for item in occurrences if item[0].clinical_datetime is None]
    if not eligible:
        return None
    return max(
        eligible,
        key=lambda item: (
            _PERSONAL_FACTOR_ROLE_PRIORITY[item[0].classification.role],
            item[0].clinical_datetime or datetime.min,
            str(item[0].document.source_path).casefold(),
        ),
    )


def _merge_personal_factors(episode: Episode, records: list[ScannedRecord]) -> None:
    for occurrences in _personal_factor_records(records).values():
        initial = _select_personal_factor(occurrences, episode.initial_meeting_at)
        final = _select_personal_factor(occurrences, episode.final_meeting_at)
        if final is None:
            continue

        selected_record, selected_observation = initial or final
        final_record, _final_observation = final
        role = selected_observation.specialist or SpecialistRole.OTHER
        initial_source = initial[0].document.source_path if initial is not None else None
        final_source = (
            final_record.document.source_path
            if initial is None or final_record.document.source_path != initial_source
            else None
        )
        episode.icf_domains.append(
            IcfDomain(
                code=selected_observation.code,
                description=selected_observation.description,
                specialist=role,
                note=selected_observation.note,
                initial_source=initial_source,
                final_source=final_source,
                initial_measured_at=(
                    initial[0].clinical_datetime if initial is not None else None
                ),
                final_measured_at=(
                    final_record.clinical_datetime
                    if final_source is not None
                    else None
                ),
            )
        )

        eligible_final = [
            item
            for item in occurrences
            if item[0].clinical_datetime is None
            or episode.final_meeting_at is None
            or item[0].clinical_datetime <= episode.final_meeting_at
        ]
        descriptions = {
            _normalized_personal_factor_description(item[1].description)
            for item in eligible_final
            if item[1].description.strip()
        }
        if len(descriptions) > 1:
            episode.issues.append(
                ReviewIssue(
                    "personal_factor_conflict",
                    "В источниках найдены разные формулировки персонального фактора Pf. "
                    "Выбрана одна строка по приоритету источника; проверьте текст вручную.",
                    ReviewSeverity.WARNING,
                    f"icf.{selected_observation.code}",
                    selected_record.document.source_path,
                )
            )


def _profile_records(records: list[ScannedRecord]) -> dict[SpecialistRole, list[tuple[ScannedRecord, list[IcfObservation]]]]:
    grouped: dict[SpecialistRole, list[tuple[ScannedRecord, list[IcfObservation]]]] = defaultdict(list)
    for record in records:
        observations = extract_icf_observations(record.document)
        source_role = record.classification.role
        if source_role is SpecialistRole.OTHER:
            continue
        physician_roles = {SpecialistRole.FRM, SpecialistRole.NEUROLOGIST}
        by_owner: dict[SpecialistRole, list[IcfObservation]] = defaultdict(list)
        for observation in observations:
            if not _allowed_from_initial_neurologist(record, observation):
                continue
            is_personal_factor = observation.code.casefold().startswith("pf")
            if is_personal_factor:
                # Pf is episode-level descriptive data and is merged separately
                # across roles to avoid duplicate rows.
                continue
            owner = observation.specialist
            compatible_owner = owner is source_role or (
                source_role in physician_roles and owner in physician_roles
            )
            if owner is not None and not compatible_owner:
                continue
            effective_owner = owner or (
                SpecialistRole.OTHER
                if source_role in physician_roles
                else source_role
            )
            by_owner[effective_owner].append(observation)
        for owner, owned_observations in by_owner.items():
            if owned_observations:
                grouped[owner].append((record, owned_observations))
    for values in grouped.values():
        values.sort(
            key=lambda pair: (
                pair[0].clinical_datetime or datetime.min,
                str(pair[0].document.source_path).casefold(),
            )
        )
    return grouped


def _eligible_icf_occurrences(
    occurrences: list[tuple[ScannedRecord, IcfObservation]],
    boundary: datetime | None,
) -> list[tuple[ScannedRecord, IcfObservation]]:
    dated = [
        item
        for item in occurrences
        if item[0].clinical_datetime is not None
        and (boundary is None or item[0].clinical_datetime <= boundary)
    ]
    eligible = dated or [
        item for item in occurrences if item[0].clinical_datetime is None
    ]
    eligible.sort(
        key=lambda item: (
            item[0].clinical_datetime or datetime.min,
            str(item[0].document.source_path).casefold(),
        )
    )

    # The same table can be embedded more than once in a diary or copied into
    # several files.  All rows for one clinical timestamp are one temporal
    # point even when their description differs slightly.  Equal ratings keep
    # the earliest provenance; conflicting ratings at that timestamp use the
    # deterministic last source as a correction, not as a fake repeat.
    by_datetime: dict[
        datetime | None, list[tuple[ScannedRecord, IcfObservation]]
    ] = defaultdict(list)
    for item in eligible:
        by_datetime[item[0].clinical_datetime].append(item)
    unique: list[tuple[ScannedRecord, IcfObservation]] = []
    for values in by_datetime.values():
        rating_sets = {
            tuple(qualifier.display() for qualifier in observation.ratings)
            for _, observation in values
        }
        unique.append(values[0] if len(rating_sets) == 1 else values[-1])
    return unique


def _merge_icf(episode: Episode, records: list[ScannedRecord]) -> None:
    _merge_personal_factors(episode, records)
    for role, profiles in _profile_records(records).items():
        profile_maps = [(record, _observation_map(values)) for record, values in profiles]
        clusters: dict[
            tuple[str, str], list[tuple[ScannedRecord, IcfObservation]]
        ] = {}
        representatives_by_code: dict[str, list[tuple[str, str]]] = defaultdict(list)
        for record, observations in profile_maps:
            for (code, description), observation in observations.items():
                representative = next(
                    (
                        candidate
                        for candidate in representatives_by_code[code]
                        if _near_duplicate_icf_description(
                            candidate[1], description
                        )
                    ),
                    None,
                )
                if representative is None:
                    representative = (code, description)
                    representatives_by_code[code].append(representative)
                    clusters[representative] = []
                clusters[representative].append((record, observation))

        for occurrences in clusters.values():
            temporal_points = _eligible_icf_occurrences(
                occurrences, episode.final_meeting_at
            )
            if not temporal_points:
                # A source written after MDRK-2 cannot introduce or update a row.
                continue

            initial_record, initial_obs = temporal_points[0]
            final_record, final_obs = temporal_points[-1]
            initial = initial_obs.ratings[0] if initial_obs.ratings else None
            has_distinct_repeat = (
                len(temporal_points) > 1 or len(final_obs.ratings) >= 2
            )
            final = final_obs.current if has_distinct_repeat else None
            # Code/wording/note belong to the baseline definition.  Only the
            # qualifier is updated from the last point, preventing later diary
            # wording from leaking into MDRK-1.
            sample = initial_obs
            specialist = sample.specialist or role
            domain = IcfDomain(
                code=sample.code,
                description=sample.description,
                specialist=specialist,
                initial=initial,
                final=final,
                note=sample.note,
                initial_source=(
                    initial_record.document.source_path if initial is not None else None
                ),
                final_source=(
                    final_record.document.source_path if final is not None else None
                ),
                initial_measured_at=initial_record.clinical_datetime,
                final_measured_at=(
                    final_record.clinical_datetime if final is not None else None
                ),
            )
            episode.icf_domains.append(domain)
            if (
                not domain.code.casefold().startswith("pf")
                and (domain.initial is None or domain.final is None)
            ):
                episode.issues.append(
                    ReviewIssue(
                        "icf_incomplete_pair",
                        f"Домен {domain.code} присутствует только в одной временной точке",
                        ReviewSeverity.WARNING,
                        f"icf.{domain.code}",
                        domain.final_source or domain.initial_source,
                    )
                )

    initial_medication = episode.initial_sections.medication.strip()
    final_medication = episode.sections.medication.strip()
    if initial_medication or final_medication:
        matching = [
            item for item in episode.icf_domains if item.code.casefold().replace(" ", "") == "e1101"
        ]
        existing = matching[0] if matching else None
        initial_medication_source = episode.initial_field_sources.get("sections.medication")
        final_medication_source = episode.field_sources.get("sections.medication")
        source_datetimes = {
            record.document.source_path: record.clinical_datetime
            for record in records
        }
        medication_source = final_medication_source or initial_medication_source
        owner = next(
            (
                record.classification.role
                for record in records
                if record.document.source_path == medication_source
                and record.classification.role in {SpecialistRole.NEUROLOGIST, SpecialistRole.FRM}
            ),
            SpecialistRole.NEUROLOGIST,
        )
        qualifier = IcfQualifier(4, facilitator=True)
        if existing:
            existing.specialist = owner
            if initial_medication:
                existing.initial = qualifier
            if final_medication:
                existing.final = qualifier
            existing.note = existing.note or "препараты"
            if initial_medication:
                existing.initial_source = initial_medication_source or existing.initial_source
                existing.initial_measured_at = (
                    source_datetimes.get(initial_medication_source)
                    or existing.initial_measured_at
                )
            if final_medication:
                existing.final_source = final_medication_source or existing.final_source
                existing.final_measured_at = (
                    source_datetimes.get(final_medication_source)
                    or existing.final_measured_at
                )
            episode.icf_domains = [
                item for item in episode.icf_domains if item is existing or item not in matching
            ]
        else:
            episode.icf_domains.append(
                IcfDomain(
                    code="e1101",
                    description="Лекарственные препараты",
                    specialist=owner,
                    initial=qualifier if initial_medication else None,
                    final=qualifier if final_medication else None,
                    note="препараты",
                    initial_source=initial_medication_source if initial_medication else None,
                    final_source=final_medication_source if final_medication else None,
                    initial_measured_at=(
                        source_datetimes.get(initial_medication_source)
                        if initial_medication
                        else None
                    ),
                    final_measured_at=(
                        source_datetimes.get(final_medication_source)
                        if final_medication
                        else None
                    ),
                )
            )

    episode.icf_domains.sort(
        key=lambda item: (
            {"b": 0, "s": 1, "d": 2, "e": 3, "p": 4}.get(item.code[:1].casefold(), 9),
            item.specialist.value,
            item.code.casefold(),
            item.description.casefold(),
        )
    )


def _scale_names_match(left: str, right: str) -> bool:
    left_normalized = _normalized_icf_description(left)
    right_normalized = _normalized_icf_description(right)
    if left_normalized == right_normalized:
        return True
    left_tokens = left_normalized.split()
    right_tokens = right_normalized.split()
    if not left_tokens or not right_tokens:
        return False
    shared = len(set(left_tokens) & set(right_tokens))
    return (
        shared / max(len(set(left_tokens)), len(set(right_tokens))) >= 0.75
        and SequenceMatcher(None, left_normalized, right_normalized).ratio() >= 0.94
    )


def _same_scale_role(left: SpecialistRole, right: SpecialistRole) -> bool:
    physician_roles = {SpecialistRole.FRM, SpecialistRole.NEUROLOGIST}
    return left is right or left in physician_roles and right in physician_roles


def _source_datetime(episode: Episode, source: Path | None) -> datetime | None:
    if source is None:
        return None
    return next(
        (
            item.clinical_datetime
            for item in episode.sources
            if item.path == source and episode.source_is_active(item)
        ),
        None,
    )


def _domain_matches_fallback(domain: IcfDomain, observation: IcfObservation) -> bool:
    return (
        _normalized_icf_code(domain.code) == _normalized_icf_code(observation.code)
        and _near_duplicate_icf_description(
            _normalized_icf_description(domain.description),
            _normalized_icf_description(observation.description),
        )
    )


def _merge_mdrk1_baseline(
    episode: Episode,
    records: list[ScannedRecord],
) -> None:
    """Fill absent baseline ICF/scales from a reliably identified MDRK-1.

    Specialist sources remain authoritative.  MDRK-1 is used only when the
    corresponding point is absent before that meeting; its narrative sections
    and conclusions never enter the episode.
    """

    eligible: list[ScannedRecord] = []
    for record in records:
        measured_at = record.clinical_datetime
        if measured_at is None:
            continue
        if (
            episode.initial_meeting_at is not None
            and measured_at.date() != episode.initial_meeting_at.date()
        ):
            # This protects against an MDRK-2 with accidentally empty repeat
            # columns being mistaken for MDRK-1.
            continue
        if episode.final_meeting_at is not None and measured_at > episode.final_meeting_at:
            continue
        eligible.append(record)
    eligible.sort(
        key=lambda item: (
            item.clinical_datetime or datetime.min,
            str(item.document.source_path).casefold(),
        ),
        reverse=True,
    )
    if not eligible:
        return

    fallback_used = False
    fallback_sources = {item.document.source_path for item in eligible}
    selected_fallback_scales = []
    for record in eligible:
        fallback_at = record.clinical_datetime
        if fallback_at is None:
            continue

        for observation in extract_icf_observations(record.document):
            if len(observation.ratings) > 1 or not observation.description.strip():
                continue
            existing = next(
                (
                    domain
                    for domain in episode.icf_domains
                    if _domain_matches_fallback(domain, observation)
                ),
                None,
            )
            fallback_value = observation.ratings[0] if observation.ratings else None
            if existing is None:
                episode.icf_domains.append(
                    IcfDomain(
                        code=observation.code,
                        description=observation.description,
                        specialist=observation.specialist or SpecialistRole.OTHER,
                        initial=fallback_value,
                        note=observation.note,
                        initial_source=record.document.source_path,
                        initial_measured_at=fallback_at,
                    )
                )
                fallback_used = True
                continue

            if existing.initial_source in fallback_sources:
                continue

            existing_at = existing.initial_measured_at or _source_datetime(
                episode, existing.initial_source
            )
            primary_baseline_present = (
                existing.initial is not None
                or existing.initial_source is not None
            ) and (existing_at is None or existing_at <= fallback_at)
            if primary_baseline_present:
                continue
            if existing.initial is not None and existing.final is None:
                existing.final = existing.initial
                existing.final_source = existing.initial_source
                existing.final_measured_at = existing_at
            existing.initial = fallback_value
            existing.initial_source = record.document.source_path
            existing.initial_measured_at = fallback_at
            fallback_used = True

        fallback_scales = extract_mdrk_scale_measurements(
            record.document,
            fallback_at,
        )
        for measurement in fallback_scales:
            measured_at = measurement.measured_at or fallback_at
            if measured_at > fallback_at:
                continue
            if episode.final_meeting_at is not None and measured_at > episode.final_meeting_at:
                continue
            matching_course_scale = next(
                (
                    existing
                    for finding in episode.findings
                    for existing in finding.scales
                    if _same_scale_role(measurement.specialist, existing.specialist)
                    and _scale_names_match(measurement.name, existing.name)
                ),
                None,
            )
            if matching_course_scale is not None:
                # FRM and neurologist are one physician block in MDRK.  Reuse
                # the specialist source's role so its later point joins the
                # same temporal scale row instead of creating a duplicate.
                measurement.specialist = matching_course_scale.specialist
            if any(
                _same_scale_role(measurement.specialist, selected.specialist)
                and _scale_names_match(measurement.name, selected.name)
                for selected in selected_fallback_scales
            ):
                continue
            authoritative = any(
                _same_scale_role(measurement.specialist, existing.specialist)
                and _scale_names_match(measurement.name, existing.name)
                and (
                    finding.source_datetime is None
                    or finding.source_datetime <= fallback_at
                )
                and (
                    existing.measured_at is None
                    or existing.measured_at <= fallback_at
                )
                for finding in episode.findings
                for existing in finding.scales
                if finding.source not in fallback_sources
            )
            if authoritative:
                continue
            episode.findings.append(
                SpecialistFinding(
                    role=measurement.specialist,
                    source_datetime=fallback_at,
                    source=record.document.source_path,
                    scales=[measurement],
                )
            )
            selected_fallback_scales.append(measurement)
            fallback_used = True

    episode.icf_domains.sort(
        key=lambda item: (
            {"b": 0, "s": 1, "d": 2, "e": 3, "p": 4}.get(
                item.code[:1].casefold(), 9
            ),
            item.specialist.value,
            item.code.casefold(),
            item.description.casefold(),
        )
    )
    if fallback_used:
        episode.issues.append(
            ReviewIssue(
                "mdrk1_baseline_fallback",
                "Недостающие исходные МКФ/шкалы восстановлены из МДРК-1.",
                ReviewSeverity.INFO,
                "sources",
                eligible[0].document.source_path,
            )
        )


def _collect_procedures(episode: Episode, records: list[ScannedRecord]) -> None:
    assignment_records = [item for item in records if item.classification.document_type == "assignment_sheet"]
    assignment_records.sort(key=lambda item: item.clinical_datetime or datetime.min)
    if assignment_records:
        reference_date = (
            episode.admission_datetime.date()
            if episode.admission_datetime is not None
            else None
        )
        episode.procedures = extract_procedures(
            assignment_records[-1].document,
            reference_date=reference_date,
        )
    if not episode.procedures:
        episode.issues.append(
            ReviewIssue(
                "procedures_missing",
                "Лист назначений не найден или из него не удалось посчитать процедуры",
                ReviewSeverity.WARNING,
                "procedures",
            )
        )
    for procedure in episode.procedures:
        if procedure.actual_count is None:
            episode.issues.append(
                ReviewIssue(
                    "procedure_count_missing",
                    f"Не удалось посчитать количество для «{procedure.name}»",
                    ReviewSeverity.WARNING,
                    "procedures",
                    procedure.source,
                )
            )
        if procedure.count_needs_review:
            episode.issues.append(
                ReviewIssue(
                    "procedure_count_ambiguous",
                    f"Количество для «{procedure.name}» получено из неоднозначных отметок «+»; проверьте его вручную.",
                    ReviewSeverity.WARNING,
                    "procedures",
                    procedure.source,
                )
            )
    if episode.procedures and any(not procedure.frequency for procedure in episode.procedures):
        episode.issues.append(
            ReviewIssue(
                "procedure_frequency_missing",
                "Кратность не выводилась из общего числа отметок «+»; при необходимости заполните её вручную.",
                ReviewSeverity.WARNING,
                "procedures.frequency",
            )
        )


def _minimum_field_issues(episode: Episode) -> None:
    required = {
        "identity.full_name": (episode.identity.full_name, "ФИО пациента"),
        "identity.medical_record_number": (episode.identity.medical_record_number, "номер ИБ"),
        "admission_datetime": (episode.admission_datetime, "дата поступления"),
        "initial_sections.clinical_diagnosis": (
            episode.initial_sections.clinical_diagnosis,
            "клинический диагноз на момент МДРК1",
        ),
        "sections.clinical_diagnosis": (episode.sections.clinical_diagnosis, "клинический диагноз"),
    }
    for field_name, (value, label) in required.items():
        if value:
            continue
        episode.issues.append(
            ReviewIssue(
                f"required_{field_name.replace('.', '_')}",
                f"Не найдено обязательное поле: {label}. Заполните его вручную.",
                ReviewSeverity.BLOCKING,
                field_name,
            )
        )
    optional_sections = (
        ("initial_sections.rehabilitation_potential", episode.initial_sections.rehabilitation_potential, "реабилитационный потенциал для МДРК1"),
        ("initial_sections.goal", episode.initial_sections.goal, "цель для МДРК1"),
        ("initial_sections.tasks", episode.initial_sections.tasks, "задачи для МДРК1"),
        ("sections.rehabilitation_potential", episode.sections.rehabilitation_potential, "реабилитационный потенциал для МДРК2"),
    )
    for field_name, value, label in optional_sections:
        if value:
            continue
        episode.issues.append(
            ReviewIssue(
                f"review_{field_name.replace('.', '_')}",
                f"Не найдено поле «{label}». Заполните его вручную или оставьте пустым после проверки.",
                ReviewSeverity.WARNING,
                field_name,
            )
        )
    for source in episode.sources:
        if source.clinical_datetime is None and source.role is not SpecialistRole.OTHER:
            episode.issues.append(
                ReviewIssue(
                    "source_datetime_missing",
                    f"Не определена клиническая дата: {source.path.name}",
                    ReviewSeverity.WARNING,
                    "sources",
                    source.path,
                )
            )


def scan_patient_folder(
    folder: Path,
    *,
    normalizer: DocumentNormalizer | None = None,
    initial_meeting_at: datetime | None = None,
    final_meeting_at: datetime | None = None,
    medical_record_number_override: str | None = None,
    admission_datetime_override: datetime | None = None,
) -> Episode:
    folder = folder.resolve()
    episode = Episode(folder=folder)
    source_files = discover_source_files(folder)
    if not source_files:
        episode.issues.append(
            ReviewIssue(
                "no_word_sources",
                "В выбранной папке нет DOCX, DOC или RTF",
                ReviewSeverity.BLOCKING,
                "folder",
            )
        )
        return episode

    owns_normalizer = normalizer is None
    normalizer = normalizer or DocumentNormalizer()
    records: list[ScannedRecord] = []
    mdrk1_records: list[ScannedRecord] = []
    failures = 0
    try:
        for source_path in source_files:
            try:
                normalized = normalizer.normalize(source_path)
                document = read_docx(normalized, source_path=source_path)
                classification = classify_document(document)
                if classification.is_mdrk:
                    if classification.mdrk_kind == "initial":
                        mdrk_datetime = extract_mdrk_document_datetime(document)
                        episode.sources.append(
                            SourceDocument(
                                path=source_path,
                                role=SpecialistRole.OTHER,
                                clinical_datetime=mdrk_datetime,
                                document_type="mdrk_initial",
                                extraction_method=(
                                    "docx"
                                    if source_path.suffix.casefold() == ".docx"
                                    else "converted"
                                ),
                                sha256=document.sha256,
                            )
                        )
                        mdrk1_records.append(
                            ScannedRecord(document, classification, mdrk_datetime)
                        )
                    continue
                clinical_datetime = (
                    None
                    if classification.document_type in {"administrative", "assignment_sheet", "other_consilium"}
                    else extract_clinical_datetime(document)
                )
                episode.sources.append(
                    SourceDocument(
                        path=source_path,
                        role=classification.role,
                        clinical_datetime=clinical_datetime,
                        document_type=classification.document_type,
                        extraction_method="docx" if source_path.suffix.casefold() == ".docx" else "converted",
                        sha256=document.sha256,
                    )
                )
                records.append(ScannedRecord(document, classification, clinical_datetime))
            except (ConversionError, OSError, ValueError, KeyError) as exc:
                failures += 1
                episode.issues.append(
                    ReviewIssue(
                        "source_read_failed",
                        f"Не удалось прочитать {source_path.name}: {exc}",
                        ReviewSeverity.WARNING,
                        "sources",
                        source_path,
                    )
                )
    finally:
        if owns_normalizer:
            normalizer.close()

    if failures and failures >= max(3, len(source_files) // 2):
        episode.issues.append(
            ReviewIssue(
                "systematic_read_failure",
                "Не удалось прочитать значительную часть исходных документов",
                ReviewSeverity.BLOCKING,
                "sources",
            )
        )
    if records:
        _merge_identity(episode, records)
        if medical_record_number_override is not None:
            cleaned_override = medical_record_number_override.strip()
            if cleaned_override:
                episode.identity.medical_record_number = cleaned_override
                episode.field_sources.pop("identity.medical_record_number", None)
                _refresh_record_number_conflict_source(episode, records)
        episode_records = _records_for_selected_medical_record(episode, records)
        if medical_record_number_override and not any(
            _normalized_record_number(
                extract_patient_identity(record.document).medical_record_number
            )
            == _normalized_record_number(medical_record_number_override)
            for record in records
            if extract_patient_identity(record.document).medical_record_number
        ):
            episode.issues.append(
                ReviewIssue(
                    "record_number_override_without_source",
                    (
                        "Введённый вручную номер ИБ не найден ни в одном источнике. "
                        "Проверьте номер и повторите сканирование."
                    ),
                    ReviewSeverity.BLOCKING,
                    "identity.medical_record_number",
                )
            )
        _merge_dates(
            episode,
            episode_records,
            admission_datetime_override=admission_datetime_override,
        )
        episode.materialized_medical_record_number = episode.identity.medical_record_number
        episode.materialized_admission_datetime = episode.admission_datetime
        if initial_meeting_at is not None:
            episode.initial_meeting_at = initial_meeting_at
        if final_meeting_at is not None:
            episode.final_meeting_at = final_meeting_at
        _latest_clinical_sections(episode, episode_records)
        _collect_findings(episode, episode_records)
        _merge_icf(episode, episode_records)
        active_mdrk1_records = _records_for_selected_medical_record(
            episode,
            mdrk1_records,
        )
        _merge_mdrk1_baseline(episode, active_mdrk1_records)
        _collect_procedures(episode, episode_records)
    _minimum_field_issues(episode)
    return episode
