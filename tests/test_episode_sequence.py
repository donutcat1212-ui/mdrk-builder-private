from datetime import date, datetime
from pathlib import Path
from unittest.mock import patch
import pytest

from mdrk_builder.domain import Episode, IcfDomain, IcfQualifier, MdrkKind, Procedure, ScaleMeasurement, SpecialistFinding, SpecialistRole
from mdrk_builder.application.snapshot import build_snapshot, FINAL_GOAL, FINAL_TASKS
from mdrk_builder.application.scanner import _merge_icf, _collect_procedures
from mdrk_builder.application.extractors import IcfObservation
from mdrk_builder.application.final_mdrk import apply_final_mdrk_document
from mdrk_builder.application.procedures import merge_procedures, select_procedures
from mdrk_builder.application.icf_conflicts import icf_choices, resolve_icf_choice, icf_conflict_issues
from mdrk_builder.application.discharge_current_fields import select_current_fields
from mdrk_builder.application.source_scan import ScannedDocument
from mdrk_builder.infrastructure.draft_store import encode, decode
from test_scanner import _record, _document
from test_final_mdrk import _mdrk_document

ROLE = SpecialistRole.PHYSICAL_THERAPIST
D = lambda day: datetime(2026, 9, day, 12)


def episode():
    return Episode(Path('/synthetic'), admission_datetime=D(1), initial_meeting_at=D(2), final_meeting_at=D(10))


def test_late_only_measurement_is_current_and_old_measurement_is_excluded():
    e = episode()
    e.findings = [SpecialistFinding(ROLE, source_datetime=D(6), scales=[
        ScaleMeasurement('Berg', '35', D(6), ROLE),
        ScaleMeasurement('Berg', '10', datetime(2026, 8, 31), ROLE)])]
    row, = build_snapshot(e, MdrkKind.FINAL).scale_rows
    assert row.initial is None and row.current.value == '35'
    assert build_snapshot(e, MdrkKind.INITIAL).scale_rows == ()


def test_import_is_additive_and_partial_icf_does_not_erase_later_points():
    e = episode()
    e.findings = [SpecialistFinding(ROLE, source_datetime=D(9), scales=[ScaleMeasurement('Berg', '48', D(9), ROLE)])]
    e.icf_domains = [IcfDomain('d450', 'Ходьба', ROLE, IcfQualifier(3), IcfQualifier(1), initial_measured_at=D(1), final_measured_at=D(9))]
    e.sections.rehabilitation_potential = 'высокий'
    e.sections.goal = 'исходная цель'
    scanned = _mdrk_document('final.docx', '', meeting='08.09.2026 12:00')
    with patch('mdrk_builder.application.final_mdrk.extract_mdrk_scale_measurements', return_value=[ScaleMeasurement('Berg', '40', D(8), ROLE)]), patch('mdrk_builder.application.final_mdrk.extract_icf_observations', return_value=[IcfObservation('d450', 'Ходьба', (), specialist=ROLE, rating_pair=(None, IcfQualifier(2)))]):
        apply_final_mdrk_document(e, scanned, discharge_scale_values={}, issues=[])
    snapshot = build_snapshot(e, MdrkKind.FINAL)
    assert {m.value for f in e.findings for m in f.scales} == {'48', '40'}
    assert snapshot.scale_rows[0].current.value == '48'
    assert e.icf_domains[0].initial == IcfQualifier(3)
    assert e.icf_domains[0].final == IcfQualifier(1)
    assert e.sections.rehabilitation_potential == 'высокий'
    assert e.sections.goal == 'исходная цель'
    assert (snapshot.goal, snapshot.tasks) == (FINAL_GOAL, FINAL_TASKS)


def test_continuations_and_copies_are_merged_and_snapshots_have_cutoffs():
    dates = [D(i).date() for i in (1, 2, 3, 4)]
    rows = [Procedure('ЛФК', 'ФТ', 3, 30, code='A1', performed_dates=tuple(dates[:3]), source=Path('/a.docx'), planned_count=10, planned_frequency='ежедневно'),
            Procedure('ЛФК', 'ФТ', 2, 30, code='A1', performed_dates=tuple(dates[2:]), source=Path('/b.docx'))]
    merged, = merge_procedures([*rows, rows[0]])
    assert merged.actual_count == 4 and len(merged.source_paths) == 2
    planned, = select_procedures([merged], D(1), D(2), MdrkKind.INITIAL)
    actual, = select_procedures([merged], D(2), D(3))
    assert (planned.actual_count, planned.frequency) == (10, 'ежедневно')
    assert actual.actual_count == 2 and actual.performed_dates == tuple(dates[1:3])
    assert merged.actual_count == 4
    merged.actual_count = 7
    merged.manual_fields.add('actual_count')
    manual, = select_procedures([merged], D(2), D(3))
    assert manual.actual_count == 7 and manual.count_needs_review


def test_undated_overlapping_sheets_do_not_sum_unverifiable_counts():
    row, = merge_procedures([Procedure('ЛФК', 'ФТ', 5), Procedure('ЛФК', 'ФТ', 7)])
    assert row.actual_count is None and row.count_needs_review


def test_scanner_collects_every_assignment_sheet():
    e = episode()
    records = [_record('/a.docx', '', document_type='assignment_sheet'), _record('/b.docx', '', document_type='assignment_sheet')]
    with patch('mdrk_builder.application.scanner.extract_procedures', side_effect=lambda doc, **kw: [Procedure(doc.source_path.stem, 'ФТ', 1, performed_dates=(D(3).date(),))]):
        _collect_procedures(e, records)
    assert {p.name for p in e.procedures} == {'a', 'b'}


