"""Synthetic regressions found by the review of 3567abd..27442ab.

Run using the reviewed repository's existing Python and PYTHONPATH=src:.
These five regressions originally failed at 27442ab.
No patient documents are read; filesystem writes use pytest temporary folders.
"""

from copy import deepcopy
from datetime import datetime
from pathlib import Path
import tkinter as tk

import pytest

from mdrk_builder.application.discharge_validation import current_discharge_issues
from mdrk_builder.application.editing import merge_rows
from mdrk_builder.domain import (
    DischargeSummaryDraft,
    Episode,
    IcfDomain,
    IcfQualifier,
    MdrkKind,
    Procedure,
    ReverseSheetDraft,
    ReverseSheetRow,
    ReviewIssue,
    ReviewSeverity,
    SpecialistRole,
)
from mdrk_builder.ui.app import MdrkBuilderApp


@pytest.fixture
def app(tmp_path, monkeypatch):
    root = tk.Tk()
    root.withdraw()
    errors = []
    monkeypatch.setattr(
        "tkinter.messagebox.showerror", lambda *args, **kwargs: errors.append(args)
    )
    ui = MdrkBuilderApp(root)
    folder = tmp_path.resolve()
    ui._set_folder_field(str(folder))
    ui._current_kind = MdrkKind.FINAL
    ui.kind_var.set(MdrkKind.FINAL.value)
    ui.document_var.set("mdrk2")
    ui._previous_document = "mdrk2"
    ui.episode = Episode(
        folder,
        admission_datetime=datetime(2026, 8, 10),
        initial_meeting_at=datetime(2026, 8, 10, 12),
        final_meeting_at=datetime(2026, 8, 18),
        course_duration_days=8,
    )
    ui._scan_baseline = deepcopy(ui.episode)
    ui._populate_from_episode()
    try:
        yield ui
        assert not errors, errors
    finally:
        root.destroy()


def test_icf_edit_remains_attached_to_same_limb_after_reorder():
    role = SpecialistRole.PHYSICAL_THERAPIST
    source = Path("/synthetic/source.docx")
    baseline = [
        IcfDomain("b730", "левая рука", role, initial=IcfQualifier(2), source=source),
        IcfDomain("b730", "правая рука", role, initial=IcfQualifier(1), source=source),
    ]
    edited = deepcopy(baseline)
    edited[0].initial = IcfQualifier(4)
    edited[0].manual_fields.add("initial")
    incoming = list(reversed(deepcopy(baseline)))

    merged, _ = merge_rows(baseline, edited, incoming)

    assert {row.description: row.initial.value for row in merged} == {
        "левая рука": 4,
        "правая рука": 1,
    }


def test_new_episode_draft_excludes_previous_episode_state(app, tmp_path):
    previous_marker = "SYNTHETIC_PREVIOUS_EPISODE_A"
    panel = app.discharge_workspace
    panel.load(
        DischargeSummaryDraft(app.episode.folder, clinical_diagnosis=previous_marker)
    )
    app.discharge_draft = panel.draft
    new_folder = tmp_path.resolve() / "B"
    new_folder.mkdir()

    app.folder_var.set(str(new_folder))
    app._active_job_folder = new_folder
    app._finish_scan(Episode(new_folder), None)
    assert app.episode.folder == new_folder
    assert panel.draft is None
    assert app._save_workspace()

    saved = (new_folder / ".mdrk draft.json").read_text(encoding="utf-8")
    assert previous_marker not in saved


def test_undo_survives_switching_between_mdrk_and_discharge(app):
    app.episode.procedures = [
        Procedure("ЛФК", "ФТ", 3, source=app.episode.folder / "source.docx")
    ]
    app._scan_baseline = deepcopy(app.episode)
    app._populate_from_episode()
    panel = app.discharge_workspace
    panel.load(
        DischargeSummaryDraft(
            app.episode.folder,
            admission_datetime=app.episode.admission_datetime,
            discharge_datetime=app.episode.final_meeting_at,
            completed_procedures=tuple(deepcopy(app.episode.procedures)),
        )
    )
    app.discharge_draft = panel.draft

    app._commit_procedure_cell("0", "count", "5")
    app._select_document("discharge")
    app._select_document("mdrk2")
    assert panel.draft.completed_procedures[0].actual_count == 5
    app._table_history.undo()
    assert app.episode.procedures[0].actual_count == 3

    app._select_document("discharge")
    app._select_document("mdrk2")

    assert app.episode.procedures[0].actual_count == 3
    assert panel.draft.completed_procedures[0].actual_count == 3


