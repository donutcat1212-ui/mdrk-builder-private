from __future__ import annotations

import tkinter as tk
from copy import deepcopy
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from mdrk_builder.application.editing import mark_manual_changes, merge_issues, merge_rows
from mdrk_builder.application.workspace import ReverseWorkspaceState
from mdrk_builder.domain import ReverseSheetDraft, ReverseSheetRow, ReviewIssue
from mdrk_builder.infrastructure.reverse_sheet_writer import write_reverse_sheet_docx
from mdrk_builder.ui.document_controls import OpenPath, add_document_source_access, safe_patient_name
from mdrk_builder.ui.episode_adapter import (
    format_date, format_datetime, parse_optional_date, parse_optional_datetime,
)
from mdrk_builder.ui.generation_review_dialog import confirm_generation_with_issues
from mdrk_builder.ui.inline_tree import InlineTreeEditor
from mdrk_builder.ui.reverse_sheet_dialog import incomplete_reverse_date_issues
from mdrk_builder.ui.source_access import (
    TableSourceAccess, SourceLinks, row_source_links, field_source_links, open_source_links,
)


def _reverse_group(row: ReverseSheetRow) -> str:
    value = f"{row.intervention} {row.performer}".casefold()
    groups = (
        (("неврол",), "Врач-невролог"),
        (("рефлекс",), "Рефлексотерапевт"),
        (("нейропсих", "психол"), "Медицинский психолог"),
        (("логопед",), "Медицинский логопед"),
        (("эрготерап",), "Эрготерапевт"),
        (("физио",), "Физиотерапевт"),
        (("лфк", "физической реабилитац"), "Врач ЛФК / ФРМ"),
    )
    for needles, label in groups:
        if any(needle in value for needle in needles):
            return label
    return "Другие"


