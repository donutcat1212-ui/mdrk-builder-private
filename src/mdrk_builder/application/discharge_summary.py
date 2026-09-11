from __future__ import annotations

from dataclasses import replace
from datetime import datetime, time
from pathlib import Path

from mdrk_builder.domain.document_dates import discharge_document_datetime

from mdrk_builder.application.admission_only_scales import omit_admission_scales_from_discharge
from mdrk_builder.application.discharge_extractors import (
    extract_complaints,
    extract_discharge_clinical_sections,
    extract_discharge_header,
    update_header_period,
    extract_discharge_final_fields,
    extract_discharge_scale_values,
    extract_instrumental_results,
    extract_laboratory_results,
    extract_local_status,
    extract_medical_examination_summary,
    extract_neurological_status,
    extract_other_consultations,
    extract_physical_exam,
    extract_provided_documents,
    extract_radiation_exposure,
    extract_signature_block,
)
from mdrk_builder.application.discharge_source_selection import (
    SourceCandidate,
    select_discharge_sources,
    source_scan_for_episode,
)
from mdrk_builder.application.final_mdrk import (
    apply_final_mdrk_document,
    select_final_mdrk_document,
)
from mdrk_builder.application.extractors import extract_clinical_datetime, extract_specialist_name, _infer_procedure_frequency
from mdrk_builder.application.discharge_identity import complete_identity, episode_header
from mdrk_builder.application.discharge_examinations import ExaminationSection, partition_examinations
from mdrk_builder.application.scanner import scan_patient_folder
from mdrk_builder.application.snapshot import Snapshot, build_snapshot, canonical_scale_name
from mdrk_builder.application.source_scan import scan_source_documents
from mdrk_builder.domain import (
    DischargeScaleRow,
    DischargeSummaryDraft,
    DischargeTeamFinding,
    MdrkKind,
    PatientIdentity,
    ReviewIssue,
    ReviewSeverity,
    SpecialistRole,
)
from mdrk_builder.infrastructure.converter import DocumentNormalizer


def _copy_episode_issues(
    issues: list[ReviewIssue],
    *,
    record_number_selected_from_sources: bool,
) -> list[ReviewIssue]:
    # Discharge has its own projected diagnosis and required-field checks.
    # Missing sections in the separate MDRK snapshot must not block it.
    issues = [issue for issue in issues if not issue.code.startswith((
        "required_initial_sections_", "required_sections_"))]
    if not record_number_selected_from_sources:
        return list(issues)
    return [
        issue
        for issue in issues
        if issue.code != "identity_conflict_medical_record_number"
    ]


def _consultation_texts(
    candidate: SourceCandidate,
    *,
    admission_at: datetime | None,
    discharge_at: datetime | None,
    issues: list[ReviewIssue],
) -> str:
    selected: list[str] = []
    for consultation in extract_other_consultations(candidate.scanned.document):
        occurred_at = consultation.occurred_at
        outside_episode = occurred_at is not None and (
            (admission_at is not None and occurred_at.date() < admission_at.date())
            or (discharge_at is not None and occurred_at.date() > discharge_at.date())
        )
        if outside_episode:
            issues.append(
                ReviewIssue(
                    "consultation_outside_episode",
                    (
                        "Консультация с датой вне текущей госпитализации исключена: "
                        f"{occurred_at:%d.%m.%Y}."
                    ),
                    ReviewSeverity.WARNING,
                    "other_consultations",
                    candidate.path,
                )
            )
            continue
        selected.append(consultation.text)
    return "\n\n".join(selected)


def _field_source(
    target: dict[str, Path],
    field_name: str,
    value: str,
    candidate: SourceCandidate | None,
) -> None:
    if value.strip() and candidate is not None:
        target[field_name] = candidate.path


def _copy_identity(identity: PatientIdentity) -> PatientIdentity:
    return PatientIdentity(
        full_name=identity.full_name,
        birth_date=identity.birth_date,
        sex=identity.sex,
        medical_record_number=identity.medical_record_number,
    )


