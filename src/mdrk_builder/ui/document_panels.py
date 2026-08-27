from __future__ import annotations

import re
import tkinter as tk
from collections.abc import Callable
from copy import deepcopy
from pathlib import Path
from tkinter import filedialog, messagebox, scrolledtext, ttk

from mdrk_builder.domain import (
    DischargeSummaryDraft,
    ReverseSheetDraft,
    ReverseSheetRow,
    ReviewIssue,
    ReviewSeverity,
)
from mdrk_builder.infrastructure.discharge_summary_writer import write_discharge_summary_docx
from mdrk_builder.infrastructure.reverse_sheet_writer import write_reverse_sheet_docx
from mdrk_builder.ui.discharge_summary_dialog import (
    DISCHARGE_FIELD_GROUPS,
    apply_discharge_form,
)
from mdrk_builder.ui.episode_adapter import (
    format_date,
    format_datetime,
    parse_optional_date,
    parse_optional_datetime,
)
from mdrk_builder.ui.generation_review_dialog import confirm_generation_with_issues
from mdrk_builder.ui.inline_tree import InlineTreeEditor
from mdrk_builder.ui.reverse_sheet_dialog import incomplete_reverse_date_issues


OpenPath = Callable[[Path | None], None]


def _safe_patient_name(value: str) -> str:
    return re.sub(r"[^0-9A-Za-zА-Яа-яЁё_-]+", "_", value).strip("_") or "пациент"


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
        self._open_path = open_path
        self._selected_group = ""
        self._row_refs: list[int] = []
        self._header_dirty: set[str] = set()
        self._rows_dirty = False
        self._populating = False
        self._header_vars = {
            "full_name": tk.StringVar(),
            "birth_date": tk.StringVar(),
            "record_number": tk.StringVar(),
            "admission": tk.StringVar(),
        }
        for name, variable in self._header_vars.items():
            variable.trace_add("write", lambda *_args, field=name: self._mark_header_dirty(field))
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
        self.row_source_button = ttk.Button(row_bar, text="Источник", command=self._open_selected_row_source)
        self.row_source_button.pack(side="right")

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
        )
        self.row_tree.bind("<Delete>", self._delete_rows)
        self.row_tree.bind("<<TreeviewSelect>>", lambda _event: self._update_row_source())

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

    def load(self, draft: ReverseSheetDraft) -> None:
        self.draft = draft
        self._header_dirty.clear()
        self._rows_dirty = False
        self._populate()

    def merge_scan(self, draft: ReverseSheetDraft) -> None:
        if self.draft is None:
            self.load(draft)
            return
        self.apply()
        previous = self.draft
        if self._rows_dirty:
            draft.rows = deepcopy(previous.rows)
        if "full_name" in self._header_dirty:
            draft.identity.full_name = previous.identity.full_name
        if "birth_date" in self._header_dirty:
            draft.identity.birth_date = previous.identity.birth_date
        if "record_number" in self._header_dirty:
            draft.identity.medical_record_number = previous.identity.medical_record_number
        if "admission" in self._header_dirty:
            draft.admission_datetime = previous.admission_datetime
        self.draft = draft
        self._populate()

    def _populate(self) -> None:
        if self.draft is None:
            return
        self._populating = True
        self._header_vars["full_name"].set(self.draft.identity.full_name)
        self._header_vars["birth_date"].set(format_date(self.draft.identity.birth_date))
        self._header_vars["record_number"].set(self.draft.identity.medical_record_number)
        self._header_vars["admission"].set(format_datetime(self.draft.admission_datetime))
        self._populating = False
        if self.draft.header_source is None:
            self.header_source_button.grid_remove()
        else:
            self.header_source_button.grid()
        self._refresh_groups()
        self._refresh_sources()
        self._refresh_warnings()

    def _mark_header_dirty(self, field: str) -> None:
        if not self._populating:
            self._header_dirty.add(field)

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
        cleaned = value.strip()
        if column == "intervention":
            row.intervention = cleaned
        elif column == "appointment":
            row.appointment_date = parse_optional_date(cleaned)
        elif column == "performed":
            row.performed_at = parse_optional_datetime(cleaned)
        elif column == "performer":
            row.performer = cleaned
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
        row = self._selected_row()
        if row is None or row.source is None:
            self.row_source_button.pack_forget()
        elif not self.row_source_button.winfo_manager():
            self.row_source_button.pack(side="right")

    def _selected_row(self) -> ReverseSheetRow | None:
        if self.draft is None:
            return None
        selected = self.row_tree.selection()
        if not selected or not selected[0].isdigit():
            return None
        return self.draft.rows[self._row_refs[int(selected[0])]]

    def _open_header_source(self) -> None:
        self._open_path(self.draft.header_source if self.draft else None)

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
            self.draft.identity.full_name = self._header_vars["full_name"].get().strip()
            self.draft.identity.birth_date = parse_optional_date(self._header_vars["birth_date"].get())
            self.draft.identity.medical_record_number = self._header_vars["record_number"].get().strip()
            self.draft.admission_datetime = parse_optional_datetime(self._header_vars["admission"].get())
        except ValueError as exc:
            messagebox.showerror("Проверьте поля", str(exc), parent=self)
            return False
        return True

    def review_issues(self) -> tuple[ReviewIssue, ...]:
        if self.draft is None:
            return ()
        return (*self.draft.issues, *incomplete_reverse_date_issues(self.draft.rows))

    def save(self) -> Path | None:
        if self.draft is None or not self.apply():
            return None
        issues = self.review_issues()
        if not confirm_generation_with_issues(self, issues, document_name="Оборотный лист"):
            return None
        output = filedialog.asksaveasfilename(
            parent=self, title="Сохранить оборотный лист", defaultextension=".docx",
            filetypes=(("Документ Word", "*.docx"),),
            initialfile=f"Оборотный_лист_{_safe_patient_name(self.draft.identity.full_name)}.docx",
        )
        if not output:
            return None
        return write_reverse_sheet_docx(self.draft, Path(output))


