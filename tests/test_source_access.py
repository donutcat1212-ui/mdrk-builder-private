import tkinter as tk
from pathlib import Path
from types import SimpleNamespace
from tkinter import ttk

import pytest

from mdrk_builder.domain import (
    Episode, IcfDomain, IcfQualifier, MdrkKind, Procedure,
    ReverseSheetDraft, ReverseSheetRow, ReviewIssue, ScaleMeasurement, SpecialistRole,
)
from mdrk_builder.ui.app import MdrkBuilderApp
from mdrk_builder.ui.reverse_sheet_panel import ReverseSheetPanel
from mdrk_builder.ui.source_access import TableSourceAccess


@pytest.fixture
def root():
    try:
        window = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"Tk display unavailable: {exc}")
    window.withdraw()
    yield window
    window.destroy()


def test_context_menu_targets_clicked_row_and_keeps_both_sources(root, monkeypatch):
    tree = ttk.Treeview(root, columns=("value",))
    tree.insert("", "end", iid="first")
    tree.insert("", "end", iid="second")
    tree.selection_set("first")
    opened = []
    links = {
        "first": [("Первый", Path("wrong.docx"))],
        "second": [("Исходный", Path("initial.docx")), ("Повторный", Path("final.docx"))],
    }
    access = TableSourceAccess(tree, root, links=links.__getitem__, open_path=opened.append)
    monkeypatch.setattr(tree, "identify_row", lambda _y: "second")
    monkeypatch.setattr(access, "_show_menu", lambda _x, _y: access._fill_menu())
    assert access._pointer_menu(SimpleNamespace(y=25, x_root=10, y_root=10)) == "break"
    assert tree.selection() == ("second",)
    access.menu.invoke(0)
    access.menu.invoke(1)
    assert opened == [Path("initial.docx"), Path("final.docx")]
    links["second"] = [("Исходный", None)]
    access.refresh()
    access._fill_menu()
    assert access.button.instate(["disabled"])
    assert access.menu.entrycget(0, "state") == "disabled"
    assert tree.bind("<Shift-F10>")
    assert tree.bind("<Button-3>")


def test_source_button_opens_single_file_and_resolves_replaced_data(root):
    tree = ttk.Treeview(root)
    tree.insert("", "end", iid="0")
    tree.selection_set("0")
    links = [("Исходный", Path("one.docx")), ("Повторный", Path("one.docx"))]
    opened = []
    access = TableSourceAccess(tree, root, links=lambda _item: links, open_path=opened.append)
    access.button.invoke()
    links[:] = [("Новый", Path("two.docx"))]
    access.button.invoke()
    assert opened == [Path("one.docx"), Path("two.docx")]


def test_main_workspace_exposes_recorded_sources_for_tables(root):
    app = MdrkBuilderApp(root)
    episode = Episode(folder=Path("/synthetic"))
    initial = Path("initial.docx")
    final = Path("final.docx")
    episode.icf_domains.append(IcfDomain(
        "d450", "Ходьба", SpecialistRole.PHYSICAL_THERAPIST,
        initial=IcfQualifier(2), final=IcfQualifier(1),
        initial_source=initial, final_source=final,
    ))
    episode.procedures.append(Procedure("Ходьба", "ФТ", 3, source=Path("assignments.docx")))
    app.episode = episode
    assert list(app._table_source_links("icf", "0")) == [("Исходный документ", initial)]
    app._current_kind = MdrkKind.FINAL
    assert [p for _, p in app._table_source_links("icf", "0")] == [initial, final]
    assert app._table_source_links("icf", "section:body_functions") == ()
    assert app._table_source_links("icf", "new:icf") == ()
    assert app._table_source_links("procedure", "0")[0][1] == Path("assignments.docx")
    app._scale_pair_refs["scale:0"] = SimpleNamespace(
        initial=ScaleMeasurement("Шкала", "2", None, SpecialistRole.OTHER, initial),
        current=ScaleMeasurement("Шкала", "1", None, SpecialistRole.OTHER, final),
    )
    assert [p for _, p in app._table_source_links("scale", "scale:0")] == [initial, final]
    assert set(app._table_sources) == {"icf", "scale", "procedure", "finding", "issue", "source"}
    app.episode = None
    assert app._table_source_links("icf", "0") == ()


def test_missing_source_file_is_reported(monkeypatch):
    messages = []
    monkeypatch.setattr("mdrk_builder.ui.app.messagebox.showerror", lambda *args: messages.append(args))
    MdrkBuilderApp._open_path(Path("/nonexistent/source.docx"))
    assert messages and "Файл не найден" in messages[0][1]


def test_reverse_sheet_sources_follow_filtered_rows_and_warnings(root):
    opened = []
    panel = ReverseSheetPanel(root, open_path=opened.append)
    draft = ReverseSheetDraft(folder=Path("/synthetic"), rows=[
        ReverseSheetRow("ЛФК", source=Path("lfk.docx")),
        ReverseSheetRow("Логопед", source=Path("speech.docx")),
    ], issues=[ReviewIssue("check", "Проверить", source=Path("warning.docx"))])
    panel.load(draft)
    panel._selected_group = "Медицинский логопед"
    panel._refresh_rows()
    panel.row_tree.selection_set("0")
    panel._row_sources.refresh()
    panel.row_source_button.invoke()
    assert opened == [Path("speech.docx")]
    panel.warning_tree.selection_set("0")
    panel._source_access[1].refresh()
    panel._source_access[1].button.invoke()
    assert opened[-1] == Path("warning.docx")
    panel.load(ReverseSheetDraft(folder=Path("/synthetic")))
    panel._row_sources.refresh()
    assert panel.row_source_button.instate(["disabled"])
