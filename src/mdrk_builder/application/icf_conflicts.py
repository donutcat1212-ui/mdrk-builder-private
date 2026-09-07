"""Pointwise ICF merging with explicit alternatives for the same assessment."""
from dataclasses import replace
import json
from mdrk_builder.domain import IcfQualifier, ReviewIssue, ReviewSeverity


def collect_icf_choices(domain, occurrences):
    for phase in ('initial', 'final'):
        stamp = getattr(domain, phase + '_measured_at')
        if stamp is None or getattr(domain, phase) is None:
            continue
        choices = []
        for record, observation in occurrences:
            if record.clinical_datetime != stamp:
                continue
            value = observation.initial if phase == 'initial' else observation.current
            choice = (value.display(), record.document.source_path) if value is not None else None
            if choice is not None and choice not in choices:
                choices.append(choice)
        if len({value for value, _ in choices}) > 1:
            domain.conflict_choices[phase] = choices


def merge_icf_summary(original, summary):
    merged = replace(original, conflict_choices=dict(original.conflict_choices))
    for phase in ('initial', 'final'):
        value = getattr(summary, phase)
        if value is None or phase in original.manual_fields:
            continue
        existing = getattr(original, phase)
        old_at = getattr(original, phase + '_measured_at')
        new_at = getattr(summary, phase + '_measured_at')
        if existing is not None and old_at == new_at:
            if existing != value:
                choices = list(merged.conflict_choices.get(phase, []))
                for pair in ((existing.display(), getattr(original, phase + '_source')),
                             (value.display(), getattr(summary, phase + '_source'))):
                    if pair not in choices:
                        choices.append(pair)
                merged.conflict_choices[phase] = choices
            continue
        if existing is not None and (phase == 'initial' or new_at is None or (old_at is not None and old_at > new_at)):
            continue
        for suffix in ('', '_source', '_measured_at'):
            setattr(merged, phase + suffix, getattr(summary, phase + suffix))
        merged.conflict_choices.pop(phase, None)
    return merged


def icf_choice_key(domain, phase):
    return 'icf:' + json.dumps([domain.key[0], domain.key[1], domain.specialist.value, phase, str(getattr(domain, phase + '_measured_at'))], ensure_ascii=False)


def icf_choices(domains):
    return {icf_choice_key(domain, phase): choices
            for domain in domains for phase, choices in domain.conflict_choices.items()
            if phase not in domain.manual_fields}


def resolve_icf_choice(domains, key, value, source):
    parts = json.loads(key.removeprefix('icf:'))
    code, description, role, phase = parts[:4]
    for domain in domains:
        if domain.key[:2] == (code, description) and domain.specialist.value == role and (len(parts) == 4 or str(getattr(domain, phase + '_measured_at')) == parts[4]):
            setattr(domain, phase, IcfQualifier(int(value.rstrip('+')), value.endswith('+')))
            setattr(domain, phase + '_source', source)
            domain.manual_fields.add(phase)
            domain.conflict_choices.pop(phase, None)
            return


def icf_conflict_issues(domains):
    return [ReviewIssue('icf_source_conflict',
        f'МКФ {domain.code}: противоречивые оценки на одну дату. Выберите источник через «Правка → Согласовать расхождения».',
        ReviewSeverity.WARNING, icf_choice_key(domain, phase), getattr(domain, phase + '_source'))
        for domain in domains for phase in domain.conflict_choices if phase not in domain.manual_fields]
