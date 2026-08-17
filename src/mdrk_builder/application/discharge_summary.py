from __future__ import annotations

from dataclasses import replace
from datetime import datetime, time
from pathlib import Path

from mdrk_builder.application.discharge_defaults import (
    ADDITIONAL_INFORMATION_TEMPLATE,
    DISCHARGE_CONDITION_TEMPLATE,
    OPERATIONS_TEMPLATE,
    RECOMMENDATIONS_TEMPLATE,
    WORK_CAPACITY_TEMPLATE,
)
from mdrk_builder.application.discharge_extractors import (
    extract_complaints,
    extract_discharge_header,
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
from mdrk_builder.application.scanner import scan_patient_folder
from mdrk_builder.application.snapshot import Snapshot, build_snapshot
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


def _project_team_findings(snapshot: Snapshot) -> tuple[DischargeTeamFinding, ...]:
    return tuple(
        DischargeTeamFinding(
            role=finding.role,
            conclusion=finding.conclusion,
            source=finding.source,
        )
        for finding in snapshot.findings
        if finding.role is not SpecialistRole.OTHER and finding.conclusion.strip()
    )


def _project_scale_rows(
    snapshot: Snapshot,
    *,
    final_mdrk_source: Path | None,
) -> tuple[tuple[DischargeScaleRow, ...], tuple[DischargeScaleRow, ...]]:
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
                    and row.initial.source == final_mdrk_source
                )
                else ""
            ),
        )
        for row in snapshot.scale_rows
    )
    discharge_rows = tuple(
        DischargeScaleRow(
            role=row.role,
            name=row.name,
            value=(
                row.current.value
                if final_mdrk_source is not None
                and row.current is not None
                and row.current.source == final_mdrk_source
                else (
                    row.initial.value
                    if final_mdrk_source is not None
                    and row.current is None
                    and row.initial is not None
                    and row.initial.source == final_mdrk_source
                    else ""
                )
            ),
        )
        for row in snapshot.scale_rows
    )
    return admission_rows, discharge_rows


def scan_discharge_summary(
    folder: Path,
    *,
    normalizer: DocumentNormalizer | None = None,
) -> DischargeSummaryDraft:
    folder = folder.resolve()
    source_scan = scan_source_documents(folder, normalizer=normalizer)
    selection = select_discharge_sources(source_scan)
    discharge = selection.discharge
    primary = selection.primary
    episode_key = selection.episode_key
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
        )
    elif not any(
        issue.code == "final_mdrk_source_ambiguous" for issue in issues
    ):
        issues.append(
            ReviewIssue(
                "final_mdrk_source_missing",
                (
                    "Структурно итоговый МДРК-2 не найден. Итоговые поля собраны "
                    "из профильных документов и требуют ручной сверки."
                ),
                ReviewSeverity.WARNING,
                "final_mdrk_source",
            )
        )
    snapshot = build_snapshot(episode, MdrkKind.FINAL)

    if discharge is None:
        issues.append(
            ReviewIssue(
                "discharge_summary_source_missing",
                "Не найден выписной эпикриз текущей госпитализации.",
                ReviewSeverity.BLOCKING,
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
    primary_sections = primary.sections if primary else {}
    discharge_at = discharge.discharge_at if discharge else None

    header_text = extract_discharge_header(discharge_document) if discharge_document else ""
    clinical_diagnosis = primary_sections.get("clinical_diagnosis", "")
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

    field_sources: dict[str, Path] = {}
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
    discharge_values = {
        "header_text": header_text,
        "laboratory_results": laboratory_results,
        "instrumental_results": instrumental_results,
        "other_consultations": other_consultations,
        "signatures": signatures,
    }
    for field_name, value in discharge_values.items():
        _field_source(field_sources, field_name, value, discharge)
    _field_source(
        field_sources,
        "radiation_exposure",
        extracted_radiation_exposure,
        discharge,
    )
    potential_source = episode.field_sources.get("sections.rehabilitation_potential")
    if (
        final_mdrk is not None
        and snapshot.sections.rehabilitation_potential
        and potential_source is not None
    ):
        field_sources["rehabilitation_potential"] = potential_source
    if final_mdrk is not None:
        if episode.sections.goal:
            field_sources["goal_result"] = final_mdrk.document.source_path
    if episode.procedures and episode.procedures[0].source is not None:
        field_sources["completed_program"] = episode.procedures[0].source

    admission_scale_rows, discharge_scale_rows = _project_scale_rows(
        snapshot,
        final_mdrk_source=(
            final_mdrk.document.source_path if final_mdrk is not None else None
        ),
    )
    if final_mdrk is not None:
        final_path = final_mdrk.document.source_path
        if snapshot.icf_domains and all(
            domain.initial_source == final_path
            and (domain.final is None or domain.final_source == final_path)
            for domain in snapshot.icf_domains
        ):
            field_sources["rehabilitation_diagnosis"] = final_path
        if any(row.value.strip() for row in discharge_scale_rows):
            field_sources["discharge_scales"] = final_path
    generated_output_paths = {
        scanned.document.source_path.resolve()
        for scanned in source_scan.documents
        if scanned.classification.is_generated_output
    }

    return DischargeSummaryDraft(
        folder=folder,
        identity=_copy_identity(episode.identity),
        admission_datetime=episode.admission_datetime,
        discharge_datetime=discharge_at,
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
        team_findings=_project_team_findings(snapshot),
        icf_domains=(
            tuple(replace(domain) for domain in snapshot.icf_domains)
            if final_mdrk is not None
            else ()
        ),
        completed_procedures=tuple(
            replace(procedure) for procedure in episode.procedures
        ),
        admission_scale_rows=admission_scale_rows,
        discharge_scale_rows=discharge_scale_rows,
        header_text=header_text,
        clinical_diagnosis=clinical_diagnosis,
        complaints=complaints,
        disease_history=primary_values["disease_history"],
        life_history=primary_values["life_history"],
        provided_documents=provided_documents,
        physical_exam=physical_exam,
        neurological_status=neurological_status,
        local_status=local_status,
        laboratory_results=laboratory_results,
        instrumental_results=instrumental_results,
        other_consultations=other_consultations,
        medications="",
        movement_regimen=primary_values["movement_regimen"],
        diet=primary_values["diet"],
        transfusions="",
        operations=OPERATIONS_TEMPLATE,
        additional_information=ADDITIONAL_INFORMATION_TEMPLATE,
        discharge_condition=DISCHARGE_CONDITION_TEMPLATE,
        discharge_neurological_status="",
        risks=primary_values["risks"],
        limitations=primary_values["limitations"],
        rehabilitation_potential=(
            snapshot.sections.rehabilitation_potential
            if final_mdrk is not None
            else ""
        ),
        goal_result=episode.sections.goal if final_mdrk is not None else "",
        work_capacity=WORK_CAPACITY_TEMPLATE,
        radiation_exposure=radiation_exposure,
        recommendations=RECOMMENDATIONS_TEMPLATE,
        signatures=signatures,
        field_sources=dict(field_sources),
        issues=[replace(issue) for issue in issues],
    )