class ReverseSheetPanel(ttk.Frame):
    def __init__(self, parent: tk.Misc, *, open_path: OpenPath) -> None:
        super().__init__(parent)
        self.draft: ReverseSheetDraft | None = None
        self._baseline = None
        self._open_path = open_path
        self._selected_group = ""
        self._row_refs: list[int] = []
        self._header_dirty: set[str] = set()
        self._rows_dirty = False
        self._populating = False
        self.on_dates_changed = lambda: None
        self._header_vars = {
            "full_name": tk.StringVar(),
            "birth_date": tk.StringVar(),
            "record_number": tk.StringVar(),
            "admission": tk.StringVar(),
            "discharge": tk.StringVar(),
        }
        for name, variable in self._header_vars.items():
            variable.trace_add("write", lambda *_args, field=name: self._mark_header_dirty(field))
        from mdrk_builder.ui.edit_history import install_history
        methods = ("_commit_row_cell", "_delete_rows", "_activate_row")
        self._table_history = install_history(self, methods, lambda: self.draft, self._restore_table_state)
        self._build()

    def _build(self) -> None:
        notebook = ttk.Notebook(self)
        notebook.pack(fill="both", expand=True, padx=6, pady=(0, 5))
        notebook.enable_traversal()

        main = ttk.Frame(notebook, padding=7)
        notebook.add(main, text="Основное")
        labels = (
            ("full_name", "ФИО пациента"),
            ("birth_date", "Дата рождения"),
            ("record_number", "Номер медкарты"),
            ("admission", "Поступление"),
            ("discharge", "Срез на дату (необязательно)"),
        )
        for index, (name, label) in enumerate(labels):
            row, pair = divmod(index, 2)
            ttk.Label(main, text=label).grid(row=row * 2, column=pair, sticky="w", padx=(0, 8), pady=(4, 2))
            ttk.Entry(main, textvariable=self._header_vars[name]).grid(
                row=row * 2 + 1, column=pair, sticky="ew", padx=(0, 12), pady=(0, 7)
            )
        main.columnconfigure(0, weight=1)
        main.columnconfigure(1, weight=1)
        self.header_source_button = ttk.Button(
            main, text="Источник", command=self._open_header_source
        )
        self.header_source_button.grid(row=4, column=1, sticky="e", padx=(0, 12), pady=(8, 0))

        specialists = ttk.Frame(notebook, padding=7)
        notebook.add(specialists, text="Специалисты")
        pane = ttk.Panedwindow(specialists, orient="horizontal")
        pane.pack(fill="both", expand=True)
        left = ttk.Frame(pane)
        right = ttk.Frame(pane)
        pane.add(left, weight=1)
        pane.add(right, weight=4)
        self.group_tree = ttk.Treeview(left, columns=("count",), show="tree")
        self.group_tree.pack(fill="both", expand=True)
        self.group_tree.bind("<<TreeviewSelect>>", self._on_group_selected)

        row_bar = ttk.Frame(right)
        row_bar.pack(fill="x", pady=(0, 4))
        self.group_title = tk.StringVar()
        ttk.Label(row_bar, textvariable=self.group_title).pack(side="left")

        table = ttk.Frame(right)
        table.pack(fill="both", expand=True)
        columns = ("intervention", "appointment", "performed", "performer")
        self.row_tree = ttk.Treeview(table, columns=columns, show="headings", selectmode="extended")
        for name, label, width in (
            ("intervention", "Медицинское вмешательство", 390),
            ("appointment", "Дата назначения", 130),
            ("performed", "Дата исполнения", 155),
            ("performer", "Исполнитель", 190),
        ):
            self.row_tree.heading(name, text=label)
            self.row_tree.column(name, width=width, minwidth=80, anchor="w")
        scroll = ttk.Scrollbar(table, orient="vertical", command=self.row_tree.yview)
        self.row_tree.configure(yscrollcommand=scroll.set)
        self.row_tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self.row_tree.tag_configure("new", foreground="#1f63c5")
        self._row_editor = InlineTreeEditor(
            self.row_tree,
            editable_columns=set(columns),
            commit=self._commit_row_cell,
            activate=self._activate_row,
            is_data_row=str.isdigit,
        )
        self.row_tree.bind("<Delete>", self._delete_rows)
        self.row_tree.bind("<<TreeviewSelect>>", lambda _event: self._update_row_source())
        self._row_sources = TableSourceAccess(
            self.row_tree, row_bar, links=self._row_source_links, open_path=self._open_path,
        )
        self.row_source_button = self._row_sources.button

        sources = ttk.Frame(notebook, padding=7)
        notebook.add(sources, text="Источники")
        self.source_tree = ttk.Treeview(sources, columns=("path",), show="headings")
        self.source_tree.heading("path", text="Документ")
        self.source_tree.column("path", width=900, anchor="w")
        self.source_tree.pack(fill="both", expand=True)
        self.source_tree.bind("<Double-1>", self._open_selected_source)

        warnings = ttk.Frame(notebook, padding=7)
        notebook.add(warnings, text="Предупреждения")
        self.warning_tree = ttk.Treeview(warnings, columns=("message", "source"), show="headings")
        self.warning_tree.heading("message", text="Сообщение")
        self.warning_tree.heading("source", text="Источник")
        self.warning_tree.column("message", width=650, anchor="w")
        self.warning_tree.column("source", width=300, anchor="w")
        self.warning_tree.pack(fill="both", expand=True)
        self._source_access = add_document_source_access(self.source_tree, self.warning_tree, self._open_path)
        self.warning_tree.bind("<Return>", self._go_to_warning)
        self.warning_tree.bind("<Double-1>", self._go_to_warning)

    def _go_to_warning(self, event=None):
        from mdrk_builder.ui.workspace_search import reveal
        selected=self.warning_tree.selection()
        if not selected:return
        values=self.warning_tree.item(selected[0],"values")
        field=str(values[1]).split(".")[0] if len(values)>2 else "rows"
        widget=getattr(self,"_widgets",{}).get(field)
        if widget is None:
            widget=getattr(self,"icf_tree",None) if field in {"icf","rehabilitation_diagnosis"} else getattr(self,"clinical_tree",getattr(self,"row_tree",None))
        if widget is not None:reveal(widget)

    def _restore_table_state(self, draft):
        if self.draft is None: return
        self.draft.rows = deepcopy(draft.rows)
        self._rows_dirty = True
        self._refresh_groups()
        self._refresh_rows()
        self._refresh_warnings()

    def capture_state(self):
        if self.draft is None:
            return None
        return deepcopy(ReverseWorkspaceState(
            self.draft, self._baseline, self._rows_dirty, self._header_dirty,
            {key: variable.get() for key, variable in self._header_vars.items()},
        ))

    def validate_state(self, state):
        if state is not None and (state.entries.keys() - self._header_vars.keys()):
            raise ValueError('Неизвестные поля документа в черновике')

    def restore_state(self, state):
        self.clear()
        if state is None:
            return
        self.load(state.draft)
        self._baseline = state.baseline or deepcopy(state.draft)
        self._populating = True
        try:
            for key, value in state.entries.items():
                self._header_vars[key].set(value)
        finally:
            self._populating = False
        self._rows_dirty = state.rows_dirty
        self._header_dirty = set(state.header_dirty)

    def clear(self):
        self._row_editor.cancel()
        self.draft = self._baseline = None
        self._table_history.clear()
        self._header_dirty.clear()
        self._rows_dirty = False
        self._row_refs.clear()
        self._selected_group = ''
        self._last_change_summary = ''
        self._populating = True
        try:
            for variable in self._header_vars.values():
                variable.set('')
        finally:
            self._populating = False
        self.group_title.set('')
        for tree in (self.group_tree, self.row_tree, self.source_tree, self.warning_tree):
            tree.delete(*tree.get_children())
        self.header_source_button.configure(text='Источник не указан', state='disabled')
        self._update_row_source()

    def load(self, draft: ReverseSheetDraft) -> None:
        self.draft = draft
        self._baseline = deepcopy(draft)
        if hasattr(self, "_table_history"):
            self._table_history.clear()
        self._header_dirty.clear()
        self._rows_dirty = False
        self._populate()

    def merge_scan(self, draft: ReverseSheetDraft) -> bool:
        if self.draft is None:
            self.load(draft)
            return True
        if not self.apply():
            return False
        previous = self.draft
        source_baseline = deepcopy(draft)
        from mdrk_builder.application.editing import change_summary
        self._last_change_summary = change_summary(getattr(self, "_baseline", None), draft)
        if self._rows_dirty:
            draft.rows, notes = merge_rows(getattr(self, "_baseline", previous).rows, previous.rows, draft.rows)
            draft.issues.extend(merge_issues(notes))
        if "full_name" in self._header_dirty:
            draft.identity.full_name = previous.identity.full_name
        if "birth_date" in self._header_dirty:
            draft.identity.birth_date = previous.identity.birth_date
        if "record_number" in self._header_dirty:
            draft.identity.medical_record_number = previous.identity.medical_record_number
        if "admission" in self._header_dirty:
            draft.admission_datetime = previous.admission_datetime
        if "discharge" in self._header_dirty:
            draft.discharge_datetime = previous.discharge_datetime
        for name in self._header_dirty:
            draft.field_sources.pop(name, None)
            origin = previous.field_sources.get(name, previous.header_source)
            if origin is not None:
                draft.field_sources[name] = origin
        self._baseline = source_baseline
        if hasattr(self, "_table_history"):
            self._table_history.clear()
        self.draft = draft
        self._populate()
        return True

    def _populate(self) -> None:
        if self.draft is None:
            return
        self._populating = True
        self._header_vars["full_name"].set(self.draft.identity.full_name)
        self._header_vars["birth_date"].set(format_date(self.draft.identity.birth_date))
        self._header_vars["record_number"].set(self.draft.identity.medical_record_number)
        self._header_vars["admission"].set(format_datetime(self.draft.admission_datetime))
        self._header_vars["discharge"].set(format_datetime(self.draft.discharge_datetime))
        self._populating = False
        if self.draft.header_source is None and not self.draft.field_sources and not self._header_dirty:
            self.header_source_button.configure(text="Источник не указан", state="disabled")
        else:
            self.header_source_button.configure(text="Источник", state="normal")
        self._refresh_groups()
        self._refresh_sources()
        self._refresh_warnings()

    def _mark_header_dirty(self, field: str) -> None:
        if not self._populating:
            self._header_dirty.add(field)
            if field in {"admission", "discharge"}:
                self.on_dates_changed()

    def _refresh_groups(self) -> None:
        self.group_tree.delete(*self.group_tree.get_children())
        if self.draft is None:
            return
        grouped: dict[str, int] = {}
        for row in self.draft.rows:
            group = _reverse_group(row)
            grouped[group] = grouped.get(group, 0) + 1
        for index, (group, count) in enumerate(grouped.items()):
            self.group_tree.insert("", "end", iid=f"group:{index}", text=f"{group}  ({count})", values=(group,))
        if not grouped:
            grouped["Другие"] = 0
            self.group_tree.insert("", "end", iid="group:0", text="Другие  (0)", values=("Другие",))
        groups = list(grouped)
        if self._selected_group not in grouped:
            self._selected_group = groups[0]
        selected = groups.index(self._selected_group)
        self.group_tree.selection_set(f"group:{selected}")
        self._refresh_rows()

    def _on_group_selected(self, _event: tk.Event | None = None) -> None:
        selected = self.group_tree.selection()
        if selected:
            values = self.group_tree.item(selected[0], "values")
            if values:
                self._selected_group = str(values[0])
        self._refresh_rows()

    def _refresh_rows(self) -> None:
        self.row_tree.delete(*self.row_tree.get_children())
        self._row_refs = []
        self.group_title.set(self._selected_group)
        if self.draft is not None:
            for index, row in enumerate(self.draft.rows):
                if _reverse_group(row) != self._selected_group:
                    continue
                item_id = str(len(self._row_refs))
                self._row_refs.append(index)
                self.row_tree.insert(
                    "", "end", iid=item_id,
                    values=(row.intervention, format_date(row.appointment_date), format_datetime(row.performed_at), row.performer),
                )
        self.row_tree.insert("", "end", iid="new:reverse", tags=("new",), values=("＋ Новая строка…", "", "", ""))
        self._update_row_source()

    def _activate_row(self, item_id: str) -> bool:
        if item_id != "new:reverse" or self.draft is None:
            return False
        self.draft.rows.append(ReverseSheetRow(""))
        self._rows_dirty = True
        index = len(self.draft.rows) - 1
        self._row_refs.append(index)
        local_id = str(len(self._row_refs) - 1)
        self.row_tree.insert("", len(self.row_tree.get_children()) - 1, iid=local_id, values=("", "", "", ""))
        self.row_tree.selection_set(local_id)
        self.after(1, lambda: self._row_editor.edit(local_id, "intervention"))
        return True

    def _commit_row_cell(self, item_id: str, column: str, value: str) -> None:
        if self.draft is None or not item_id.isdigit():
            return
        row = self.draft.rows[self._row_refs[int(item_id)]]
        previous = deepcopy(row)
        cleaned = value.strip()
        if column == "intervention":
            row.intervention = cleaned
        elif column == "appointment":
            row.appointment_date = parse_optional_date(cleaned)
        elif column == "performed":
            row.performed_at = parse_optional_datetime(cleaned)
        elif column == "performer":
            row.performer = cleaned
        mark_manual_changes(previous, row)
        self._rows_dirty = True
        self._refresh_groups()
        self._refresh_warnings()

    def _delete_rows(self, _event: tk.Event | None = None) -> str:
        if self.draft is None:
            return "break"
        indices = sorted(
            (self._row_refs[int(item)] for item in self.row_tree.selection() if item.isdigit()),
            reverse=True,
        )
        for index in indices:
            del self.draft.rows[index]
        if indices:
            self._rows_dirty = True
            self._refresh_groups()
            self._refresh_warnings()
        return "break"

    def _refresh_sources(self) -> None:
        self.source_tree.delete(*self.source_tree.get_children())
        if self.draft is None:
            return
        paths = {row.source for row in self.draft.rows if row.source is not None}
        if self.draft.header_source is not None:
            paths.add(self.draft.header_source)
        for index, path in enumerate(sorted(paths, key=lambda item: str(item).casefold())):
            self.source_tree.insert("", "end", iid=str(index), values=(str(path),))

    def _refresh_warnings(self) -> None:
        self.warning_tree.delete(*self.warning_tree.get_children())
        if self.draft is None:
            return
        for index, issue in enumerate(self.review_issues()):
            self.warning_tree.insert("", "end", iid=str(index), values=(issue.message, str(issue.source or "")))

    def _update_row_source(self) -> None:
        self._row_sources.refresh()

    def _row_source_links(self, item: str) -> SourceLinks:
        if self.draft is None or not item.isdigit() or int(item) >= len(self._row_refs):
            return ()
        row = self.draft.rows[self._row_refs[int(item)]]
        links = list(row_source_links(row, "Документ вмешательства"))
        labels = {"appointment_date": "Дата назначения", "performed_at": "Дата исполнения", "performer": "Исполнитель"}
        links.extend((labels.get(key, key), path) for key, path in row.field_sources.items())
        return links

    def _selected_row(self) -> ReverseSheetRow | None:
        if self.draft is None:
            return None
        selected = self.row_tree.selection()
        if not selected or not selected[0].isdigit():
            return None
        return self.draft.rows[self._row_refs[int(selected[0])]]

    def _open_header_source(self) -> None:
        links = []
        if self.draft is not None:
            labels = {"full_name": "ФИО", "birth_date": "Дата рождения",
                      "record_number": "Номер ИБ", "admission": "Поступление"}
            for name, label in labels.items():
                origin = self.draft.field_sources.get(name) if name in self._header_dirty else self.draft.field_sources.get(name, self.draft.header_source)
                entries = field_source_links({name: origin} if origin else {}, name,
                                             manual=name in self._header_dirty)
                links.extend((f"{label}: {text}", path) for text, path in
                             entries or [("Источник не указан", None)])
        open_source_links(self.header_source_button, links, self._open_path)

    def _open_selected_row_source(self) -> None:
        row = self._selected_row()
        self._open_path(row.source if row else None)

    def _open_selected_source(self, _event: tk.Event | None = None) -> None:
        selected = self.source_tree.selection()
        if selected:
            values = self.source_tree.item(selected[0], "values")
            self._open_path(Path(str(values[0])) if values else None)

    def apply(self) -> bool:
        if self.draft is None:
            return False
        try:
            birth_date = parse_optional_date(self._header_vars["birth_date"].get())
            admission = parse_optional_datetime(self._header_vars["admission"].get())
            discharge = parse_optional_datetime(self._header_vars["discharge"].get())
        except ValueError as exc:
            messagebox.showerror("Проверьте поля", str(exc), parent=self)
            return False
        self.draft.identity.full_name = self._header_vars["full_name"].get().strip()
        self.draft.identity.birth_date = birth_date
        self.draft.identity.medical_record_number = self._header_vars["record_number"].get().strip()
        self.draft.admission_datetime = admission
        self.draft.discharge_datetime = discharge
        return True

    def review_issues(self) -> tuple[ReviewIssue, ...]:
        if self.draft is None:
            return ()
        return (*self.draft.issues, *incomplete_reverse_date_issues(self.draft.rows, self.draft.admission_datetime, self.draft.discharge_datetime))

    def save(self, *, defer=False):
        if self.draft is None or not self.apply():
            return None
        issues = self.review_issues()
        if not confirm_generation_with_issues(self, issues, document_name="Оборотный лист"):
            return None
        output = filedialog.asksaveasfilename(
            parent=self, title="Сохранить оборотный лист", defaultextension=".docx",
            filetypes=(("Документ Word", "*.docx"),),
            initialfile=f"Оборотный лист {safe_patient_name(self.draft.identity.full_name)}.docx",
        )
        if not output:
            return None
        if defer:
            from functools import partial
            return partial(write_reverse_sheet_docx, deepcopy(self.draft), Path(output))
        return write_reverse_sheet_docx(self.draft, Path(output))