def test_same_date_icf_conflict_has_persistent_source_choice():
    e = episode()
    records = [_record('/a.docx', '', role=ROLE, clinical_datetime=D(6)), _record('/b.docx', '', role=ROLE, clinical_datetime=D(6))]
    with patch('mdrk_builder.application.scanner.extract_icf_observations', side_effect=lambda doc: [IcfObservation('d450', 'Ходьба', (IcfQualifier(2 if doc.source_path.stem == 'a' else 3),), specialist=ROLE)]):
        _merge_icf(e, records)
    assert e.icf_domains[0].initial is None
    assert icf_conflict_issues(e.icf_domains)
    e = decode(encode(e))
    key, = icf_choices(e.icf_domains)
    resolve_icf_choice(e.icf_domains, key, '2', Path('/a.docx'))
    assert e.icf_domains[0].final == IcfQualifier(2)
    assert e.icf_domains[0].final_source == Path('/a.docx')
    assert not icf_conflict_issues(e.icf_domains)


@pytest.mark.parametrize('a,b,marker', [(IcfQualifier(1, True), IcfQualifier(3, True), '+'), (IcfQualifier(2), IcfQualifier(2, True), '+'), (IcfQualifier(3, True), IcfQualifier(1, True), '-'), (IcfQualifier(1), IcfQualifier(3), '-'), (IcfQualifier(0), IcfQualifier(0, True), '')])
def test_icf_direction_respects_facilitator(a, b, marker):
    assert IcfDomain('e115', 'Средства', ROLE, a, b).dynamic_marker == marker


def test_current_fields_use_latest_explicit_source_with_same_day_conflict():
    records = [
        _record('/primary.docx', 'Осмотр невролога 01.09.2026 12:00\nДвигательный режим: палатный', clinical_datetime=D(1)),
        _record('/diary.docx', 'Осмотр невролога 06.09.2026 12:00\nДвигательный режим: свободный', clinical_datetime=D(6)),
        _record('/future.docx', 'Осмотр невролога 11.09.2026 12:00\nДвигательный режим: постельный', clinical_datetime=D(11)),
    ]
    scanned = [ScannedDocument(r.document, r.classification) for r in records]
    values, sources, choices, _ = select_current_fields(scanned, D(1), D(10))
    assert values['movement_regimen'] == 'свободный'
    assert sources['movement_regimen'] == Path('/diary.docx')
    assert not choices
    competing = _record('/conflict.docx', 'Осмотр невролога 06.09.2026 12:00\nДвигательный режим: постельный', clinical_datetime=D(6))
    _, _, choices, issues = select_current_fields([*scanned, ScannedDocument(competing.document, competing.classification)], D(1), D(10))
    assert len(choices['movement_regimen']) == 2 and issues


def test_final_clinician_advice_does_not_require_mis_treatment_heading():
    final = _record('/final.docx', 'Осмотр невролога 09.09.2026 12:00\nСостояние при выписке: удовлетворительное\nРекомендации: продолжить занятия', document_type='final', clinical_datetime=D(9))
    values, sources, _, _ = select_current_fields([ScannedDocument(final.document, final.classification)], D(1), D(10))
    assert values['discharge_condition'] == 'удовлетворительное'
    assert values['recommendations'] == 'продолжить занятия'
    assert sources['recommendations'] == Path('/final.docx')


def test_selected_icf_point_is_not_redated_by_newer_source_on_rescan():
    from copy import deepcopy
    from mdrk_builder.application.editing import merge_rows
    original = IcfDomain('d450', 'Ходьба', ROLE, final=IcfQualifier(3),
                         final_source=Path('/b.docx'), final_measured_at=D(6), source=Path('/a.docx'),
                         conflict_choices={'final': [('2', Path('/a.docx')), ('3', Path('/b.docx'))]})
    edited = deepcopy(original)
    key, = icf_choices([edited])
    resolve_icf_choice([edited], key, '2', Path('/a.docx'))
    current = IcfDomain('d450', 'Ходьба', ROLE, final=IcfQualifier(1),
                        final_source=Path('/later.docx'), final_measured_at=D(9), source=Path('/a.docx'))
    merged, _ = merge_rows([original], [edited], [current])
    assert (merged[0].final, merged[0].final_source, merged[0].final_measured_at) == (IcfQualifier(1), Path('/later.docx'), D(9))
    assert 'final' not in merged[0].manual_fields
    unchanged, _ = merge_rows([original], [edited], [original])
    assert unchanged[0].final == IcfQualifier(2)
    assert unchanged[0].final_measured_at == D(6)
    assert not icf_choices(unchanged)


def test_prescribed_count_and_frequency_are_extracted_separately_from_executions():
    from mdrk_builder.application.extractors import extract_procedures
    from mdrk_builder.infrastructure.ooxml_reader import ParsedCell, ParsedRow, ParsedTable
    def row(values):
        return ParsedRow(tuple(ParsedCell(i, 1, v) for i, v in enumerate(values)), len(values))
    document = _document('/plan.docx', tables=[ParsedTable((
        row(['Назначения', 'Назначено', 'Кратность', '01.09.2026', '02.09.2026']),
        row(['A19.23.001 ЛФК', '10', '3 раза в неделю', '+', '+']),
    ))])
    procedure, = extract_procedures(document, reference_date=D(1).date())
    assert procedure.planned_count == 10
    assert procedure.planned_frequency == '3 раза в неделю'
    assert procedure.actual_count == 2
    assert procedure.performed_dates == (D(1).date(), D(2).date())
