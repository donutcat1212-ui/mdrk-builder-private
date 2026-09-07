from __future__ import annotations

import tkinter as tk
from collections.abc import Callable, Sequence
from tkinter import messagebox, ttk


CommitCallback = Callable[[str, str, str], None]
ValuesCallback = Callable[[str, str], Sequence[str] | None]
ActivateCallback = Callable[[str], bool]


class InlineTreeEditor:
    """Small in-place editor for ttk.Treeview data cells."""

    def __init__(
        self,
        tree: ttk.Treeview,
        *,
        editable_columns: set[str],
        commit: CommitCallback,
        values: ValuesCallback | None = None,
        activate: ActivateCallback | None = None,
    ) -> None:
        self.tree = tree
        self.editable_columns = editable_columns
        self.commit = commit
        self.values = values
        self.activate = activate
        self._widget: ttk.Entry | ttk.Combobox | None = None
        self._closing = False
        tree.bind("<Double-1>", self._on_double_click, add="+")
        tree.bind("<F2>", self._on_f2, add="+")

    def _column_name(self, display_column: str) -> str | None:
        if display_column == "#0":
            return None
        try:
            return str(self.tree.column(display_column, "id"))
        except tk.TclError:
            return None

    def _on_double_click(self, event: tk.Event) -> str | None:
        item_id = self.tree.identify_row(event.y)
        if not item_id:
            return None
        if self.activate is not None and self.activate(item_id):
            return "break"
        column = self._column_name(self.tree.identify_column(event.x))
        if column is None or column not in self.editable_columns:
            return None
        self.edit(item_id, column)
        return "break"

    def _on_f2(self, _event: tk.Event) -> str | None:
        selected = self.tree.selection()
        if not selected:
            return None
        item_id = selected[0]
        if self.activate is not None and self.activate(item_id):
            return "break"
        displayed = tuple(self.tree.cget("displaycolumns"))
        if displayed == ("#all",):
            displayed = tuple(self.tree.cget("columns"))
        column = next(
            (name for identifier in displayed
             if (name := self._column_name(identifier)) in self.editable_columns),
            None,
        )
        if column is None:
            return None
        self.edit(item_id, column)
        return "break"

    def edit(self, item_id: str, column: str) -> None:
        if column not in self.editable_columns:
            return
        if column not in tuple(self.tree.cget("columns")):
            return
        box = self.tree.bbox(item_id, column)
        if not box:
            self.tree.see(item_id)
            box = self.tree.bbox(item_id, column)
        if not box:
            return
        self.cancel()
        x, y, width, height = box
        current = str(self.tree.set(item_id, column))
        choices = self.values(item_id, column) if self.values is not None else None
        if choices is None:
            widget: ttk.Entry | ttk.Combobox = ttk.Entry(self.tree)
            widget.insert(0, current)
        else:
            variable = tk.StringVar(value=current)
            widget = ttk.Combobox(
                self.tree,
                textvariable=variable,
                values=tuple(choices),
                state="readonly",
            )
        widget.place(x=x, y=y, width=width, height=height)
        widget.focus_set()
        if isinstance(widget, ttk.Entry):
            widget.selection_range(0, "end")
        self._widget = widget
        self._closing = False
        widget.bind("<Return>", lambda _event: self.accept(item_id, column))
        widget.bind("<Tab>", lambda event: self._next_cell(item_id, column, 1))
        widget.bind("<Shift-Tab>", lambda event: self._next_cell(item_id, column, -1))
        widget.bind("<ISO_Left_Tab>", lambda event: self._next_cell(item_id, column, -1))
        widget.bind("<Escape>", lambda _event: self.cancel())
        widget.bind("<FocusOut>", lambda _event: self.accept(item_id, column))

    def _next_cell(self, item_id, column, step):
        columns = [c for c in self.tree["columns"] if c in self.editable_columns]
        rows = []
        def visit(parent=""):
            for item in self.tree.get_children(parent):
                rows.append(item)
                visit(item)
        visit()
        if item_id not in rows or column not in columns:
            return "break"
        index = rows.index(item_id) * len(columns) + columns.index(column) + step
        self.accept(item_id, column)
        if self._widget is not None:
            return "break"
        while 0 <= index < len(rows) * len(columns):
            row, col = rows[index // len(columns)], columns[index % len(columns)]
            if self.activate is None or self.activate(row):
                self.tree.selection_set(row)
                self.edit(row, col)
                break
            index += step
        return "break"

    def accept(self, item_id: str, column: str) -> str:
        if self._widget is None or self._closing:
            return "break"
        self._closing = True
        value = self._widget.get()
        try:
            self.commit(item_id, column, value)
        except ValueError as exc:
            self._closing = False
            messagebox.showerror("Проверьте значение", str(exc), parent=self.tree)
            self._widget.focus_set()
            return "break"
        self.cancel()
        return "break"

    def cancel(self) -> str:
        widget, self._widget = self._widget, None
        self._closing = False
        if widget is not None:
            widget.destroy()
        return "break"
