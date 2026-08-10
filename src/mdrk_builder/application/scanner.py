from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, fields
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Iterable

from mdrk_builder.application.extractors import (
    IcfObservation,
    extract_admission_datetime,
    extract_clinical_datetime,
    extract_clinical_sections,
    extract_conclusion,
    extract_discharge_datetime,
    extract_icf_observations,
    extract_mdrk_meeting_datetimes,
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
    discharge_values = [extract_discharge_datetime(item.document) for item in records]
    discharge_present = [value for value in discharge_values if value is not None]
    episode.discharge_datetime = max(discharge_present) if discharge_present else None
    if episode.admission_datetime:
        episode.initial_meeting_at = datetime.combine(
            _initial_mdrk_day(episode.admission_datetime.date()), time(8, 0)
        )
    scheduled_final_candidates = [
        meeting
        for item in records
        for meeting in extract_mdrk_meeting_datetimes(item.document)
        if (episode.initial_meeting_at is None or meeting > episode.initial_meeting_at)
        and (episode.discharge_datetime is None or meeting <= episode.discharge_datetime)
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
    elif episode.discharge_datetime:
        discharge_meeting = datetime.combine(episode.discharge_datetime.date(), time(11, 0))
        if episode.initial_meeting_at is None or discharge_meeting > episode.initial_meeting_at:
            episode.final_meeting_at = discharge_meeting
    if episode.admission_datetime and episode.discharge_datetime:
        duration_days = (
            episode.discharge_datetime.date() - episode.admission_datetime.date()
        ).days
        if duration_days > 0:
            episode.course_duration_days = duration_days
        elif duration_days == 0:
            episode.course_duration_days = 1
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
        return sorted(eligible, key=lambda item: item.clinical_datetime or datetime.min)

    def fill_as_of(
        target,
        provenance: dict[str, Path],
        boundary: datetime | None,
        meeting_field: str,
    ) -> None:
        physician_eligible = eligible_as_of(physician_records, boundary)
        future_physician_records = (
            sorted(
                (
                    item
                    for item in physician_records
                    if item.clinical_datetime is not None
                    and item.clinical_datetime > boundary
                ),
                key=lambda item: item.clinical_datetime or datetime.max,
            )
            if boundary is not None
            else []
        )
        all_eligible = eligible_as_of(clinical_records, boundary)
        specialist_fallback_fields = {"laboratory_results", "instrumental_results"}
        for field_info in fields(target):
            field_name = field_info.name
            candidates = [
                (record, extracted[id(record)][field_name])
                for record in physician_eligible
                if extracted[id(record)][field_name]
            ]
            if not candidates and future_physician_records:
                future_candidates = [
                    (record, extracted[id(record)][field_name])
                    for record in future_physician_records
                    if extracted[id(record)][field_name]
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
                ]
            if not candidates:
                continue
            record, value = candidates[-1]
            setattr(target, field_name, value)
            provenance[f"sections.{field_name}"] = record.document.source_path

    fill_as_of(
        episode.initial_sections,
        episode.initial_field_sources,
        episode.initial_meeting_at,
        "initial_meeting_at",
    )
    fill_as_of(
        episode.sections,
        episode.field_sources,
        episode.final_meeting_at,
        "final_meeting_at",
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


def _observation_map(observations: list[IcfObservation]) -> dict[tuple[str, str], IcfObservation]:
    return {
        (item.code.casefold().replace(" ", ""), " ".join(item.description.casefold().split())): item
        for item in observations
    }


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
        role = record.classification.role
        if role is SpecialistRole.OTHER:
            continue
        physician_roles = {SpecialistRole.FRM, SpecialistRole.NEUROLOGIST}
        filtered: list[IcfObservation] = []
        for observation in observations:
            is_personal_factor = observation.code.casefold().startswith("pf")
            if is_personal_factor:
                # Pf is episode-level descriptive data and is merged separately
                # across roles to avoid duplicate rows.
                continue
            owner = observation.specialist
            compatible_owner = owner is role or (
                role in physician_roles and owner in physician_roles
            )
            if owner is not None and not compatible_owner:
                continue
            filtered.append(observation)
        observations = filtered
        if observations:
            grouped[role].append((record, observations))
    for values in grouped.values():
        values.sort(key=lambda pair: pair[0].clinical_datetime or datetime.min)
    return grouped


def _merge_icf(episode: Episode, records: list[ScannedRecord]) -> None:
    _merge_personal_factors(episode, records)
    for role, profiles in _profile_records(records).items():
        profile_maps = [(record, _observation_map(values)) for record, values in profiles]
        all_keys = list(dict.fromkeys(key for _, values in profile_maps for key in values))
        for key in all_keys:
            occurrences = [(record, values[key]) for record, values in profile_maps if key in values]

            def as_of(boundary: datetime | None):
                dated = [
                    (record, observation)
                    for record, observation in occurrences
                    if record.clinical_datetime is not None
                    and (boundary is None or record.clinical_datetime <= boundary)
                ]
                return dated or [
                    (record, observation)
                    for record, observation in occurrences
                    if record.clinical_datetime is None
                ]

            initial_occurrences = as_of(episode.initial_meeting_at)
            final_occurrences = as_of(episode.final_meeting_at)
            if not final_occurrences:
                # A source written after MDRK-2 cannot introduce or update a row.
                continue

            initial_record = None
            initial_obs = None
            initial = None
            if initial_occurrences:
                initial_record, initial_obs = initial_occurrences[-1]
                initial = initial_obs.ratings[0] if initial_obs.ratings else None

            final_record, final_obs = final_occurrences[-1]
            explicit_repeat = (
                len(final_obs.ratings) >= 2
                or final_record.classification.document_type in {"final", "follow_up"}
                or (
                    final_record.clinical_datetime is not None
                    and episode.initial_meeting_at is not None
                    and final_record.clinical_datetime > episode.initial_meeting_at
                )
            )
            final = final_obs.current if explicit_repeat else None
            sample = final_obs if final_obs is not None else initial_obs
            if sample is None:
                continue
            is_personal_factor = sample.code.casefold().startswith("pf")
            has_distinct_final_source = (
                final_record is not initial_record
                or final_record.clinical_datetime is not None
                and episode.initial_meeting_at is not None
                and final_record.clinical_datetime > episode.initial_meeting_at
            )
            specialist = sample.specialist or (
                SpecialistRole.OTHER
                if role in {SpecialistRole.FRM, SpecialistRole.NEUROLOGIST}
                else role
            )
            domain = IcfDomain(
                code=sample.code,
                description=sample.description,
                specialist=specialist,
                initial=initial,
                final=final,
                note=sample.note,
                initial_source=(
                    initial_record.document.source_path
                    if initial_record is not None and (initial is not None or is_personal_factor)
                    else None
                ),
                final_source=(
                    final_record.document.source_path
                    if final is not None or is_personal_factor and has_distinct_final_source
                    else None
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
            if final_medication:
                existing.final_source = final_medication_source or existing.final_source
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
    failures = 0
    try:
        for source_path in source_files:
            try:
                normalized = normalizer.normalize(source_path)
                document = read_docx(normalized, source_path=source_path)
                classification = classify_document(document)
                if classification.is_mdrk:
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
        _collect_procedures(episode, episode_records)
    _minimum_field_issues(episode)
    return episode
