import tkinter as tk
from tkinter import ttk

import pytest

from mdrk_builder.ui.inline_tree import InlineTreeEditor


@pytest.fixture
def tree():
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"Tk display unavailable: {exc}")
    root.geometry("600x200")
    table = ttk.Treeview(
        root, columns=("code", "initial", "final", "responsible"),
        displaycolumns=("code", "initial", "responsible"), show="headings",
    )
    table.pack(fill="both", expand=True)
    table.insert("", "end", iid="row", values=("d450", "2", "1", "ЛФК"))
    table.selection_set("row")
    root.update()
    yield table
    root.destroy()


def test_hidden_repeat_column_does_not_redirect_responsible_edit(tree):
    commits = []
    editor = InlineTreeEditor(
        tree, editable_columns={"initial", "final", "responsible"},
        commit=lambda *args: commits.append(args),
    )
    assert editor._column_name("#3") == "responsible"
    editor.edit("row", editor._column_name("#3"))
    assert editor._widget.get() == "ЛФК"
    assert int(editor._widget.place_info()["x"]) == tree.bbox("row", "responsible")[0]
    editor._widget.delete(0, "end")
    editor._widget.insert(0, "ФРМ")
    editor.accept("row", "responsible")
    assert commits == [("row", "responsible", "ФРМ")]


@pytest.mark.parametrize("displayed, expected", [
    (("code", "initial", "responsible"), "initial"),
    (("responsible", "initial"), "responsible"),
    ("#all", "initial"),
    (("code",), None),
])
def test_f2_uses_first_visible_editable_column(tree, displayed, expected):
    tree.configure(displaycolumns=displayed)
    editor = InlineTreeEditor(
        tree, editable_columns={"final", "responsible", "initial"},
        commit=lambda *args: None,
    )
    opened = []
    editor.edit = lambda item, column: opened.append((item, column))
    editor._on_f2(None)
    assert opened == ([] if expected is None else [("row", expected)])
