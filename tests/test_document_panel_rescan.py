import tkinter as tk
from pathlib import Path

import pytest

from mdrk_builder.domain import DischargeSummaryDraft, ReverseSheetDraft
from mdrk_builder.ui.document_panels import DischargeSummaryPanel, ReverseSheetPanel


@pytest.fixture
def root(monkeypatch):
    try:
        window = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(str(exc))
    window.withdraw()
    monkeypatch.setattr('mdrk_builder.ui.document_panels.messagebox.showerror', lambda *a, **k: None)
    yield window
    window.destroy()


@pytest.mark.parametrize('discharge', [True, False])
def test_invalid_date_does_not_partially_mutate_or_replace_form(root, tmp_path, discharge):
    cls, draft_cls = (DischargeSummaryPanel, DischargeSummaryDraft) if discharge else (ReverseSheetPanel, ReverseSheetDraft)
    panel = cls(root, open_path=lambda _: None)
    old = draft_cls(folder=tmp_path)
    old.identity.full_name = 'Исходное имя'
    panel.load(old)
    variables = panel._identity_vars if discharge else panel._header_vars
    variables['full_name'].set('Ручная правка имени')
    variables['birth_date'].set('неверная дата')
    if discharge:
        panel._widgets['clinical_diagnosis'].insert('1.0', 'Ручной текст')
    assert panel.merge_scan(draft_cls(folder=tmp_path)) is False
    assert panel.draft is old
    assert old.identity.full_name == 'Исходное имя'
    assert variables['full_name'].get() == 'Ручная правка имени'
    assert variables['birth_date'].get() == 'неверная дата'
    if discharge:
        assert panel._widgets['clinical_diagnosis'].get('1.0', 'end-1c') == 'Ручной текст'


def test_text_change_without_keyrelease_survives_rescan(root, tmp_path):
    panel = DischargeSummaryPanel(root, open_path=lambda _: None)
    old = DischargeSummaryDraft(folder=tmp_path, clinical_diagnosis='Исходный текст')
    panel.load(old)
    widget = panel._widgets['clinical_diagnosis']
    widget.delete('1.0', 'end')
    widget.insert('1.0', 'Вставлено через меню')
    fresh = DischargeSummaryDraft(folder=tmp_path, clinical_diagnosis='Повторное извлечение')
    fresh.field_sources['clinical_diagnosis'] = Path('source.docx')
    assert panel.merge_scan(fresh) is True
    assert panel.draft.clinical_diagnosis == 'Вставлено через меню'
    assert 'clinical_diagnosis' not in panel.draft.field_sources


def test_confirmed_date_is_passed_to_background_scan_and_manual_text_survives(root, tmp_path, monkeypatch):
    from datetime import datetime
    from types import SimpleNamespace
    from mdrk_builder.ui.app import MdrkBuilderApp
    from mdrk_builder.domain import Procedure
    panel = DischargeSummaryPanel(root, open_path=lambda _: None)
    start, old_end, new_end = datetime(2026, 8, 10), datetime(2026, 8, 17), datetime(2026, 8, 18)
    old = DischargeSummaryDraft(folder=tmp_path, admission_datetime=start,
        discharge_datetime=old_end, projection_period=(start, old_end))
    panel.load(old)
    panel._identity_vars["discharge"].set("18.08.2026 00:00")
    panel._widgets["clinical_diagnosis"].insert("1.0", "Ручной диагноз")
    calls = []
    monkeypatch.setattr("mdrk_builder.ui.app.scan_discharge_summary",
        lambda folder, **kw: calls.append((folder, kw)))
    app = SimpleNamespace(
        _scanning=False, discharge_workspace=panel,
        _auxiliary_scan_folder=lambda: tmp_path,
        _set_scanning=lambda value: None, status_var=tk.StringVar(),
        _start_background_job=lambda job, callback, **kw: job(),
        _finish_discharge_summary_scan=lambda *args: None,
    )
    MdrkBuilderApp._start_discharge_summary_scan(app)
    assert calls == [(tmp_path, {"discharge_datetime_override": new_end, "scan_session": None})]
    assert old.requires_period_rescan()
    fresh = DischargeSummaryDraft(folder=tmp_path, admission_datetime=start,
        discharge_datetime=new_end, projection_period=(start, new_end),
        manual_fields={"discharge_datetime", "header_text"},
        completed_procedures=(Procedure("ЛФК", "ФТ", 3),))
    assert panel.merge_scan(fresh)
    assert panel.draft.clinical_diagnosis == "Ручной диагноз"
    assert panel.draft.completed_procedures[0].actual_count == 3
    assert "18.08.2026" in panel.draft.header_text
    assert not panel.draft.requires_period_rescan()
