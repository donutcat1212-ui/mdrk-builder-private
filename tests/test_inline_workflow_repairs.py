from copy import deepcopy
from datetime import datetime
from types import SimpleNamespace
import tkinter as tk

import pytest

from mdrk_builder.domain import DischargeSummaryDraft, Episode, MdrkKind, Procedure
from mdrk_builder.application.snapshot import build_snapshot
from test_daily_workflow_ui import app


@pytest.mark.parametrize('kind', [MdrkKind.INITIAL, MdrkKind.FINAL])
def test_new_icf_row_is_visible_and_editable_without_popup(app, kind):
    app._current_kind = kind
    app.kind_var.set(kind.value)
    app._populate_from_episode()
    app.root.deiconify()
    app.notebook.select(1)
    app.root.update()
    tree = app.icf_tree
    tree.see('new:icf')
    app.root.update()
    x, y, width, height = tree.bbox('new:icf', 'code')
    app._finish_icf_pointer(SimpleNamespace(x=x + width // 2, y=y + height // 2))
    app.root.update()
    assert tree.exists('0')
    assert len(build_snapshot(app.episode, kind).icf_domains) == 1
    app._icf_editor.edit('0', 'code')
    assert app._icf_editor._widget is not None
    app._icf_editor._widget.insert(0, 'd450')
    app._icf_editor._next_cell('0', 'code', 1)
    assert app.episode.icf_domains[0].code == 'd450'
    assert app._icf_editor._widget is not None
    assert not any(isinstance(w, tk.Toplevel) for w in app.root.winfo_children())


def test_discharge_icf_and_program_are_edited_in_cells(app):
    panel = app.discharge_workspace
    panel.load(DischargeSummaryDraft(app.episode.folder, completed_procedures=(Procedure('A', 'Процедура', 3),)))
    app.discharge_draft = panel.draft
    app.document_var.set('discharge')
    app._on_document_changed()
    app.root.deiconify()
    panel.notebook.select(1)
    app.root.update()
    panel._edit_discharge_icf('add')
    app.root.update()
    assert panel.icf_tree.exists('domain:0')
    panel._icf_editor.edit('domain:0', 'code')
    assert panel._icf_editor._widget is not None
    panel._icf_editor._widget.insert(0, 'd540')
    panel._icf_editor.accept('domain:0', 'code')
    assert panel.draft.icf_domains[0].code == 'd540'
    panel._commit_clinical_cell('program:0:field:frequency', 'value', '3 раза в неделю')
    assert panel.draft.completed_procedures[0].frequency == '3 раза в неделю'
    assert 'frequency' in panel.draft.completed_procedures[0].manual_fields
    assert not any(isinstance(w, tk.Toplevel) for w in app.root.winfo_children())


def test_clearing_fallback_planned_frequency_is_preserved(app):
    app.episode.procedures = [Procedure('A', 'Процедура', None, frequency='Ежедневно')]
    app._refresh_procedures()
    assert app.procedure_tree.set('0', 'frequency') == 'Ежедневно'
    app._commit_procedure_cell('0', 'frequency', '')
    assert app.procedure_tree.set('0', 'frequency') == ''
    assert 'planned_frequency' in app.episode.procedures[0].manual_fields
    assert build_snapshot(app.episode, MdrkKind.INITIAL).procedures[0].frequency == ''
    assert build_snapshot(app.episode, MdrkKind.FINAL).procedures[0].frequency == 'Ежедневно'


def test_icf_specialist_and_note_are_independently_editable(app):
    from mdrk_builder.domain import IcfDomain, IcfQualifier, SpecialistRole
    app.episode.icf_domains = [IcfDomain('d450', 'Ходьба', SpecialistRole.NEUROLOGIST,
                                        initial=IcfQualifier(2), note='с опорой')]
    app._refresh_icf()
    app._commit_icf_cell('0', 'responsible', SpecialistRole.PHYSICAL_THERAPIST.display_name)
    assert app.episode.icf_domains[0].note == 'с опорой'
    assert app.icf_tree.set('0', 'note') == 'с опорой'
    app._commit_icf_cell('0', 'note', 'ручное уточнение')
    assert app.episode.icf_domains[0].specialist is SpecialistRole.PHYSICAL_THERAPIST
    panel = app.discharge_workspace
    panel.load(DischargeSummaryDraft(app.episode.folder, icf_domains=tuple(app.episode.icf_domains)))
    panel._commit_icf_cell('domain:0', 'responsible', SpecialistRole.NEUROLOGIST.display_name)
    assert panel.draft.icf_domains[0].note == 'ручное уточнение'
    panel._commit_icf_cell('domain:0', 'note', 'ещё одно уточнение')
    assert panel.draft.icf_domains[0].specialist is SpecialistRole.NEUROLOGIST
    assert panel.icf_tree.set('domain:0', 'note') == 'ещё одно уточнение'


def test_discharge_conclusion_edit_only_changes_selected_specialist(app):
    from mdrk_builder.domain import DischargeTeamFinding, SpecialistRole
    panel = app.discharge_workspace
    roles = (SpecialistRole.NEUROLOGIST, SpecialistRole.LOGOPEDIST)
    panel.load(DischargeSummaryDraft(app.episode.folder, team_findings=tuple(
        DischargeTeamFinding(role, conclusion='SOURCE ' + role.value) for role in roles)))
    panel._commit_clinical_cell('team:0:field:conclusion', 'value', 'EDIT NEUROLOGIST')
    assert panel.draft.team_findings[0].conclusion == 'EDIT NEUROLOGIST'
    assert panel.draft.team_findings[1].conclusion == 'SOURCE ' + roles[1].value


def test_signatures_keep_source_text_until_edited_and_sync_separate_fields(app):
    panel = app.discharge_workspace
    source = 'Лечащий врач: Врач А\nЗаведующий отделением: Врач Б'
    panel.load(DischargeSummaryDraft(app.episode.folder, signatures=source))
    app.root.update()
    assert panel.signature_fields._fields['doctor'].get('1.0', 'end-1c') == 'Врач А'
    assert panel.draft.signatures == source
    field = panel.signature_fields._fields['doctor']
    field.delete('1.0', 'end')
    field.insert('1.0', 'Ручная подпись')
    app.root.update()
    assert panel.apply()
    assert 'Лечащий врач: Ручная подпись' in panel.draft.signatures
    assert 'Заведующий отделением: Врач Б' in panel.draft.signatures
    assert 'signatures' in panel.draft.manual_fields


def test_planned_days_are_independent_of_actual_days_and_survive_rescan(app):
    assert app._entry_variables['duration'].get() == '16'
    app._entry_variables['duration'].set('12')
    app.kind_var.set(MdrkKind.FINAL.value)
    app._on_kind_changed()
    assert app.episode.planned_course_duration_days == 12
    assert app._entry_variables['duration'].get() == '7'
    app._pending_manual_state = app._capture_manual_state()
    fresh = Episode(app.episode.folder, admission_datetime=datetime(2026, 8, 10),
                    final_meeting_at=datetime(2026, 8, 20), course_duration_days=10)
    app._merge_manual_state(fresh)
    assert fresh.planned_course_duration_days == 12
    assert fresh.course_duration_days == 10


def test_explicit_multiline_conclusion_is_committed_without_popup(app):
    from tkinter import ttk
    from mdrk_builder.ui.inline_tree import InlineTreeEditor
    top = tk.Toplevel(app.root)
    tree = ttk.Treeview(top, columns=('value',), show='headings')
    tree.pack()
    tree.insert('', 'end', iid='0', values=('Исходный текст',))
    top.update()
    editor = InlineTreeEditor(tree, editable_columns={'value'}, multiline=lambda *_: True,
                              commit=lambda row, col, value: tree.set(row, col, value))
    editor.edit('0', 'value')
    editor._widget.insert('end', '\nПолная рекомендация (продолжение).')
    editor.accept('0', 'value')
    assert tree.set('0', 'value').endswith('\nПолная рекомендация (продолжение).')
    top.destroy()


@pytest.mark.parametrize('kind', [MdrkKind.INITIAL, MdrkKind.FINAL])
def test_conclusion_edit_stays_with_selected_specialist(app, kind):
    from mdrk_builder.domain import SpecialistFinding, SpecialistRole
    roles = (SpecialistRole.NEUROLOGIST, SpecialistRole.LOGOPEDIST, SpecialistRole.NEUROPSYCHOLOGIST)
    app.episode.findings = [SpecialistFinding(role, conclusion='SOURCE ' + role.value,
        source_datetime=datetime(2026, 8, 10), source=app.episode.folder / (role.value + '.docx')) for role in roles]
    app._current_kind = kind
    app.kind_var.set(kind.value)
    app._populate_from_episode()
    app.root.deiconify()
    app.notebook.select(2)
    app.root.update()
    app.finding_tree.selection_set('0')
    app.root.update()
    app.specialist_conclusion.focus_force()
    app.specialist_conclusion.delete('1.0', 'end')
    app.specialist_conclusion.insert('1.0', 'EDITED NEUROLOGIST')
    neurologist_buffer = app.specialist_conclusion
    for index in (1, 2, 0, 2, 1):
        tree = app.finding_tree
        x, y, width, height = tree.bbox(str(index), 'role')
        tree.event_generate('<ButtonPress-1>', x=x + 10, y=y + height // 2)
        tree.event_generate('<ButtonRelease-1>', x=x + 10, y=y + height // 2)
        app.root.update()
        expected = 'EDITED NEUROLOGIST' if index == 0 else 'SOURCE ' + roles[index].value
        assert app.specialist_conclusion.get('1.0', 'end-1c') == expected
        if index != 0:
            assert app.specialist_conclusion is not neurologist_buffer
    assert [row.conclusion for row in app.episode.findings] == [
        'EDITED NEUROLOGIST', 'SOURCE logopedist', 'SOURCE neuropsychologist']
    # A late focus event from the hidden neurologist editor retains its owner.
    neurologist_buffer.insert('end', ' LATE')
    neurologist_buffer.event_generate('<FocusOut>')
    app.root.update()
    assert app.episode.findings[0].conclusion == 'EDITED NEUROLOGIST LATE'
    assert app.episode.findings[1].conclusion == 'SOURCE logopedist'
    app.specialist_conclusion.insert('end', ' AUTOSAVE')
    saved = app._capture_manual_state()['episode']
    assert saved.findings[1].conclusion == 'SOURCE logopedist AUTOSAVE'