def test_missing_repeat_warning_survives_validation_refresh():
    domain = IcfDomain(
        "b730", "синтетический домен", SpecialistRole.PHYSICAL_THERAPIST,
        initial=IcfQualifier(2),
    )
    draft = DischargeSummaryDraft(
        Path("/synthetic/episode"),
        header_text="Синтетическая шапка",
        clinical_diagnosis="Синтетический диагноз",
        discharge_datetime=datetime(2026, 8, 18),
        icf_domains=(domain,),
        issues=[
            ReviewIssue(
                "icf_incomplete_pair", "Нет повторной оценки",
                ReviewSeverity.WARNING, "icf.b730",
            )
        ],
    )

    issues = current_discharge_issues(draft)

    assert domain.final is None
    assert any(issue.field.startswith("icf") for issue in issues)


def test_tab_in_reverse_sheet_moves_without_creating_a_row(app):
    panel = app.reverse_workspace
    panel.load(
        ReverseSheetDraft(app.episode.folder, rows=[ReverseSheetRow("SYNTHETIC ROW")])
    )
    app.mdrk_workspace.pack_forget()
    panel.pack(fill="both", expand=True)
    app.root.deiconify()
    app.root.geometry("1000x700")
    app.root.update()
    editor = panel._row_editor
    editor.edit("0", "intervention")
    assert editor._widget is not None

    editor._next_cell("0", "intervention", 1)

    assert len(panel.draft.rows) == 1
    assert panel.row_tree.selection() == ("0",)


def test_shared_undo_redo_tracks_edits_from_both_documents(app):
    procedure = Procedure('ЛФК', 'ФТ', 3, source=app.episode.folder / 'source.docx')
    app.episode.procedures = [procedure]
    app._scan_baseline = deepcopy(app.episode)
    panel = app.discharge_workspace
    panel.load(DischargeSummaryDraft(app.episode.folder, completed_procedures=(deepcopy(procedure),)))
    app.discharge_draft = panel.draft
    app._populate_from_episode()
    assert app._table_history is panel._table_history

    app._commit_procedure_cell('0', 'count', '5')
    assert panel.draft.completed_procedures[0].actual_count == 5
    panel._table_history.wrap(lambda: setattr(panel.draft.completed_procedures[0], 'actual_count', 7))()
    assert app.episode.procedures[0].actual_count == 7

    for expected in (5, 3):
        app._table_history.undo()
        assert app.episode.procedures[0].actual_count == expected
        assert panel.draft.completed_procedures[0].actual_count == expected
    for expected in (5, 7):
        panel._table_history.redo()
        assert app.episode.procedures[0].actual_count == expected
        assert panel.draft.completed_procedures[0].actual_count == expected


def test_renaming_source_icf_preserves_identity_during_rescan():
    from mdrk_builder.application.editing import mark_manual_changes
    role = SpecialistRole.PHYSICAL_THERAPIST
    source = Path('/synthetic/source.docx')
    baseline = [IcfDomain('b730', 'левая рука', role, initial=IcfQualifier(2), source=source),
                IcfDomain('b730', 'правая рука', role, initial=IcfQualifier(1), source=source)]
    edited = deepcopy(baseline)
    edited[0].description = 'левая верхняя конечность'
    mark_manual_changes(baseline[0], edited[0])
    incoming = list(reversed(deepcopy(baseline)))
    incoming[1].final = IcfQualifier(1)
    merged, _ = merge_rows(baseline, edited, incoming)
    assert [(r.description, r.initial.value) for r in merged] == [
        ('правая рука', 1), ('левая верхняя конечность', 2),
    ]
    assert merged[1].final.value == 1


def test_renamed_discharge_scale_retains_both_dates_during_rescan():
    from dataclasses import replace
    from mdrk_builder.application.editing import mark_manual_changes
    from mdrk_builder.domain import DischargeScaleRow
    initial = DischargeScaleRow(SpecialistRole.PHYSICAL_THERAPIST, 'Берг', '20', Path('/synthetic/scales.docx'), current_at=datetime(2026, 8, 10))
    final = replace(initial, value='30', current_at=datetime(2026, 8, 18))
    baseline = [initial, final]
    renamed = replace(initial, name='Уточнённая шкала', value='21')
    mark_manual_changes(initial, renamed)
    merged, _ = merge_rows(baseline, [renamed, final], [final, initial])
    assert [(r.name, r.value, r.current_at) for r in merged] == [
        ('Берг', '30', final.current_at), ('Уточнённая шкала', '21', initial.current_at),
    ]


