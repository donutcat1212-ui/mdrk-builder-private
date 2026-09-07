"""Shared source links and output filename formatting for document panels."""
import re
from collections.abc import Callable
from pathlib import Path
from tkinter import ttk
from mdrk_builder.ui.source_access import TableSourceAccess, path_column_links

OpenPath = Callable[[Path | None], None]


def add_document_source_access(
    source_tree: ttk.Treeview, warning_tree: ttk.Treeview, open_path: OpenPath,
) -> tuple[TableSourceAccess, ...]:
    controls = []
    for tree in (source_tree, warning_tree):
        bar = ttk.Frame(tree.master)
        bar.pack(fill="x", pady=(0, 5), before=tree)
        column = "source" if "source" in tree["columns"] else "path"
        controls.append(TableSourceAccess(
            tree, bar, links=lambda item, t=tree, c=column: path_column_links(t, item, c),
            open_path=open_path,
        ))
    return tuple(controls)


def safe_patient_name(value: str) -> str:
    return re.sub(r"[^0-9A-Za-zА-Яа-яЁё-]+", " ", value).strip() or "пациент"