def _project_team_findings(snapshot: Snapshot, sources=()) -> tuple[DischargeTeamFinding, ...]:
    return tuple(
        DischargeTeamFinding(
            role=finding.role,
            specialist_title=finding.specialist_title,
            conclusion=finding.conclusion,
            source=finding.source,
            specialist_name=next((source.specialist_name for source in sources if source.path == finding.source), ""),
            occurred_at=finding.source_datetime,
            scales=tuple(DischargeScaleRow(
                row.role, row.name,
                value=(row.current or row.initial).value if (row.current or row.initial) else "",
                source=(row.current or row.initial).source if (row.current or row.initial) else None,
                initial_value=row.initial.value if row.initial else "",
                initial_source=row.initial.source if row.initial else None,
                initial_at=row.initial.measured_at if row.initial else None,
                current_at=(row.current or row.initial).measured_at if (row.current or row.initial) else None,
            ) for row in snapshot.scale_rows if row.role is finding.role),
        )
        for finding in snapshot.findings
    )


def _project_scale_rows(
    snapshot: Snapshot,
    *,
    final_mdrk_source: Path | None,
    discharge_source: Path | None = None,
) -> tuple[tuple[DischargeScaleRow, ...], tuple[DischargeScaleRow, ...]]:
    final_sources = {source for source in (final_mdrk_source, discharge_source) if source is not None}
    admission_rows = tuple(
        DischargeScaleRow(
            role=row.role,
            name=row.name,
            value=(
                row.initial.value
                if row.initial is not None
                and not (
                    final_mdrk_source is not None
                    and row.current is None
                    and row.initial.source in final_sources
                )
                else ""
            ),
            source=row.initial.source if row.initial is not None else None,
            current_at=row.initial.measured_at if row.initial is not None else None,
        )
        for row in snapshot.scale_rows
    )
    discharge_rows = tuple(
        DischargeScaleRow(
            role=row.role,
            name=row.name,
            value=(
                row.current.value
                if row.current is not None
                else (
                    row.initial.value
                    if row.initial is not None
                    else ""
                )
            ),
            source=(row.current.source if row.current is not None else row.initial.source if row.initial is not None else None),
            current_at=(row.current.measured_at if row.current is not None else row.initial.measured_at if row.initial is not None else None),
        )
        for row in snapshot.scale_rows
    )
    return (
        tuple(replace(row, source=None) if not row.value.strip() else row for row in admission_rows),
        tuple(replace(row, source=None) if not row.value.strip() else row for row in discharge_rows),
    )


