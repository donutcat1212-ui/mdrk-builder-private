"""Validation of the current editable discharge, independent of scan-time warnings."""
from mdrk_builder.application.icf_validation import icf_assessment_issues
from mdrk_builder.domain import ReviewIssue, ReviewSeverity
from mdrk_builder.application.scale_registry import scale_bounds, numeric_scale_value


def scale_value_issue(name, value, field, source=None):
    bounds, number = scale_bounds(name), numeric_scale_value(value)
    if bounds and number is not None and not bounds[0] <= number <= bounds[1]:
        return ReviewIssue('scale_value_out_of_range', f'«{name}»: {value}, допустимый диапазон {bounds[0]}–{bounds[1]}.', ReviewSeverity.WARNING, field, source)
    return None


def procedure_issues(rows):
    result = []
    for i, row in enumerate(rows):
        if row.count_needs_review or (row.performed_dates and row.actual_count != len(row.performed_dates)):
            result.append(ReviewIssue('procedure_dates_count_mismatch', f'«{row.name}»: по датам {len(row.performed_dates)}, указано {row.actual_count}. Кратность: {row.frequency or "не указана"}.', ReviewSeverity.WARNING, f'procedures.{i}', row.source))
    return result


def current_discharge_issues(draft):
    dynamic = {'primary_clinical_diagnosis_missing', 'discharge_datetime_missing', 'discharge_header_missing', 'discharge_current_required', 'scale_value_out_of_range', 'procedure_dates_count_mismatch', 'icf_incomplete_pair', 'icf_initial_missing', 'icf_final_missing'}
    # Drafts saved by earlier versions may still contain this retired warning.
    dynamic.add('final_mdrk_source_missing')
    dynamic.add('discharge_summary_source_missing')
    issues = [i for i in draft.issues if i.code not in dynamic and not i.code.startswith(('required_', 'scale_initial_missing', 'scale_final_missing'))]
    for name, label in (('clinical_diagnosis', 'Заключительный диагноз'), ('header_text', 'Шапка'), ('discharge_datetime', 'Дата выписки')):
        value = getattr(draft, name)
        if name == 'clinical_diagnosis':
            from mdrk_builder.application.diagnosis import diagnosis_parts
            value = diagnosis_parts(value)['main']
        if not value or isinstance(value, str) and not value.strip():
            issues.append(ReviewIssue('discharge_current_required', f'Не заполнено: {label}', ReviewSeverity.BLOCKING, name))
    for i, row in enumerate((*draft.admission_scale_rows, *draft.discharge_scale_rows, *(s for f in draft.team_findings for s in f.scales))):
        for value in (row.value, row.initial_value):
            issue = scale_value_issue(row.name, value, f'scales.{i}', row.source)
            if issue:
                issues.append(issue)
    issues.extend(icf_assessment_issues(draft.icf_domains, include_final=True))
    issues.extend(procedure_issues(draft.completed_procedures))
    return issues