class DischargeSummaryPanel(ttk.Frame):
    def __init__(self, parent: tk.Misc, *, open_path: OpenPath) -> None:
        super().__init__(parent)
        self.draft: DischargeSummaryDraft | None = None
        self._open_path = open_path
        self._widgets: dict[str, tk.Text] = {}
        self._source_buttons: dict[str, ttk.Button] = {}
        self._dirty_fields: set[str] = set()
        self._dirty_identity: set[str] = set()
        self._populating = False
        self._identity_vars = {
            name: tk.StringVar()
            for name in ("full_name", "record_number", "birth_date", "sex", "admission", "discharge")
        }
        for name, variable in self._identity_vars.items():
            variable.trace_add("write", lambda *_args, field=name: self._mark_identity_dirty(field))
        self._build()

    def _build(self) -> None:
        self.notebook = ttk.Notebook(self)
        self.notebook.pack(fill="both", expand=True, padx=6, pady=(0, 5))
        self.notebook.enable_traversal()
        for group_index, (group_name, fields) in enumerate(DISCHARGE_FIELD_GROUPS):
            tab = ttk.Frame(self.notebook, padding=7)
            self.notebook.add(tab, text=group_name)
            row_offset = 0
            if group_index == 0:
                identity = ttk.Frame(tab)
                identity.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 8))
                identity_fields = (
                    ("full_name", "ФИО пациента"),
                    ("record_number", "Номер медкарты"),
                    ("birth_date", "Дата рождения"),
                    ("sex", "Пол"),
                    ("admission", "Поступление"),
                    ("discharge", "Выписка"),
                )
                for index, (name, label) in enumerate(identity_fields):
                    row, pair = divmod(index, 2)
                    ttk.Label(identity, text=label).grid(row=row * 2, column=pair, sticky="w", padx=(0, 8), pady=(2, 1))
                    ttk.Entry(identity, textvariable=self._identity_vars[name]).grid(
                        row=row * 2 + 1, column=pair, sticky="ew", padx=(0, 12), pady=(0, 5)
                    )
                identity.columnconfigure(0, weight=1)
                identity.columnconfigure(1, weight=1)
                self.identity_source_button = ttk.Button(
                    identity,
                    text="Источник",
                    command=self._open_identity_source,
                )
                self.identity_source_button.grid(row=6, column=1, sticky="e", padx=(0, 12), pady=(2, 0))
                row_offset = 1
            for index, field in enumerate(fields):
                row, column = divmod(index, 2)
                holder = ttk.Frame(tab)
                holder.grid(row=row + row_offset, column=column, sticky="nsew", padx=(0, 7) if column == 0 else (7, 0), pady=(0, 7))
                bar = ttk.Frame(holder)
                bar.pack(fill="x", pady=(0, 2))
                ttk.Label(bar, text=field.label).pack(side="left")
                button = ttk.Button(bar, text="Источник", command=lambda name=field.name: self._open_field_source(name))
                button.pack(side="right")
                self._source_buttons[field.name] = button
                widget = scrolledtext.ScrolledText(holder, height=max(7, field.height), wrap="word", undo=True)
                widget.pack(fill="both", expand=True)
                widget.bind("<KeyRelease>", lambda _event, name=field.name: self._mark_dirty(name))
                self._widgets[field.name] = widget
            for row in range(row_offset, row_offset + (len(fields) + 1) // 2):
                tab.rowconfigure(row, weight=1)
            tab.columnconfigure(0, weight=1)
            tab.columnconfigure(1, weight=1)

        sources = ttk.Frame(self.notebook, padding=7)
        self.notebook.add(sources, text="Источники")
        self.source_tree = ttk.Treeview(sources, columns=("field", "source"), show="headings")
        self.source_tree.heading("field", text="Поле")
        self.source_tree.heading("source", text="Документ")
        self.source_tree.column("field", width=280, anchor="w")
        self.source_tree.column("source", width=700, anchor="w")
        self.source_tree.pack(fill="both", expand=True)
        self.source_tree.bind("<Double-1>", self._open_selected_source)

        warnings = ttk.Frame(self.notebook, padding=7)
        self.notebook.add(warnings, text="Предупреждения")
        self.warning_tree = ttk.Treeview(warnings, columns=("message", "field", "source"), show="headings")
        for name, label, width in (
            ("message", "Сообщение", 600),
            ("field", "Поле", 180),
            ("source", "Источник", 300),
        ):
            self.warning_tree.heading(name, text=label)
            self.warning_tree.column(name, width=width, anchor="w")
        self.warning_tree.pack(fill="both", expand=True)

    def load(self, draft: DischargeSummaryDraft) -> None:
        self.draft = draft
        self._dirty_fields.clear()
        self._dirty_identity.clear()
        self._populate()

    def merge_scan(self, draft: DischargeSummaryDraft) -> None:
        if self.draft is None:
            self.load(draft)
            return
        self.apply()
        previous = self.draft
        for name in self._dirty_fields:
            setattr(draft, name, getattr(previous, name))
            draft.field_sources.pop(name, None)
        if "full_name" in self._dirty_identity:
            draft.identity.full_name = previous.identity.full_name
        if "record_number" in self._dirty_identity:
            draft.identity.medical_record_number = previous.identity.medical_record_number
        if "birth_date" in self._dirty_identity:
            draft.identity.birth_date = previous.identity.birth_date
        if "sex" in self._dirty_identity:
            draft.identity.sex = previous.identity.sex
        if "admission" in self._dirty_identity:
            draft.admission_datetime = previous.admission_datetime
        if "discharge" in self._dirty_identity:
            draft.discharge_datetime = previous.discharge_datetime
        self.draft = draft
        self._populate()

    def _populate(self) -> None:
        if self.draft is None:
            return
        self._populating = True
        self._identity_vars["full_name"].set(self.draft.identity.full_name)
        self._identity_vars["record_number"].set(self.draft.identity.medical_record_number)
        self._identity_vars["birth_date"].set(format_date(self.draft.identity.birth_date))
        self._identity_vars["sex"].set(self.draft.identity.sex)
        self._identity_vars["admission"].set(format_datetime(self.draft.admission_datetime))
        self._identity_vars["discharge"].set(format_datetime(self.draft.discharge_datetime))
        for name, widget in self._widgets.items():
            widget.delete("1.0", "end")
            widget.insert("1.0", getattr(self.draft, name))
        self._populating = False
        if self.draft.discharge_source is None:
            self.identity_source_button.grid_remove()
        else:
            self.identity_source_button.grid()
        self._refresh_sources()
        self._refresh_warnings()

    def _mark_dirty(self, name: str) -> None:
        if self._populating:
            return
        self._dirty_fields.add(name)
        self.draft.field_sources.pop(name, None) if self.draft is not None else None
        self._update_source_button(name)

    def _mark_identity_dirty(self, name: str) -> None:
        if not self._populating:
            self._dirty_identity.add(name)

    def _update_source_button(self, name: str) -> None:
        button = self._source_buttons[name]
        if self.draft is None or name not in self.draft.field_sources:
            button.pack_forget()
        elif not button.winfo_manager():
            button.pack(side="right")

    def _refresh_sources(self) -> None:
        self.source_tree.delete(*self.source_tree.get_children())
        if self.draft is None:
            return
        for name in self._source_buttons:
            self._update_source_button(name)
        rows = [(name, source) for name, source in self.draft.field_sources.items()]
        known = {source for _name, source in rows}
        for label, source in (
            ("Выписной эпикриз", self.draft.discharge_source),
            ("Первичный осмотр", self.draft.primary_neurologist_source),
            ("МДРК-2", self.draft.final_mdrk_source),
        ):
            if source is not None and source not in known:
                rows.append((label, source))
                known.add(source)
        for source in self.draft.source_paths:
            if source not in known:
                rows.append(("Документ эпизода", source))
                known.add(source)
        for index, (name, source) in enumerate(sorted(rows, key=lambda item: (item[0], str(item[1])))):
            self.source_tree.insert("", "end", iid=str(index), values=(name, str(source)))

    def _refresh_warnings(self) -> None:
        self.warning_tree.delete(*self.warning_tree.get_children())
        if self.draft is None:
            return
        for index, issue in enumerate(self.draft.issues):
            self.warning_tree.insert("", "end", iid=str(index), values=(issue.message, issue.field, str(issue.source or "")))

    def _open_field_source(self, name: str) -> None:
        self._open_path(self.draft.field_sources.get(name) if self.draft else None)

    def _open_identity_source(self) -> None:
        self._open_path(self.draft.discharge_source if self.draft else None)

    def _open_selected_source(self, _event: tk.Event | None = None) -> None:
        selected = self.source_tree.selection()
        if selected:
            values = self.source_tree.item(selected[0], "values")
            self._open_path(Path(str(values[1])) if len(values) > 1 else None)

    def apply(self) -> bool:
        if self.draft is None:
            return False
        try:
            self.draft.identity.full_name = self._identity_vars["full_name"].get().strip()
            self.draft.identity.medical_record_number = self._identity_vars["record_number"].get().strip()
            self.draft.identity.birth_date = parse_optional_date(self._identity_vars["birth_date"].get())
            self.draft.identity.sex = self._identity_vars["sex"].get().strip()
            self.draft.admission_datetime = parse_optional_datetime(self._identity_vars["admission"].get())
            self.draft.discharge_datetime = parse_optional_datetime(self._identity_vars["discharge"].get())
        except ValueError as exc:
            messagebox.showerror("Проверьте поля", str(exc), parent=self)
            return False
        apply_discharge_form(
            self.draft,
            {name: widget.get("1.0", "end-1c") for name, widget in self._widgets.items()},
        )
        return True

    def save(self) -> Path | None:
        if self.draft is None or not self.apply():
            return None
        issues = tuple(
            issue for issue in self.draft.issues
            if issue.severity in {ReviewSeverity.BLOCKING, ReviewSeverity.WARNING}
        )
        if not confirm_generation_with_issues(self, issues, document_name="Выписной эпикриз"):
            return None
        output = filedialog.asksaveasfilename(
            parent=self, title="Сохранить выписной эпикриз", defaultextension=".docx",
            filetypes=(("Документ Word", "*.docx"),),
            initialfile=f"Выписной_эпикриз_{_safe_patient_name(self.draft.identity.full_name)}.docx",
        )
        if not output:
            return None
        return write_discharge_summary_docx(self.draft, Path(output), ignore_issues=bool(issues))