def scan_discharge_summary(
    folder: Path,
    *,
    normalizer: DocumentNormalizer | None = None,
    scan_session=None,
    admission_datetime_override: datetime | None = None,
    discharge_datetime_override: datetime | None = None,
) -> DischargeSummaryDraft:
    folder = folder.resolve()
    source_scan = scan_source_documents(folder, normalizer=normalizer, session=scan_session)
    selection = select_discharge_sources(source_scan)
    discharge = selection.discharge
    primary = selection.primary
    episode_key = selection.episode_key
    if episode_key is not None:
        episode_key = replace(
            episode_key,
            admission_at=admission_datetime_override or episode_key.admission_at,
            discharge_at=discharge_datetime_override or episode_key.discharge_at,
        )
        if (episode_key.admission_at and episode_key.discharge_at
                and episode_key.discharge_at < episode_key.admission_at):
            raise ValueError("Дата выписки не может быть раньше поступления.")
    selected_record = selection.medical_record_number
    final_boundary = (
        datetime.combine(episode_key.discharge_at.date(), time.max)
        if episode_key is not None and episode_key.discharge_at is not None
        else None
    )
    projection_issues: list[ReviewIssue] = []
    episode_source_scan = source_scan_for_episode(
        source_scan,
        episode_key,
        issues=projection_issues,
    )
    episode = scan_patient_folder(
        folder,
        normalizer=normalizer,
        final_meeting_at=final_boundary,
        medical_record_number_override=selected_record,
        admission_datetime_override=(
            episode_key.admission_at if episode_key is not None else None
        ),
        source_scan=episode_source_scan,
    )
    complete_identity(episode, discharge)
    issues = [
        *selection.issues,
        *projection_issues,
        *_copy_episode_issues(
            episode.issues,
            record_number_selected_from_sources=selected_record is not None,
        ),
    ]
    final_mdrk = (
        select_final_mdrk_document(
            source_scan,
            episode_key=episode_key,
            issues=issues,
        )
        if episode_key is not None
        else None
    )
    if final_mdrk is not None:
        apply_final_mdrk_document(
            episode,
            final_mdrk,
            discharge_scale_values=(
                extract_discharge_scale_values(discharge.scanned.document)
                if discharge
                else {}
            ),
            issues=issues,
            discharge_source=discharge.path if discharge else None,
        )
    snapshot = build_snapshot(episode, MdrkKind.FINAL)

    if discharge is None:
        issues.append(
            ReviewIssue(
                "discharge_summary_source_missing",
                "Не найден выписной эпикриз текущей госпитализации.",
                ReviewSeverity.WARNING,
                "discharge_source",
            )
        )
    if primary is None:
        issues.append(
            ReviewIssue(
                "primary_neurologist_source_missing",
                "Не найден первичный осмотр лечащего врача-невролога.",
                ReviewSeverity.BLOCKING,
                "primary_neurologist_source",
            )
        )

    discharge_document = discharge.scanned.document if discharge else None
    primary_document = primary.scanned.document if primary else None
    primary_sections = extract_discharge_clinical_sections(primary_document) if primary_document else {}
    discharge_at = discharge_document_datetime(discharge_datetime_override or (discharge.discharge_at if discharge else None))

    header_text = extract_discharge_header(discharge_document) if discharge_document else episode_header(episode)
    if discharge_at is not None and header_text.strip():
        header_text = update_header_period(header_text, episode.admission_datetime, discharge_at)
    from mdrk_builder.application.diagnosis import compose_diagnosis, diagnosis_choices
    diagnosis_candidates = []
    if primary:
        diagnosis_candidates.append((primary.path, primary_sections.get("clinical_diagnosis", "")))
    if discharge_document and not any(i.severity is ReviewSeverity.BLOCKING for i in selection.issues):
        diagnosis_candidates.append((discharge.path, extract_discharge_clinical_sections(discharge_document).get("clinical_diagnosis", "")))
    if not any(i.severity is ReviewSeverity.BLOCKING for i in selection.issues):
        used_paths = {path for path, _ in diagnosis_candidates}
        for item in episode_source_scan.documents:
            if item.document.source_path in used_paths or item.classification.is_generated_output:
                continue
            if item.classification.role in {SpecialistRole.NEUROLOGIST, SpecialistRole.FRM}:
                text = extract_discharge_clinical_sections(item.document).get('clinical_diagnosis', '')
                if text:
                    diagnosis_candidates.append((item.document.source_path, text))
    clinical_diagnosis, diagnosis_sources, diagnosis_issues = compose_diagnosis(diagnosis_candidates)
    issues.extend(diagnosis_issues)
    choices = diagnosis_choices(diagnosis_candidates)
    from mdrk_builder.application.icf_conflicts import icf_choices, icf_conflict_issues
    choices.update(icf_choices(snapshot.icf_domains))
    issues.extend(icf_conflict_issues(snapshot.icf_domains))
    from mdrk_builder.application.conflicts import scale_conflicts
    for rows in scale_conflicts(episode):
        key = 'scale:' + rows[0].specialist.value + '|' + rows[0].measured_at.isoformat() + '|' + rows[0].name
        choices[key] = [(r.value, r.source) for r in rows]
        issues.append(ReviewIssue('scale_source_conflict', 'Разные значения одной оценки: '+rows[0].name, ReviewSeverity.WARNING, key, rows[0].source))
    if discharge is not None and not header_text:
        issues.append(
            ReviewIssue(
                "discharge_header_missing",
                "В текущем выписном эпикризе не удалось выделить паспортную шапку.",
                ReviewSeverity.BLOCKING,
                "header_text",
                discharge.path,
            )
        )
    if primary is not None and not clinical_diagnosis:
        issues.append(
            ReviewIssue(
                "primary_clinical_diagnosis_missing",
                "В первичном осмотре невролога не найден заключительный диагноз.",
                ReviewSeverity.BLOCKING,
                "clinical_diagnosis",
                primary.path,
            )
        )
    if discharge is not None and discharge_at is None:
        issues.append(
            ReviewIssue(
                "discharge_datetime_missing",
                "В текущем выписном эпикризе не найдена дата выписки.",
                ReviewSeverity.BLOCKING,
                "discharge_datetime",
                discharge.path,
            )
        )

    complaints = extract_complaints(primary_document) if primary_document else ""
    provided_documents = (
        extract_provided_documents(primary_document) if primary_document else ""
    )
    physical_exam = extract_physical_exam(primary_document) if primary_document else ""
    neurological_status = (
        extract_neurological_status(primary_document) if primary_document else ""
    )
    local_status = extract_local_status(primary_document) if primary_document else ""
    laboratory_results = (
        extract_laboratory_results(discharge_document) if discharge_document else ""
    )
    instrumental_results = (
        extract_instrumental_results(discharge_document) if discharge_document else ""
    )
    examination_summary = (
        extract_medical_examination_summary(discharge_document)
        if discharge_document
        else ""
    )
    if examination_summary.casefold().startswith("не провод"):
        laboratory_results = laboratory_results or examination_summary
        instrumental_results = instrumental_results or examination_summary
    other_consultations = (
        _consultation_texts(
            discharge,
            admission_at=episode.admission_datetime,
            discharge_at=discharge_at,
            issues=issues,
        )
        if discharge
        else ""
    )
    extracted_radiation_exposure = (
        extract_radiation_exposure(discharge_document) if discharge_document else ""
    )
    radiation_exposure = extracted_radiation_exposure or "0 мЗв"
    signatures = extract_signature_block(discharge_document) if discharge_document else ""
    source_signatures = bool(signatures)
    if not signatures and primary is not None:
        doctor = extract_specialist_name(primary_document, primary.scanned.classification.role)
        signatures = "Лечащий врач: " + doctor + "\nЗаведующий отделением: Поляев Б.Б."

    field_sources = {key: value for key, value in episode.field_sources.items()
                     if key.startswith("identity.") or key == "admission_datetime"}
    if discharge is not None and discharge_at is not None:
        field_sources["discharge_datetime"] = discharge.path
    primary_values = {
        "clinical_diagnosis": clinical_diagnosis,
        "complaints": complaints,
        "disease_history": primary_sections.get("disease_history", ""),
        "life_history": primary_sections.get("life_history", ""),
        "provided_documents": provided_documents,
        "physical_exam": physical_exam,
        "neurological_status": neurological_status,
        "local_status": local_status,
        "movement_regimen": primary_sections.get("movement_regimen", ""),
        "diet": primary_sections.get("diet", ""),
        "risks": primary_sections.get("risks", ""),
        "limitations": primary_sections.get("limitations", ""),
    }
    for field_name, value in primary_values.items():
        _field_source(field_sources, field_name, value, primary)
    final_values = extract_discharge_final_fields(discharge_document) if discharge_document else {}
    discharge_values = {
        **final_values,
        "header_text": header_text,
        "laboratory_results": laboratory_results,
        "instrumental_results": instrumental_results,
        "other_consultations": other_consultations,
        "signatures": signatures,
    }
    for field_name, value in discharge_values.items():
        _field_source(field_sources, field_name, value, discharge)
    if not source_signatures:
        field_sources.pop("signatures", None)
        if primary is not None and doctor:
            field_sources["signatures.treating_physician"] = primary.path
    _field_source(
        field_sources,
        "radiation_exposure",
        extracted_radiation_exposure,
        discharge,
    )
    imported_goal = ""
    if (final_mdrk is not None
            and episode.field_sources.get("sections.goal") == final_mdrk.document.source_path):
        imported_goal = episode.sections.goal
        field_sources["goal_result"] = final_mdrk.document.source_path
    if episode.procedures and episode.procedures[0].source is not None:
        field_sources["completed_program"] = episode.procedures[0].source

    # Prefer the motor specialist's own measurements to physician copies.
    motor_names = {canonical_scale_name(row.name) for row in snapshot.scale_rows
                   if row.role is SpecialistRole.PHYSICAL_THERAPIST}
    snapshot = replace(snapshot, scale_rows=tuple(
        row for row in snapshot.scale_rows
        if row.role not in {SpecialistRole.FRM, SpecialistRole.NEUROLOGIST}
        or canonical_scale_name(row.name) not in motor_names
    ))

    admission_scale_rows, discharge_scale_rows = _project_scale_rows(
        snapshot,
        final_mdrk_source=(
            final_mdrk.document.source_path if final_mdrk is not None else None
        ),
        discharge_source=discharge.path if discharge else None,
    )
    if final_mdrk is not None:
        final_path = final_mdrk.document.source_path
        if snapshot.icf_domains and all(
            domain.initial_source == final_path
            and (domain.final is None or domain.final_source == final_path)
            for domain in snapshot.icf_domains
        ):
            field_sources["rehabilitation_diagnosis"] = final_path
    scale_sources = {row.source for row in discharge_scale_rows if row.value.strip()}
    if len(scale_sources) == 1 and None not in scale_sources:
        field_sources["discharge_scales"] = next(iter(scale_sources))
    field_sources.update(diagnosis_sources)
    from mdrk_builder.application.procedures import select_procedures
    completed_procedures = select_procedures(episode.procedures, episode.admission_datetime, discharge_at)
    from mdrk_builder.application.discharge_current_fields import select_current_fields, CURRENT_FIELDS
    current_values, current_sources, current_choices, current_issues = select_current_fields(
        episode_source_scan.documents, episode.admission_datetime, discharge_at,
        discharge.path if discharge else None)
    for key in CURRENT_FIELDS:
        if key in current_values:
            primary_values[key] = current_values[key]
    final_values.update({key: value for key, value in current_values.items() if key not in CURRENT_FIELDS})
    field_sources.update(current_sources)
    if not current_values.get("rehabilitation_potential"):
        field_sources.pop("rehabilitation_potential", None)
    choices.update(current_choices)
    issues.extend(current_issues)
    examination_sections = []
    for candidate in (primary, discharge):
        if candidate is None:
            continue
        document = candidate.scanned.document
        for name, extract in (
            ("provided_documents", extract_provided_documents),
            ("laboratory_results", extract_laboratory_results),
            ("instrumental_results", extract_instrumental_results),
        ):
            value = extract(document)
            if candidate is discharge and not value:
                value = discharge_values.get(name, "")
            examination_sections.append(ExaminationSection(name, value, candidate.path))
    examination_fields, examination_sources, examination_issues = partition_examinations(
        examination_sections, episode.admission_datetime, discharge_at)
    for name in examination_fields:
        field_sources.pop(name, None)
    field_sources.update(examination_sources)
    issues.extend(examination_issues)

    generated_output_paths = {
        scanned.document.source_path.resolve()
        for scanned in source_scan.documents
        if scanned.classification.is_generated_output
    }

    draft = DischargeSummaryDraft(
        folder=folder,
        conflict_choices=choices,
        identity=_copy_identity(episode.identity),
        admission_datetime=episode.admission_datetime,
        discharge_datetime=discharge_at,
        projection_period=(episode.admission_datetime, discharge_at),
        manual_fields=({"discharge_datetime", "header_text"} if discharge_datetime_override else set())
            | ({"admission_datetime", "header_text"} if admission_datetime_override else set()),
        source_paths=tuple(
            path.resolve()
            for path in source_scan.source_files
            if path.resolve() not in generated_output_paths
        ),
        discharge_source=discharge.path if discharge else None,
        primary_neurologist_source=primary.path if primary else None,
        final_mdrk_source=(
            final_mdrk.document.source_path if final_mdrk is not None else None
        ),
        team_findings=_project_team_findings(snapshot, episode.sources),
        initial_assessment_datetime=(extract_clinical_datetime(primary_document) if primary_document else None),
        icf_domains=tuple(replace(domain) for domain in snapshot.icf_domains),
        completed_procedures=tuple(completed_procedures),
        admission_scale_rows=admission_scale_rows,
        discharge_scale_rows=discharge_scale_rows,
        header_text=header_text,
        clinical_diagnosis=clinical_diagnosis,
        complaints=complaints,
        disease_history=primary_values["disease_history"],
        life_history=primary_values["life_history"],
        provided_documents=examination_fields["provided_documents"],
        physical_exam=physical_exam,
        neurological_status=neurological_status,
        local_status=local_status,
        laboratory_results=examination_fields["laboratory_results"],
        instrumental_results=examination_fields["instrumental_results"],
        other_consultations=other_consultations,
        medications=final_values.get("medications") or "",
        movement_regimen=primary_values["movement_regimen"],
        diet=primary_values["diet"],
        transfusions=final_values.get("transfusions") or "",
        operations=final_values.get("operations", ""),
        additional_information=final_values.get("additional_information", ""),
        discharge_condition=final_values.get("discharge_condition", ""),
        discharge_neurological_status=final_values.get("discharge_neurological_status") or "",
        risks=primary_values["risks"],
        limitations=primary_values["limitations"],
        rehabilitation_potential=current_values.get("rehabilitation_potential") or "средний",
        goal_result=final_values.get("goal_result") or imported_goal or "достигнут в полном объёме",
        work_capacity=final_values.get("work_capacity", ""),
        radiation_exposure=radiation_exposure,
        recommendations=final_values.get("recommendations", ""),
        signatures=signatures,
        field_sources=dict(field_sources),
        issues=[replace(issue) for issue in issues],
    )
    omit_admission_scales_from_discharge(draft)
    return draft
