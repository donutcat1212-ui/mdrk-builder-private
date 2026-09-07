"""Propagate explicitly edited episode facts, keeping document-specific prose separate."""
from dataclasses import replace
from copy import deepcopy
from mdrk_builder.domain import DischargeTeamFinding, DischargeScaleRow, ScaleMeasurement, SpecialistFinding, MdrkKind
from mdrk_builder.application.editing import merge_rows, merge_issues, row_key
from mdrk_builder.application.scale_registry import canonical_scale_name


def _same_point(left, right):
    return (left.role, canonical_scale_name(left.name), left.current_at, left.source) == (
        right.role, canonical_scale_name(right.name), right.current_at, right.source)


def _pair_point(row, initial):
    return replace(row, value=row.initial_value if initial else row.value,
                   source=row.initial_source if initial else row.source,
                   current_at=row.initial_at if initial else row.current_at)


def synchronize_scale_rows(draft, row):
    for attr, initial in (('admission_scale_rows',True),('discharge_scale_rows',False)):
        rows=list(getattr(draft,attr));value=row.initial_value if initial else row.value
        source=row.initial_source if initial else row.source
        point=replace(row, value=value, source=source, current_at=row.initial_at if initial else row.current_at, initial_value="", initial_source=None, initial_at=None)
        match=next((i for i,r in enumerate(rows) if _same_point(r, point)),None)
        if match is None:rows.append(point)
        else:rows[match]=point
        setattr(draft,attr,tuple(rows))


def transfer_episode_edits(episode, baseline, draft, identity_fields=()):
    if episode is None or draft is None:return
    for name in identity_fields:
        mapped={'record_number':'medical_record_number','full_name':'full_name','birth_date':'birth_date','sex':'sex'}.get(name)
        if mapped:
            setattr(draft.identity,mapped,getattr(episode.identity,mapped));draft.manual_fields.add('identity.'+mapped)
        elif name=='admission':
            draft.admission_datetime=episode.admission_datetime;draft.manual_fields.add('admission_datetime')
    if baseline:
        for attr in ('icf_domains','completed_procedures'):
            source_attr='procedures' if attr=='completed_procedures' else attr
            rows,notes=merge_rows(getattr(baseline,source_attr),getattr(episode,source_attr),getattr(draft,attr))
            setattr(draft,attr,tuple(rows));draft.issues.extend(merge_issues(notes))
    from mdrk_builder.application.snapshot import select_scale_rows
    for pair in select_scale_rows(episode, MdrkKind.FINAL):
        for attr, point in (('admission_scale_rows', pair.initial), ('discharge_scale_rows', pair.current)):
            if point is None or not point.manual_fields:
                continue
            rows = list(getattr(draft, attr))
            index = next((i for i, row in enumerate(rows) if row.role == pair.role and canonical_scale_name(row.name) == canonical_scale_name(pair.name)), None)
            old = rows[index] if index is not None else None
            new = DischargeScaleRow(pair.role, pair.name, point.value, point.source,
                                    current_at=point.measured_at, manual_fields=set(point.manual_fields))
            if index is None:
                rows.append(new)
            else:
                rows[index] = new
            setattr(draft, attr, tuple(rows))
            synchronize_discharge_point(draft, attr, old, new)


