from __future__ import annotations

from dataclasses import fields, is_dataclass, replace
from datetime import date, datetime
from enum import Enum
from hashlib import sha256
import json
from pathlib import Path
from typing import Any

from mdrk_builder.application.snapshot import (
    select_findings,
    select_icf_domains,
    select_scale_rows,
)
from mdrk_builder.domain import Episode, MdrkKind, ReviewIssue, ReviewSeverity, SpecialistRole


_RECOMPUTED_CODES = {
    "required_full_name",
    "required_record_number",
    "required_admission_datetime",
    "required_diagnosis",
    "required_meeting_datetime",
    "required_physician_source",
    "meeting_before_admission",
    "final_meeting_not_after_initial",
    "icf_incomplete_pair",
    "icf_initial_missing",
    "icf_final_missing",
    "procedure_specialist_missing",
    "procedure_count_missing",
    "procedure_duration_missing",
    "procedure_frequency_missing",
    "rehab_daily_minutes_below_minimum",
    "rehab_daily_minutes_incomplete",
    "participant_latest_source_not_extracted",
    "participant_finding_missing",
    "participant_conclusion_missing",
    "scale_initial_missing",
    "scale_final_missing",
    "scale_datetime_missing",
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


def _canonical_value(value: Any) -> Any:
    """Return stable JSON-compatible state for an issue acknowledgement."""

    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value) and not isinstance(value, type):
        return {
            item.name: _canonical_value(getattr(value, item.name))
            for item in fields(value)
            if item.name not in {"acknowledged", "acknowledgement_key"}
        }
    if isinstance(value, dict):
        return {
            str(key): _canonical_value(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (list, tuple)):
        return [_canonical_value(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return sorted(
            (_canonical_value(item) for item in value),
            key=lambda item: json.dumps(item, ensure_ascii=False, sort_keys=True),
        )
    return value


def _indexed_item(items: list[Any], index_text: str) -> Any:
    try:
        return items[int(index_text)]
    except (ValueError, IndexError):
        return items


def _issue_field_state(episode: Episode, issue: ReviewIssue, kind: MdrkKind) -> Any:
    """Resolve the part of the episode that can change this issue.

    Validation fields are UI-oriented paths rather than direct model paths, so
    the few collection aliases are handled explicitly. Falling back to the
    issue itself is safe: the acknowledgement will still be bound to its text,
    severity, field and source.
    """

    path = issue.field.split(".") if issue.field else []
    if not path:
        return None
    root = path[0]
    if root == "meeting_at":
        return episode.meeting_at(kind)
    if root in {"admission_datetime", "discharge_datetime", "final_meeting_at"}:
        return getattr(episode, root)
    if root in {"identity", "initial_sections", "sections"}:
        value: Any = getattr(episode, root)
        for segment in path[1:]:
            value = getattr(value, segment, value)
        return value
    if root == "sources":
        return (episode.sources, episode.excluded_source_paths)
    if root == "findings":
        return (episode.findings, episode.meeting_at(kind))
    if root == "scales":
        rows = select_scale_rows(episode, kind)
        return _indexed_item(rows, path[1]) if len(path) > 1 else rows
    if root == "icf":
        return (
            _indexed_item(episode.icf_domains, path[1])
            if len(path) > 1
            else episode.icf_domains
        )
    if root == "procedures":
        if len(path) > 1 and path[1].isdigit():
            return _indexed_item(episode.procedures, path[1])
        return episode.procedures
    return getattr(episode, root, None)


def _issue_fingerprint(episode: Episode, issue: ReviewIssue, kind: MdrkKind) -> str:
    payload = {
        "episode": str(episode.folder),
        "kind": kind.value,
        "issue": {
            "code": issue.code,
            "message": issue.message,
            "severity": issue.severity.value,
            "field": issue.field,
            "source": str(issue.source) if issue.source is not None else None,
        },
        "field_state": _canonical_value(_issue_field_state(episode, issue, kind)),
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"{kind.value}:{sha256(encoded).hexdigest()}"


def acknowledge_issue(episode: Episode, issue: ReviewIssue, kind: MdrkKind) -> None:
    """Ignore one current review issue without removing it from the review list."""

    key = issue.acknowledgement_key or _issue_fingerprint(episode, issue, kind)
    episode.acknowledged_issues.add(key)


def clear_issue_acknowledgements(episode: Episode) -> None:
    episode.acknowledged_issues.clear()
    episode.acknowledged_conflicts.clear()


def has_issue_acknowledgements(episode: Episode) -> bool:
    return bool(episode.acknowledged_issues or episode.acknowledged_conflicts)


def is_issue_acknowledged(
    episode: Episode,
    issue: ReviewIssue,
    kind: MdrkKind,
) -> bool:
    if issue.acknowledged:
        return True
    key = issue.acknowledgement_key or _issue_fingerprint(episode, issue, kind)
    return key in episode.acknowledged_issues


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
    clear_issue_acknowledgements(episode)


def is_conflict_acknowledged(episode: Episode, code: str) -> bool:
    fingerprint = _conflict_fingerprint(episode, code)
    return (
        code in ACKNOWLEDGEABLE_CONFLICT_CODES
        and fingerprint is not None
        and episode.acknowledged_conflicts.get(code) == fingerprint
    )


def _apply_issue_acknowledgement(
    episode: Episode,
    issue: ReviewIssue,
    kind: MdrkKind,
) -> ReviewIssue:
    key = _issue_fingerprint(episode, issue, kind)
    acknowledged = key in episode.acknowledged_issues or is_conflict_acknowledged(
        episode, issue.code
    )
    if not acknowledged:
        return issue
    selected_value = _conflict_display_value(episode, issue.code)
    selected_note = f" Использовать «{selected_value}»." if selected_value else ""
    original_note = (
        "Исходный конфликт сохранён"
        if issue.code in ACKNOWLEDGEABLE_CONFLICT_CODES
        else "Исходное предупреждение"
    )
    previous_severity = {
        ReviewSeverity.BLOCKING: "блокирующая проблема",
        ReviewSeverity.WARNING: "предупреждение",
        ReviewSeverity.INFO: "информация",
    }[issue.severity]
    return replace(
        issue,
        severity=ReviewSeverity.INFO,
        message=(
            f"Игнорировано вручную (было: {previous_severity})."
            f"{selected_note} "
            f"{original_note}: {issue.message}"
        ),
        acknowledged=True,
        acknowledgement_key=key,
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

    visible_icf_domains = select_icf_domains(episode, kind)
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
        for point_name, measurement in (
            ("initial", row.initial),
            ("final", row.current if kind is MdrkKind.FINAL else None),
        ):
            if measurement is None or measurement.measured_at is not None:
                continue
            point_label = "исходной" if point_name == "initial" else "повторной"
            issues.append(
                ReviewIssue(
                    code="scale_datetime_missing",
                    message=(
                        f"У {point_label} оценки по шкале «{row.name}» "
                        "не определены дата и время"
                    ),
                    severity=ReviewSeverity.WARNING,
                    field=f"scales.{index}.{point_name}_datetime",
                    source=measurement.source,
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

    daily_minutes: dict[date, int] = {}
    dates_with_unknown_duration: set[date] = set()
    source_by_date: dict[date, Path | None] = {}
    for procedure in episode.procedures:
        for performed_date in procedure.performed_dates:
            if performed_date.weekday() >= 5:
                continue
            source_by_date.setdefault(performed_date, procedure.source)
            if procedure.duration_minutes is None:
                dates_with_unknown_duration.add(performed_date)
                continue
            daily_minutes[performed_date] = (
                daily_minutes.get(performed_date, 0) + procedure.duration_minutes
            )
    deficient = [
        (performed_date, minutes)
        for performed_date, minutes in sorted(daily_minutes.items())
        if performed_date not in dates_with_unknown_duration and minutes < 180
    ]
    if deficient:
        details = ", ".join(
            f"{performed_date.strftime('%d.%m.%Y')} — {minutes} мин"
            for performed_date, minutes in deficient
        )
        issues.append(
            ReviewIssue(
                code="rehab_daily_minutes_below_minimum",
                message=(
                    "Недобор реабилитационных занятий: минимум 180 минут "
                    f"в день. {details}."
                ),
                severity=ReviewSeverity.WARNING,
                field="procedures.daily_minutes",
                source=source_by_date.get(deficient[0][0]),
            )
        )
    if dates_with_unknown_duration:
        details = ", ".join(
            performed_date.strftime("%d.%m.%Y")
            for performed_date in sorted(dates_with_unknown_duration)
        )
        issues.append(
            ReviewIssue(
                code="rehab_daily_minutes_incomplete",
                message=(
                    "Нельзя проверить минимум 180 минут: не указана "
                    f"длительность процедур на {details}."
                ),
                severity=ReviewSeverity.WARNING,
                field="procedures.daily_minutes",
                source=source_by_date.get(next(iter(dates_with_unknown_duration))),
            )
        )
    return issues


def current_issues(episode: Episode, kind: MdrkKind) -> list[ReviewIssue]:
    """Combine stable extraction issues with validation of current UI values."""

    stable = [
        issue for issue in episode.issues
        if issue.code not in _RECOMPUTED_CODES
        and not issue.code.startswith("required_")
        and not issue.code.startswith("review_")
    ]
    raw_issues = [*stable, *generation_issues(episode, kind)]

    # Acknowledgements are state-bound. Once an issue disappears or changes,
    # discard its old key so recreating the issue requires a fresh decision.
    valid_keys = {_issue_fingerprint(episode, issue, kind) for issue in raw_issues}
    kind_prefix = f"{kind.value}:"
    episode.acknowledged_issues = {
        key
        for key in episode.acknowledged_issues
        if not key.startswith(kind_prefix) or key in valid_keys
    }
    return [
        _apply_issue_acknowledgement(episode, issue, kind) for issue in raw_issues
    ]


def can_generate(episode: Episode, kind: MdrkKind) -> bool:
    return not any(
        issue.severity is ReviewSeverity.BLOCKING for issue in current_issues(episode, kind)
    )