def test_discharge_warning_clears_only_after_missing_assessment_is_filled():
    draft = DischargeSummaryDraft(Path('/synthetic/episode'), icf_domains=(
        IcfDomain('b730', 'левая рука', SpecialistRole.PHYSICAL_THERAPIST),
        IcfDomain('pf', 'мотивация', SpecialistRole.OTHER),
    ))
    draft.issues = current_discharge_issues(draft)
    assert [i.code for i in draft.issues if i.field.startswith('icf.')] == ['icf_initial_missing', 'icf_final_missing']
    draft.icf_domains[0].initial = IcfQualifier(2)
    draft.issues = current_discharge_issues(draft)
    assert [i.code for i in draft.issues if i.field.startswith('icf.')] == ['icf_final_missing']
    draft.icf_domains[0].final = IcfQualifier(1)
    assert not [i for i in current_discharge_issues(draft) if i.field.startswith('icf.')]


def test_tab_uses_visible_columns_and_skips_action_rows(app):
    from tkinter import ttk
    from mdrk_builder.ui.inline_tree import InlineTreeEditor
    top = tk.Toplevel(app.root)
    tree = ttk.Treeview(top, columns=('a', 'hidden', 'b'), displaycolumns=('b', 'a'), show='headings')
    tree.pack()
    tree.insert('', 'end', iid='0', values=('a0', 'hidden0', 'b0'))
    tree.insert('', 'end', iid='action', values=('add', '', ''))
    tree.insert('', 'end', iid='1', values=('a1', 'hidden1', 'b1'))
    top.update()
    actions = []
    editor = InlineTreeEditor(tree, editable_columns={'a', 'hidden', 'b'},
        commit=lambda row, col, value: tree.set(row, col, value),
        activate=lambda row: actions.append(row), is_data_row=str.isdigit)
    editor.edit('0', 'b')
    editor._next_cell('0', 'b', 1)
    assert editor._widget.get() == 'a0'
    editor._next_cell('0', 'a', 1)
    assert editor._widget.get() == 'b1'
    editor._next_cell('1', 'b', -1)
    assert editor._widget.get() == 'a0'
    assert actions == []
    top.destroy()


def test_legacy_workspace_drops_unloaded_patient_panels(tmp_path):
    from mdrk_builder.application.workspace import restore_workspace_state
    from mdrk_builder.infrastructure.draft_store import encode
    legacy = {'kind': MdrkKind.FINAL, 'document': 'mdrk2', 'episode': Episode(tmp_path),
        'discharge': None, 'discharge_baseline': DischargeSummaryDraft(tmp_path / 'previous'),
        'discharge_text': {'clinical_diagnosis': 'SYNTHETIC_PREVIOUS_EPISODE'},
        'reverse': None, 'reverse_entries': {'full_name': 'SYNTHETIC_PREVIOUS_EPISODE'}}
    state = restore_workspace_state(legacy, tmp_path)
    assert state.discharge is None and state.reverse is None
    assert 'SYNTHETIC_PREVIOUS_EPISODE' not in str(encode(state))


def test_foreign_baseline_rejected_before_touching_open_workspace(app, monkeypatch):
    from mdrk_builder.infrastructure.draft_store import save_draft
    panel = app.discharge_workspace
    panel.load(DischargeSummaryDraft(app.episode.folder, clinical_diagnosis='CURRENT'))
    state = app._capture_workspace()
    state.discharge.baseline = DischargeSummaryDraft(app.episode.folder / 'foreign')
    save_draft(app.episode.folder / '.mdrk draft.json', state)
    errors = []
    monkeypatch.setattr('tkinter.messagebox.askyesno', lambda *args, **kwargs: True)
    monkeypatch.setattr('tkinter.messagebox.showerror', lambda *args, **kwargs: errors.append(args))
    episode = app.episode
    assert not app._restore_workspace(app.episode.folder)
    assert errors
    assert app.episode is episode
    assert panel.draft.clinical_diagnosis == 'CURRENT'
    assert not panel._populating and not app._populating


