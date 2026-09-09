"""Select explicitly documented current fields, keeping admission prose separate."""
from collections import defaultdict
from mdrk_builder.application.clinical_text import is_empty_clinical_update
from mdrk_builder.application.discharge_extractors import (
    extract_discharge_clinical_sections, extract_discharge_final_fields,
    extract_physical_exam, extract_neurological_status,
)
from mdrk_builder.application.extractors import extract_clinical_datetime, extract_clinical_sections, extract_mdrk_document_datetime
from mdrk_builder.domain import MdrkKind, ReviewIssue, ReviewSeverity, SpecialistRole

CURRENT_FIELDS = ('movement_regimen', 'diet', 'risks', 'limitations', 'rehabilitation_potential')


def select_current_fields(documents, admission, discharge, mis_path=None):
    candidates = defaultdict(list)
    for item in documents:
        classification = item.classification
        if classification.is_generated_output:
            continue
        if classification.role not in {SpecialistRole.NEUROLOGIST, SpecialistRole.FRM} and item.document.source_path != mis_path and not classification.is_mdrk:
            continue
        stamp = discharge if item.document.source_path == mis_path else extract_mdrk_document_datetime(item.document) if classification.is_mdrk else extract_clinical_datetime(item.document)
        if stamp is None or (admission and stamp.date() < admission.date()) or (discharge and stamp.date() > discharge.date()):
            continue
        sections = extract_clinical_sections(item.document)
        sections.update({key: value for key, value in extract_discharge_clinical_sections(item.document).items() if value})
        values = {key: sections.get(key, '') for key in CURRENT_FIELDS}
        # These extractors require explicit discharge headings; a medication list
        # in life history or an admission examination cannot become final advice.
        values.update(extract_discharge_final_fields(item.document, final_context=classification.document_type == 'final' or item.document.source_path == mis_path))
        if classification.document_type in {"follow_up", "final", "consultation"}:
            values.setdefault("discharge_condition", extract_physical_exam(item.document))
            values.setdefault("discharge_neurological_status", extract_neurological_status(item.document))
        if classification.is_mdrk:
            values = {'rehabilitation_potential': sections.get('rehabilitation_potential', '')}
        if classification.document_type == "initial" or classification.mdrk_kind is MdrkKind.INITIAL:
            values.pop("rehabilitation_potential", None)
        for key, value in values.items():
            if value and not is_empty_clinical_update(value):
                candidates[key].append((stamp, value, item.document.source_path))
    values, sources, choices, issues = {}, {}, {}, []
    for key, rows in candidates.items():
        latest_day = max(stamp.date() for stamp, _, _ in rows)
        latest = [(value, path) for stamp, value, path in sorted(rows, key=lambda row: row[0], reverse=True)
                  if stamp.date() == latest_day]
        distinct = {' '.join(value.casefold().split()) for value, _ in latest}
        value, path = latest[0]
        values[key], sources[key] = value, path
        if len(distinct) > 1:
            choices[key] = list(dict.fromkeys(latest))
            issues.append(ReviewIssue('current_field_conflict',
                f'Разные сведения в итоговых источниках: {key}. Выберите вариант через «Правка → Согласовать расхождения».',
                ReviewSeverity.WARNING, key, path))
    return values, sources, choices, issues
