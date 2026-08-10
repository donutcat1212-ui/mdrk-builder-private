from __future__ import annotations

from dataclasses import replace

from mdrk_builder.application.snapshot import select_findings, select_scale_rows
from mdrk_builder.domain import Episode, MdrkKind, ReviewIssue, ReviewSeverity, SpecialistRole


_RECOMPUTED_CODES = {
    "required_full_name",
    "required_record_number",
    "required_admission_datetime",
    "required_diagnosis",
    "required_meeting_datetime",
    "required_physician_source",
    "meeting_before_admission",
    "discharge_before_admission",
    "meeting_after_discharge",
    "final_meeting_not_after_initial",
    "icf_incomplete_pair",
    "icf_initial_missing",
    "icf_final_missing",
    "procedure_specialist_missing",
    "procedure_count_missing",
    "procedure_duration_missing",
    "procedure_frequency_missing",
    "participant_latest_source_not_extracted",
    "participant_finding_missing",
    "participant_conclusion_missing",
    "scale_initial_missing",
    "scale_final_missing",
    "source_selection_stale_record_number",
    "source_selection_stale_admission",
}

ACKNOWLEDGEABLE_CONFLICT_CODES = frozenset(
    {
        "identity_conflict_medical_record_number",
        "mixed_hospitalizations_admission_date",
    }
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


def _conflict_fingerprint(episode: Episode, code: str) -> str | None:
    if code == "identity_conflict_medical_record_number":
        value = _normalized_record_number(episode.identity.medical_record_number)
        return value or None
    if code == "mixed_hospitalizations_admission_date":
        value = episode.admission_datetime
        return value.isoformat(timespec="minutes") if value is not None else None
    return None


def _conflict_display_value(episode: Episode, code: str) -> str:
    if code == "identity_conflict_medical_record_number":
        return episode.identity.medical_record_number.strip()
    if code == "mixed_hospitalizations_admission_date":
        value = episode.admission_datetime
        return value.strftime("%d.%m.%Y %H:%M") if value is not None else ""
    return ""


def acknowledge_conflict(episode: Episode, code: str) -> None:
    """Acknowledge one safe-to-override source conflict for its current value."""

    if code not in ACKNOWLEDGEABLE_CONFLICT_CODES:
        raise ValueError("Эту блокирующую проблему нельзя подтвердить вручную")
    if (
        code == "identity_conflict_medical_record_number"
        and episode.materialized_medical_record_number
        and _normalized_record_number(episode.identity.medical_record_number)
        != _normalized_record_number(episode.materialized_medical_record_number)
    ):
        raise ValueError(
            "Номер ИБ изменён. Нажмите «Сканировать», чтобы заново собрать эпизод "
            "по этому номеру, и затем подтвердите конфликт."
        )
    if (
        code == "mixed_hospitalizations_admission_date"
        and episode.materialized_admission_datetime is not None
        and episode.admission_datetime != episode.materialized_admission_datetime
    ):
        raise ValueError(
            "Дата поступления изменена. Нажмите «Сканировать», чтобы пересчитать даты эпизода, "
            "и затем подтвердите конфликт."
        )
    fingerprint = _conflict_fingerprint(episode, code)
    if fingerprint is None:
        raise ValueError("Сначала заполните проверенное значение вручную")
    episode.acknowledged_conflicts[code] = fingerprint


def clear_conflict_acknowledgements(episode: Episode) -> None:
    episode.acknowledged_conflicts.clear()


def is_conflict_acknowledged(episode: Episode, code: str) -> bool:
    fingerprint = _conflict_fingerprint(episode, code)
    return (
        code in ACKNOWLEDGEABLE_CONFLICT_CODES
        and fingerprint is not None
        and episode.acknowledged_conflicts.get(code) == fingerprint
    )


def _apply_conflict_acknowledgement(
    episode: Episode,
    issue: ReviewIssue,
) -> ReviewIssue:
    if (
        issue.severity is not ReviewSeverity.BLOCKING
        or not is_conflict_acknowledged(episode, issue.code)
    ):
        return issue
    selected_value = _conflict_display_value(episode, issue.code)
    return replace(
        issue,
        severity=ReviewSeverity.WARNING,
        message=(
            f"Подтверждено вручную: использовать «{selected_value}». "
            f"Исходный конфликт сохранён: {issue.message}"
        ),
    )


def generation_issues(episode: Episode, kind: MdrkKind) -> list[ReviewIssue]:
    """Validate the current, possibly manually edited episode state.

    Scanner issues describe extraction. These issues describe whether the current
    UI state can be rendered and are deliberately recomputed after every edit.
    """

    issues: list[ReviewIssue] = []
    sections = episode.initial_sections if kind is MdrkKind.INITIAL else episode.sections
    sections_prefix = "initial_sections" if kind is MdrkKind.INITIAL else "sections"
    required = (
        ("required_full_name", episode.identity.full_name, "ФИО пациента", "identity.full_name"),
        (
            "required_record_number",
            episode.identity.medical_record_number,
            "номер ИБ",
            "identity.medical_record_number",
        ),
        (
            "required_admission_datetime",
            episode.admission_datetime,
            "дата поступления",
            "admission_datetime",
        ),
        (
            "required_diagnosis",
            sections.clinical_diagnosis,
            "клинический диагноз",
            f"{sections_prefix}.clinical_diagnosis",
        ),
        (
            "required_meeting_datetime",
            episode.meeting_at(kind),
            "дата и время заседания",
            "meeting_at",
        ),
    )
    for code, value, label, field in required:
        if value:
            continue
        issues.append(
            ReviewIssue(
                code=code,
                message=f"Не заполнено обязательное поле: {label}",
                severity=ReviewSeverity.BLOCKING,
                field=field,
            )
        )

    if (
        episode.materialized_medical_record_number
        and _normalized_record_number(episode.identity.medical_record_number)
        != _normalized_record_number(episode.materialized_medical_record_number)
    ):
        issues.append(
            ReviewIssue(
                code="source_selection_stale_record_number",
                message=(
                    "Номер ИБ изменён после сканирования. Нажмите «Сканировать», чтобы "
                    "заново отобрать источники и клинические данные."
                ),
                severity=ReviewSeverity.BLOCKING,
                field="identity.medical_record_number",
            )
        )
    if (
        episode.materialized_admission_datetime is not None
        and episode.admission_datetime != episode.materialized_admission_datetime
    ):
        issues.append(
            ReviewIssue(
                code="source_selection_stale_admission",
                message=(
                    "Дата поступления изменена после сканирования. Нажмите «Сканировать», чтобы "
                    "пересчитать границы эпизода и заново отобрать данные."
                ),
                severity=ReviewSeverity.BLOCKING,
                field="admission_datetime",
            )
        )

    selected_meeting = episode.meeting_at(kind)
    if (
        selected_meeting is not None
        and episode.admission_datetime is not None
        and selected_meeting < episode.admission_datetime
    ):
        issues.append(
            ReviewIssue(
                code="meeting_before_admission",
                message="Время заседания указано раньше поступления",
                severity=ReviewSeverity.BLOCKING,
                field="meeting_at",
            )
        )
    if (
        episode.admission_datetime is not None
        and episode.discharge_datetime is not None
        and episode.discharge_datetime < episode.admission_datetime
    ):
        issues.append(
            ReviewIssue(
                code="discharge_before_admission",
                message="Дата выписки указана раньше даты поступления",
                severity=ReviewSeverity.BLOCKING,
                field="admission_datetime",
            )
        )
    if (
        selected_meeting is not None
        and episode.discharge_datetime is not None
        and selected_meeting > episode.discharge_datetime
    ):
        issues.append(
            ReviewIssue(
                code="meeting_after_discharge",
                message="Время заседания указано позже выписки",
                severity=ReviewSeverity.BLOCKING,
                field="meeting_at",
            )
        )
    if (
        kind is MdrkKind.FINAL
        and episode.initial_meeting_at is not None
        and episode.final_meeting_at is not None
        and episode.final_meeting_at <= episode.initial_meeting_at
    ):
        issues.append(
            ReviewIssue(
                code="final_meeting_not_after_initial",
                message="Итоговое заседание должно быть позже первичного",
                severity=ReviewSeverity.BLOCKING,
                field="final_meeting_at",
            )
        )

    if not any(
        source.role in {SpecialistRole.FRM, SpecialistRole.NEUROLOGIST}
        for source in episode.sources
        if episode.source_is_active(source)
    ):
        issues.append(
            ReviewIssue(
                code="required_physician_source",
                message="Не найден читаемый документ врача ФРМ или невролога",
                severity=ReviewSeverity.BLOCKING,
                field="sources",
            )
        )

    boundary = episode.meeting_at(kind)
    selected_findings = {
        finding.role: finding for finding in select_findings(episode, boundary)
    }
    participating_sources = [
        source
        for source in episode.sources
        if episode.source_is_active(source)
        if source.role is not SpecialistRole.OTHER
        and (
            source.clinical_datetime is None
            or boundary is None
            or source.clinical_datetime <= boundary
        )
    ]
    for role in dict.fromkeys(source.role for source in participating_sources):
        role_sources = [source for source in participating_sources if source.role is role]
        dated_sources = [source for source in role_sources if source.clinical_datetime is not None]
        latest_source = (
            max(dated_sources, key=lambda item: item.clinical_datetime)
            if dated_sources
            else role_sources[-1]
        )
        source = latest_source.path
        finding = selected_findings.get(role)
        if (
            finding is not None
            and finding.source_datetime is not None
            and latest_source.clinical_datetime is not None
            and latest_source.clinical_datetime > finding.source_datetime
        ):
            issues.append(
                ReviewIssue(
                    code="participant_latest_source_not_extracted",
                    message=(
                        f"Для участника «{role.display_name}» найден более новый "
                        "документ, но из него не извлечены заключение или шкалы. "
                        "В МДРК используются более ранние данные; проверьте вручную."
                    ),
                    severity=ReviewSeverity.WARNING,
                    field=f"findings.{role.value}",
                    source=source,
                )
            )
        if finding is not None and finding.conclusion.strip():
            continue
        if finding is not None:
            issues.append(
                ReviewIssue(
                    code="participant_conclusion_missing",
                    message=(
                        f"Для участника «{role.display_name}» извлечены шкалы, но не заключение. "
                        "Проверьте вкладку «Заключения»."
                    ),
                    severity=ReviewSeverity.WARNING,
                    field=f"findings.{role.value}.conclusion",
                    source=finding.source or source,
                )
            )
            continue
        issues.append(
            ReviewIssue(
                code="participant_finding_missing",
                message=(
                    f"Для участника «{role.display_name}» найден исходный документ, "
                    "но не извлечены заключение или шкалы. Проверьте вкладку «Заключения»."
                ),
                severity=ReviewSeverity.WARNING,
                field=f"findings.{role.value}",
                source=source,
            )
        )

    review_fields = [
        ("disease_history", "анамнез заболевания"),
        ("life_history", "анамнез жизни"),
        ("rehabilitation_potential", "реабилитационный потенциал"),
        ("limitations", "ограничивающие факторы"),
        ("risks", "факторы риска"),
        ("movement_regimen", "двигательный режим"),
        ("diet", "диета"),
    ]
    if kind is MdrkKind.INITIAL:
        review_fields.extend((("goal", "цель"), ("tasks", "задачи")))
    for field_name, label in review_fields:
        if getattr(sections, field_name).strip():
            continue
        issues.append(
            ReviewIssue(
                code=f"review_{field_name}_missing",
                message=f"Не найдено поле «{label}». Проверьте и при необходимости заполните вручную.",
                severity=ReviewSeverity.WARNING,
                field=f"{sections_prefix}.{field_name}",
            )
        )

    visible_icf_domains = (
        [
            domain
            for domain in episode.icf_domains
            if domain.initial is not None or domain.initial_source is not None
        ]
        if kind is MdrkKind.INITIAL
        else episode.icf_domains
    )
    for index, domain in enumerate(visible_icf_domains):
        # Personal factors are descriptive rows (for example age/motivation),
        # not numeric ICF qualifier pairs.
        if domain.code.strip().casefold().startswith("pf"):
            continue
        if domain.initial is None:
            issues.append(
                ReviewIssue(
                    code="icf_initial_missing",
                    message=f"У домена {domain.code} отсутствует исходная оценка",
                    severity=ReviewSeverity.WARNING,
                    field=f"icf.{index}.initial",
                    source=domain.initial_source or domain.final_source,
                )
            )
        if kind is MdrkKind.FINAL and domain.final is None:
            issues.append(
                ReviewIssue(
                    code="icf_final_missing",
                    message=f"У домена {domain.code} отсутствует повторная оценка",
                    severity=ReviewSeverity.WARNING,
                    field=f"icf.{index}.final",
                    source=domain.final_source or domain.initial_source,
                )
            )

    for index, row in enumerate(select_scale_rows(episode, kind)):
        if row.initial is None:
            issues.append(
                ReviewIssue(
                    code="scale_initial_missing",
                    message=f"У шкалы «{row.name}» отсутствует исходное значение",
                    severity=ReviewSeverity.WARNING,
                    field=f"scales.{index}.initial",
                    source=row.current.source if row.current else None,
                )
            )
        if kind is MdrkKind.FINAL and row.current is None:
            issues.append(
                ReviewIssue(
                    code="scale_final_missing",
                    message=f"У шкалы «{row.name}» отсутствует повторное значение",
                    severity=ReviewSeverity.WARNING,
                    field=f"scales.{index}.final",
                    source=row.initial.source if row.initial else None,
                )
            )
    for index, procedure in enumerate(episode.procedures):
        checks = (
            ("procedure_specialist_missing", procedure.specialist, "ответственный специалист"),
            ("procedure_count_missing", procedure.actual_count, "фактическое количество"),
            ("procedure_duration_missing", procedure.duration_minutes, "длительность"),
            ("procedure_frequency_missing", procedure.frequency, "кратность"),
        )
        for code, value, label in checks:
            present = value is not None if code in {
                "procedure_count_missing",
                "procedure_duration_missing",
            } else bool(value)
            if present:
                continue
            issues.append(
                ReviewIssue(
                    code=code,
                    message=f"Для «{procedure.name}» не заполнено поле: {label}",
                    severity=ReviewSeverity.WARNING,
                    field=f"procedures.{index}",
                    source=procedure.source,
                )
            )
    return issues


def current_issues(episode: Episode, kind: MdrkKind) -> list[ReviewIssue]:
    """Combine stable extraction issues with validation of current UI values."""

    stable = [
        _apply_conflict_acknowledgement(episode, issue)
        for issue in episode.issues
        if issue.code not in _RECOMPUTED_CODES
        and not issue.code.startswith("required_")
        and not issue.code.startswith("review_")
    ]
    return [*stable, *generation_issues(episode, kind)]


def can_generate(episode: Episode, kind: MdrkKind) -> bool:
    return not any(
        issue.severity is ReviewSeverity.BLOCKING for issue in current_issues(episode, kind)
    )