def test_discharge_scale_identity_edit_replaces_original_observation():
    from dataclasses import replace
    from mdrk_builder.application.editing import mark_manual_changes
    from mdrk_builder.application.shared_edits import transfer_discharge_edits
    from mdrk_builder.domain import DischargeScaleRow, ScaleMeasurement, SpecialistFinding
    role = SpecialistRole.PHYSICAL_THERAPIST
    initial_at = datetime(2026, 8, 10)
    current_at = datetime(2026, 8, 18)
    source = Path('/synthetic/scale.docx')
    episode = Episode(source.parent, findings=[SpecialistFinding(role, scales=[
        ScaleMeasurement('Берг', '20', initial_at, role, source),
    ])])
    old = DischargeScaleRow(role, 'Берг', '20', source, current_at=initial_at)
    draft = DischargeSummaryDraft(source.parent, admission_scale_rows=(old,))
    baseline = deepcopy(draft)
    edited = replace(old, name='Уточнённая шкала', current_at=current_at, value='')
    mark_manual_changes(old, edited)
    draft.admission_scale_rows = (edited,)
    transfer_discharge_edits(draft, episode, baseline=baseline)
    scales = [s for f in episode.findings for s in f.scales]
    assert [(s.name, s.value, s.measured_at) for s in scales] == [('Уточнённая шкала', '', current_at)]


def test_ambiguous_duplicate_identity_never_assigns_edits_by_position():
    role = SpecialistRole.PHYSICAL_THERAPIST
    first = IcfDomain('b730', 'рука', role, initial=IcfQualifier(2), source=Path('/synthetic/source.docx'))
    second = deepcopy(first)
    second.initial = IcfQualifier(1)
    baseline = [first, second]
    edited = deepcopy(baseline)
    edited[0].initial = IcfQualifier(4)
    edited[0].manual_fields.add('initial')
    incoming = list(reversed(deepcopy(baseline)))
    incoming[0].note = 'обновлённая строка источника'
    merged, issues = merge_rows(baseline, edited, incoming)
    assert merged == edited
    assert any('без сопоставления по позиции' in issue for issue in issues)
    assert not any(row.note for row in merged)


def test_table_undo_is_available_from_actual_menu(app):
    app.episode.procedures = [Procedure('ЛФК', 'ФТ', 3)]
    app._populate_from_episode()
    app._commit_procedure_cell('0', 'count', '5')
    menu = app.root.nametowidget(app.root.cget('menu'))
    edit = app.root.nametowidget(menu.entrycget('Правка', 'menu'))
    edit.invoke('Отменить табличную правку')
    assert app.episode.procedures[0].actual_count == 3
    edit.invoke('Повторить табличную правку')
    assert app.episode.procedures[0].actual_count == 5


def test_discharge_edits_survive_first_mdrk_scan(app):
    folder = app.episode.folder
    source = folder / 'source.docx'
    panel = app.discharge_workspace
    panel.load(DischargeSummaryDraft(folder, completed_procedures=(Procedure('ЛФК', 'ФТ', 3, source=source),)))
    app.discharge_draft = panel.draft
    app.episode = None
    panel._table_history.wrap(lambda: setattr(panel.draft.completed_procedures[0], 'actual_count', 5))()
    app._active_job_folder = folder
    app._finish_scan(Episode(folder, procedures=[Procedure('ЛФК', 'ФТ', 3, source=source)]), None)
    assert app.episode.procedures[0].actual_count == 5
    assert app.discharge_draft.completed_procedures[0].actual_count == 5
    assert 'procedures' in app._manual_collections


def test_scale_name_edited_inline_is_reconciled_with_its_source(app):
    from mdrk_builder.domain import ScaleMeasurement, SpecialistFinding
    role = SpecialistRole.PHYSICAL_THERAPIST
    measurement = ScaleMeasurement('Берг', '20', datetime(2026, 8, 10), role, app.episode.folder / 'source.docx')
    app.episode.findings = [SpecialistFinding(role, scales=[measurement])]
    baseline = deepcopy(app.episode.findings)
    app._edit_scale_measurement(measurement, 'name', 'Уточнённая шкала')
    merged, _ = merge_rows(baseline, app.episode.findings, baseline)
    assert [s.name for f in merged for s in f.scales] == ['Уточнённая шкала']
