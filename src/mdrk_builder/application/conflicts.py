"""Explicit alternatives for contradictory observations of the same assessment."""
from collections import defaultdict
from mdrk_builder.application.scale_registry import canonical_scale_name
from mdrk_builder.domain import ReviewIssue, ReviewSeverity


def scale_conflicts(episode):
    groups=defaultdict(list)
    for finding in episode.findings:
        for measurement in finding.scales:
            if measurement.measured_at is not None:
                groups[(measurement.specialist,canonical_scale_name(measurement.name),measurement.measured_at)].append(measurement)
    return [rows for rows in groups.values() if len({r.value.strip().casefold() for r in rows})>1 and not any('value' in r.manual_fields for r in rows)]


def conflict_issues(episode):
    from mdrk_builder.application.icf_conflicts import icf_conflict_issues
    return icf_conflict_issues(episode.icf_domains) + [ReviewIssue('scale_source_conflict', f'«{rows[0].name}» на {rows[0].measured_at:%d.%m.%Y}: ' + '; '.join(f'{r.value} ({r.source.name if r.source else "без источника"})' for r in rows) + '. Выберите вариант через «Правка → Согласовать расхождения».', ReviewSeverity.WARNING, 'scales', rows[0].source) for rows in scale_conflicts(episode)]


def resolve_discharge_scale(draft, key, value, source):
    """Resolve one dated observation, leaving every other date untouched."""
    from dataclasses import replace
    from datetime import datetime
    role, stamp, name = key.removeprefix('scale:').split('|', 2)
    measured_at = datetime.fromisoformat(stamp)

    def same(row):
        return row.role.value == role and canonical_scale_name(row.name) == canonical_scale_name(name)

    for attr in ('admission_scale_rows', 'discharge_scale_rows'):
        setattr(draft, attr, tuple(
            replace(row, value=value, source=source, manual_fields=row.manual_fields | {'value'})
            if same(row) and row.current_at == measured_at else row
            for row in getattr(draft, attr)
        ))
    teams = []
    for team in draft.team_findings:
        scales = []
        for row in team.scales:
            changes = {}
            if same(row):
                if row.initial_at == measured_at:
                    changes.update(initial_value=value, initial_source=source)
                if row.current_at == measured_at:
                    changes.update(value=value, source=source)
            scales.append(replace(row, **changes, manual_fields=row.manual_fields | set(changes)) if changes else row)
        teams.append(replace(team, scales=tuple(scales)))
    draft.team_findings = tuple(teams)
    draft.manual_fields.add(key)
