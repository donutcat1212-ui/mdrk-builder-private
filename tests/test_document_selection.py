from datetime import datetime
from types import SimpleNamespace

import pytest

from mdrk_builder.domain import Episode, MdrkKind
from mdrk_builder.ui.app import MdrkBuilderApp


class Variable:
    def __init__(self, value=""):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value


@pytest.mark.parametrize("current", [MdrkKind.INITIAL, MdrkKind.FINAL])
@pytest.mark.parametrize("invalid_date", [True, False])
def test_rejected_snapshot_switch_restores_document_selection(tmp_path, monkeypatch, current, invalid_date):
    app = MdrkBuilderApp.__new__(MdrkBuilderApp)
    app.episode = Episode(folder=tmp_path)
    app.episode.initial_meeting_at = datetime(2026, 8, 10, 8)
    app.episode.final_meeting_at = datetime(2026, 8, 20, 8)
    app._current_kind = current
    target = MdrkKind.FINAL if current is MdrkKind.INITIAL else MdrkKind.INITIAL
    app.kind_var = Variable(target.value)
    app.document_var = Variable("mdrk2" if target is MdrkKind.FINAL else "mdrk1")
    app.status_var = Variable()

    def parse_form():
        if invalid_date:
            raise ValueError("Некорректная дата")
        return SimpleNamespace(meeting_at=datetime(2026, 8, 15, 8))

    app._parsed_form_data = parse_form
    monkeypatch.setattr("mdrk_builder.ui.app.messagebox.showerror", lambda *args: None)
    app._on_kind_changed()
    assert app._current_kind is current
    assert app.kind_var.get() == current.value
    assert app.document_var.get() == ("mdrk1" if current is MdrkKind.INITIAL else "mdrk2")