def transfer_discharge_edits(draft, episode, identity_fields=(), baseline=None):
    if draft is None or episode is None:return set()
    before = deepcopy(episode)
    for name in identity_fields:
        mapped={'record_number':'medical_record_number','full_name':'full_name','birth_date':'birth_date','sex':'sex'}.get(name)
        if mapped:setattr(episode.identity,mapped,getattr(draft.identity,mapped))
        elif name=='admission':episode.admission_datetime=draft.admission_datetime
        elif name=='discharge':episode.discharge_datetime=draft.discharge_datetime
    if baseline is not None:
        for attr, source_attr in (('icf_domains', 'icf_domains'), ('completed_procedures', 'procedures')):
            rows, notes = merge_rows(getattr(baseline, attr), getattr(draft, attr), getattr(episode, source_attr))
            setattr(episode, source_attr, rows)
            episode.issues.extend(merge_issues(notes))
        for attr in ('admission_scale_rows', 'discharge_scale_rows'):
            edited = {row_key(row): row for row in getattr(draft, attr)}
            for old in getattr(baseline, attr):
                new = edited.get(row_key(old))
                for finding in episode.findings:
                    for scale in list(finding.scales):
                        if (scale.specialist, canonical_scale_name(scale.name), scale.measured_at, scale.source) != (old.role, canonical_scale_name(old.name), old.current_at, old.source):
                            continue
                        if new is None:
                            finding.scales.remove(scale)
                        elif (new.role, canonical_scale_name(new.name)) != (old.role, canonical_scale_name(old.name)):
                            finding.scales.remove(scale)
                        elif new.current_at != old.current_at:
                            scale.measured_at = new.current_at
                            scale.value = new.value
                            scale.manual_fields.update({'value', 'measured_at'})
    else:
        for attr, source_attr in (('icf_domains','icf_domains'),('completed_procedures','procedures')):
            rows=getattr(episode,source_attr)
            for row in getattr(draft,attr):
                if not row.manual_fields:continue
                index=next((i for i,v in enumerate(rows) if row_key(v)==row_key(row)),None)
                if index is None:rows.append(deepcopy(row))
                else:rows[index]=deepcopy(row)
    for finding in episode.findings:
        for scale in finding.scales:
            candidates=[r for r in (*draft.admission_scale_rows,*draft.discharge_scale_rows) if r.manual_fields and r.source==scale.source and canonical_scale_name(r.name)==canonical_scale_name(scale.name) and r.role==scale.specialist and r.current_at==scale.measured_at]
            if candidates:
                scale.value=candidates[-1].value;scale.manual_fields.update({'value'})

    for row in (*draft.admission_scale_rows, *draft.discharge_scale_rows):
        if not row.manual_fields:
            continue
        present = any(scale.specialist == row.role and canonical_scale_name(scale.name) == canonical_scale_name(row.name)
                      and scale.source == row.source and scale.measured_at == row.current_at
                      for finding in episode.findings for scale in finding.scales)
        if present:
            continue
        finding = next((f for f in episode.findings if f.role == row.role and f.source == row.source), None)
        if finding is None:
            finding = SpecialistFinding(row.role, source=row.source, source_datetime=row.current_at)
            episode.findings.append(finding)
        finding.scales.append(ScaleMeasurement(row.name, row.value, row.current_at, row.role, row.source, {'value'}))

    return {label for attr, label in (('icf_domains','icf'),('procedures','procedures'),('findings','findings'))
            if getattr(before, attr) != getattr(episode, attr)}


def synchronize_discharge_point(draft, attr, old, new):
    """Mirror an edited single assessment in its specialist's dynamic table."""
    if old is not None and new is not None and old.role != new.role:
        synchronize_discharge_point(draft, attr, old, None)
        synchronize_discharge_point(draft, attr, None, new)
        return
    role = old.role if old else new.role
    name = old.name if old else new.name
    initial = attr == 'admission_scale_rows'
    teams = list(draft.team_findings)
    index = next((i for i, team in enumerate(teams) if team.role == role and (old is None or any(_same_point(_pair_point(r, initial), old) for r in team.scales))), None)
    if index is None:
        if new is None:
            return
        teams.append(DischargeTeamFinding(role=role, conclusion=''))
        index = len(teams) - 1
    team = teams[index]
    scales = list(team.scales)
    row_index = next((i for i, row in enumerate(scales) if (_same_point(_pair_point(row, initial), old) if old else canonical_scale_name(row.name) == canonical_scale_name(name) and not (_pair_point(row, initial).value))), None)
    row = scales[row_index] if row_index is not None else DischargeScaleRow(role=role, name=name)
    changes = {'initial_value': new.value if new else '', 'initial_source': new.source if new else None,
               'initial_at': new.current_at if new else None} if initial else {
               'value': new.value if new else '', 'source': new.source if new else None,
               'current_at': new.current_at if new else None}
    if new:
        changes.update(name=new.name)
    row = replace(row, **changes, manual_fields=row.manual_fields | set(changes))
    if row_index is None:
        scales.append(row)
    elif not row.value and not row.initial_value and new is None:
        scales.pop(row_index)
    else:
        scales[row_index] = row
    teams[index] = replace(team, scales=tuple(scales))
    draft.team_findings = tuple(teams)


def remove_scale_rows(draft, row):
    for attr, initial in (('admission_scale_rows', True), ('discharge_scale_rows', False)):
        point = _pair_point(row, initial)
        setattr(draft, attr, tuple(r for r in getattr(draft, attr) if not _same_point(r, point)))


def transfer_identity(source, target, fields):
    """Share only explicitly edited identity facts inside one open episode."""
    if source is None or target is None:
        return
    for key in fields:
        name = {'record_number': 'medical_record_number'}.get(key, key)
        if name in {'full_name', 'medical_record_number', 'birth_date', 'sex'}:
            setattr(target.identity, name, getattr(source.identity, name))
        elif key in {'admission', 'discharge'}:
            name = key + '_datetime'
            setattr(target, name, getattr(source, name))
