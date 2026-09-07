from __future__ import annotations

import re
import tkinter as tk
from collections.abc import Callable
from copy import deepcopy
from pathlib import Path
from tkinter import filedialog, messagebox, scrolledtext, ttk

from mdrk_builder.application.discharge_extractors import update_header_period
from mdrk_builder.application.editing import merge_rows, merge_issues
from mdrk_builder.domain import (
    DischargeSummaryDraft,
    ReverseSheetDraft,
    ReverseSheetRow,
    ReviewIssue,
    ReviewSeverity,
    IcfSection,
    SpecialistRole,
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
from mdrk_builder.ui.icf_table import apply_icf_grid_style
from mdrk_builder.ui.source_access import (
    TableSourceAccess, SourceLinks, icf_source_links, path_column_links,
    row_source_links, field_source_links, open_source_links, mark_manual_changes,
)
from mdrk_builder.ui.reverse_sheet_dialog import incomplete_reverse_date_issues


OpenPath = Callable[[Path | None], None]


def _add_document_source_access(
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


def _safe_patient_name(value: str) -> str:
    return re.sub(r"[^0-9A-Za-zА-Яа-яЁё-]+", " ", value).strip() or "пациент"


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
        from mdrk_builder.ui.edit_history import install_history
        methods = [name for name in ("_commit_row_cell", "_delete_rows", "_add_row", "_edit_row", "_edit_discharge_icf", "_edit_clinical_row") if hasattr(self, name)]
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
        self._source_access = _add_document_source_access(self.source_tree, self.warning_tree, self._open_path)
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
        except ValueError as exc:
            messagebox.showerror("Проверьте поля", str(exc), parent=self)
            return False
        self.draft.identity.full_name = self._header_vars["full_name"].get().strip()
        self.draft.identity.birth_date = birth_date
        self.draft.identity.medical_record_number = self._header_vars["record_number"].get().strip()
        self.draft.admission_datetime = admission
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
            initialfile=f"Оборотный лист {_safe_patient_name(self.draft.identity.full_name)}.docx",
        )
        if not output:
            return None
        if defer:
            from functools import partial
            return partial(write_reverse_sheet_docx, deepcopy(self.draft), Path(output))
        return write_reverse_sheet_docx(self.draft, Path(output))


from mdrk_builder.ui.discharge_tables import DischargeTableEditing


class DischargeSummaryPanel(DischargeTableEditing, ttk.Frame):
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
        from mdrk_builder.ui.edit_history import install_history
        methods = [name for name in ("_commit_row_cell", "_delete_rows", "_add_row", "_edit_row", "_edit_discharge_icf", "_edit_clinical_row") if hasattr(self, name)]
        self._table_history = install_history(self, methods, lambda: self.draft, self._restore_table_state)
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
                widget.bind("<<Modified>>", lambda _event, name=field.name: self._on_text_modified(name))
                self._widgets[field.name] = widget
            for row in range(row_offset, row_offset + (len(fields) + 1) // 2):
                tab.rowconfigure(row, weight=1)
            tab.columnconfigure(0, weight=1)
            tab.columnconfigure(1, weight=1)

        self._build_icf_tab()
        self._build_clinical_data_tab()

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
        self._source_access = _add_document_source_access(self.source_tree, self.warning_tree, self._open_path)
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
        for field in ('icf_domains', 'completed_procedures', 'team_findings', 'admission_scale_rows', 'discharge_scale_rows'):
            setattr(self.draft, field, deepcopy(getattr(draft, field)))
        self._refresh_icf()
        self._refresh_clinical_data()
        self._refresh_live_issues()

    def load(self, draft: DischargeSummaryDraft) -> None:
        self.draft = draft
        self._baseline = deepcopy(draft)
        if hasattr(self, "_table_history"):
            self._table_history.clear()
        self._dirty_fields.clear()
        self._dirty_identity.clear()
        self._populate()

    def merge_scan(self, draft: DischargeSummaryDraft) -> bool:
        if self.draft is None:
            self.load(draft)
            return True
        if not self.apply():
            return False
        previous = self.draft
        source_baseline = deepcopy(draft)
        from mdrk_builder.application.editing import change_summary
        self._last_change_summary = change_summary(getattr(self, "_baseline", None), draft)
        for collection in ("icf_domains", "completed_procedures", "admission_scale_rows", "discharge_scale_rows", "team_findings"):
            baseline = getattr(self, "_baseline", previous)
            rows, notes = merge_rows(getattr(baseline, collection), getattr(previous, collection), getattr(draft, collection))
            setattr(draft, collection, tuple(rows))
            draft.issues.extend(merge_issues(notes))
        for key in previous.manual_fields:
            if key not in previous.conflict_choices and key in getattr(self, '_baseline', previous).conflict_choices:
                old_choices = getattr(self, '_baseline', previous).conflict_choices.get(key)
                if draft.conflict_choices.get(key) == old_choices:
                    draft.conflict_choices.pop(key, None)
                    draft.issues = [issue for issue in draft.issues if issue.field != key]
                    draft.manual_fields.add(key)
        for name in self._dirty_fields:
            setattr(draft, name, getattr(previous, name))
            draft.manual_fields.add(name)
            draft.field_sources.pop(name, None)
            if name in previous.field_sources:
                draft.field_sources[name] = previous.field_sources[name]
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
        source_keys = {"full_name": "identity.full_name", "record_number": "identity.medical_record_number",
                       "birth_date": "identity.birth_date", "sex": "identity.sex",
                       "admission": "admission_datetime", "discharge": "discharge_datetime"}
        for name in self._dirty_identity:
            key = source_keys[name]
            draft.field_sources.pop(key, None)
            if key in previous.field_sources:
                draft.field_sources[key] = previous.field_sources[key]
        if draft.manual_fields & {"admission_datetime", "discharge_datetime"}:
            draft.header_text = update_header_period(
                draft.header_text, draft.admission_datetime, draft.discharge_datetime,
            )
        self._baseline = source_baseline
        if hasattr(self, "_table_history"):
            self._table_history.clear()
        self.draft = draft
        self._populate()
        return True

    def _build_clinical_data_tab(self) -> None:
        tab = ttk.Frame(self.notebook, padding=7)
        self.notebook.add(tab, text="Шкалы и программа")
        bar = ttk.Frame(tab)
        bar.pack(fill="x")
        ttk.Label(tab, text="Заключения, шкалы и выполненные процедуры. Источники: ПКМ или Shift+F10.").pack(fill="x", before=bar)
        self.clinical_tree = ttk.Treeview(tab, columns=("value",), show="tree headings")
        self.clinical_tree.heading("#0", text="Раздел / показатель")
        self.clinical_tree.heading("value", text="Значение")
        self.clinical_tree.column("#0", width=350)
        self.clinical_tree.column("value", width=650)
        self.clinical_tree.pack(fill="both", expand=True)
        self.clinical_detail = scrolledtext.ScrolledText(tab, height=6, wrap="word", state="disabled")
        self.clinical_detail.pack(fill="x", pady=(6, 0))
        self.clinical_tree.bind("<<TreeviewSelect>>", self._show_clinical_detail)
        self.clinical_tree.bind("<Double-1>", lambda event: self._edit_clinical_row())
        self.clinical_tree.bind("<F2>", lambda event: self._edit_clinical_row())
        for action, label in (("add","Добавить"),("add_scale","Добавить шкалу специалисту"),("edit","Изменить"),("delete","Удалить"),("source","Вернуть из источника")):
            ttk.Button(bar,text=label,command=lambda a=action:self._edit_clinical_row(a)).pack(side="left")
        self._clinical_links = {}
        self._clinical_sources = TableSourceAccess(
            self.clinical_tree, bar, links=lambda item: self._clinical_links.get(item, ()), open_path=self._open_path)

    def _show_clinical_detail(self, _event=None) -> None:
        selected = self.clinical_tree.selection()
        text = self.clinical_tree.set(selected[0], "value") if selected else ""
        self.clinical_detail.configure(state="normal")
        self.clinical_detail.delete("1.0", "end")
        self.clinical_detail.insert("1.0", text)
        self.clinical_detail.configure(state="disabled")

    def _refresh_clinical_data(self) -> None:
        self.clinical_tree.delete(*self.clinical_tree.get_children())
        self._show_clinical_detail()
        self._clinical_links = {}
        if self.draft is None:
            return
        groups = (("team", "Заключения специалистов", self.draft.team_findings),
                  ("admission", "Шкалы при поступлении", self.draft.admission_scale_rows),
                  ("discharge", "Шкалы при выписке", self.draft.discharge_scale_rows),
                  ("program", "Выполненная программа", self.draft.completed_procedures))
        for key, label, rows in groups:
            self.clinical_tree.insert("", "end", iid=key, text=label, open=True)
            for index, row in enumerate(rows):
                item = f"{key}:{index}"
                if key == "team":
                    title = " ".join(part for part in (row.role.display_name, row.specialist_name) if part)
                    if row.occurred_at:
                        title += " от " + row.occurred_at.strftime("%d.%m.%Y")
                    value = row.conclusion
                elif key == "program":
                    title = f"{row.code} {row.name}".strip()
                    value = f"{row.specialist}; количество: {row.actual_count if row.actual_count is not None else 'не указано'}; длительность: {row.duration_minutes if row.duration_minutes is not None else 'не указана'}; кратность: {row.frequency}"
                else:
                    title, value = f"{row.role.display_name}: {row.name}", row.value
                self.clinical_tree.insert(key, "end", iid=item, text=title, values=(value,))
                links = list(row_source_links(row))
                if key == "program":
                    links.append(("Расчёт: количество — ячейки с +; кратность — по датам выполнения", None))
                self._clinical_links[item] = links
                if key == "team":
                    for scale_index, scale in enumerate(row.scales):
                        scale_id = f"{item}:scale:{scale_index}"
                        self.clinical_tree.insert(item, "end", iid=scale_id, text=scale.name,
                            values=(f"{scale.initial_value or '—'} → {scale.value or '—'}",))
                        self._clinical_links[scale_id] = [
                            ("Первичное измерение", scale.initial_source),
                            ("Повторное измерение", scale.source)]

    def _build_icf_tab(self) -> None:
        tab = ttk.Frame(self.notebook, padding=7)
        self.notebook.insert(1, tab, text="МКФ")
        bar = ttk.Frame(tab)
        bar.pack(fill="x", pady=(0, 7))
        self.icf_status = tk.StringVar(value="Выберите папку эпизода и выполните сканирование.")
        ttk.Label(bar, textvariable=self.icf_status, wraplength=680).pack(side="left")
        self.icf_source_button = ttk.Button(
            bar, text="Источник МДРК-2",
            command=lambda: self._open_path(self.draft.final_mdrk_source if self.draft else None),
        )
        self.icf_source_button.pack(side="right")
        self.icf_source_button.state(["disabled"])
        table = ttk.Frame(tab)
        table.pack(fill="both", expand=True)
        columns = ("code", "description", "initial", "final", "responsible", "dynamic")
        self.icf_tree = ttk.Treeview(table, columns=columns, show="tree headings")
        apply_icf_grid_style(self.icf_tree)
        self.icf_tree.heading("#0", text="Раздел")
        self.icf_tree.column("#0", width=185, minwidth=140)
        for name, label, width in (
            ("code", "Код", 75),
            ("description", "МКФ категория", 300),
            ("initial", "Исх.", 60),
            ("final", "Повт.", 60),
            ("responsible", "Ответственный / уточнение", 250),
            ("dynamic", "+/−", 50),
        ):
            self.icf_tree.heading(name, text=label)
            self.icf_tree.column(name, width=width, minwidth=width,
                                 anchor="w" if name in {"description", "responsible"} else "center")
        vertical = ttk.Scrollbar(table, orient="vertical", command=self.icf_tree.yview)
        horizontal = ttk.Scrollbar(table, orient="horizontal", command=self.icf_tree.xview)
        self.icf_tree.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        self.icf_tree.grid(row=0, column=0, sticky="nsew")
        vertical.grid(row=0, column=1, sticky="ns")
        horizontal.grid(row=1, column=0, sticky="ew")
        table.rowconfigure(0, weight=1)
        table.columnconfigure(0, weight=1)
        self.icf_tree.tag_configure("section", background="#e2e7ed")
        row_bar = ttk.Frame(tab)
        row_bar.pack(fill="x", pady=(0, 5), before=table)
        ttk.Label(row_bar, text="Источники строки: ПКМ или Shift+F10").pack(side="left")
        self.icf_tree.bind("<Double-1>", lambda event: self._edit_discharge_icf())
        self.icf_tree.bind("<F2>", lambda event: self._edit_discharge_icf())
        for action,label in (("add","Добавить"),("edit","Изменить"),("delete","Удалить"),("source","Вернуть из источника")):
            ttk.Button(row_bar,text=label,command=lambda a=action:self._edit_discharge_icf(a)).pack(side="left")
        self._icf_sources = TableSourceAccess(
            self.icf_tree, row_bar, links=self._icf_source_links, open_path=self._open_path,
            show_button=False,
        )

    def _icf_source_links(self, item: str) -> SourceLinks:
        if self.draft is None or not item.startswith("domain:"):
            return ()
        index = int(item.split(":", 1)[1])
        if index >= len(self.draft.icf_domains):
            return ()
        return icf_source_links(self.draft.icf_domains[index])

    def _refresh_icf(self) -> None:
        self.icf_tree.delete(*self.icf_tree.get_children())
        if self.draft is None:
            return
        for column, label, value in (
            ("initial", "Первичка", self.draft.initial_assessment_datetime),
            ("final", "Выписка", self.draft.discharge_datetime),
        ):
            self.icf_tree.heading(column, text=label + (" " + value.strftime("%d.%m.%Y") if value else ""))
            self.icf_tree.column(column, width=135)
        domains = self.draft.icf_domains
        self.icf_source_button.state(["!disabled" if self.draft.final_mdrk_source else "disabled"])
        self.icf_status.set(
            "Профиль для выписного эпикриза — просмотр. Пустая оценка означает отсутствие данных."
            if domains else
            "МКФ не извлечена из МДРК-2. Проверьте источник и предупреждения."
            if self.draft.final_mdrk_source else
            "Итоговый МДРК-2 не найден. Проверьте документы эпизода и предупреждения."
        )
        for index, domain in enumerate(domains):
            group = domain.section.value
            if not self.icf_tree.exists(group):
                self.icf_tree.insert("", "end", iid=group, text=domain.section.display_name,
                                     open=True, tags=("section",))
            personal = domain.section is IcfSection.PERSONAL_FACTORS
            responsible = domain.note.strip() or (
                domain.specialist.display_name if domain.specialist is not SpecialistRole.OTHER else ""
            )
            self.icf_tree.insert(group, "end", iid=f"domain:{index}", values=(
                domain.code, domain.description,
                domain.initial.display() if domain.initial is not None and not personal else "",
                domain.final.display() if domain.final is not None and not personal else "",
                responsible if not personal else "",
                (domain.dynamic_marker or "") if not personal else "",
            ))

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
            widget.edit_modified(False)
            widget.edit_reset()
        self._populating = False
        if not self.draft.field_sources:
            self.identity_source_button.configure(text="Источник не указан", state="disabled")
        else:
            self.identity_source_button.configure(text="Источник", state="normal")
        self._refresh_sources()
        self._refresh_warnings()
        self._refresh_icf()
        self._refresh_clinical_data()

    def _on_text_modified(self, name: str) -> None:
        widget = self._widgets[name]
        if not widget.edit_modified():
            return
        widget.edit_modified(False)
        if self.draft is not None and widget.get("1.0", "end-1c") != getattr(self.draft, name):
            self._mark_dirty(name)
        if not self._populating:
            self.after_idle(self._refresh_live_issues)

    def _mark_dirty(self, name: str) -> None:
        if self._populating:
            return
        self._dirty_fields.add(name)
        if self.draft is not None:
            self.draft.manual_fields.add(name)
        self._update_source_button(name)
        self.after_idle(self._refresh_live_issues)

    def _mark_identity_dirty(self, name: str) -> None:
        if not self._populating:
            self._dirty_identity.add(name)
            self.after_idle(self._refresh_identity_header)
            self.after_idle(self._refresh_live_issues)

    def _refresh_live_issues(self):
        if self.draft is None or self._populating:
            return
        from mdrk_builder.application.discharge_validation import current_discharge_issues
        current = deepcopy(self.draft)
        for name, widget in self._widgets.items():
            setattr(current, name, widget.get('1.0', 'end-1c'))
        try:
            current.discharge_datetime = parse_optional_datetime(self._identity_vars['discharge'].get())
        except ValueError:
            return
        self.draft.issues = current_discharge_issues(current)
        self._refresh_warnings()

    def _refresh_identity_header(self):
        if self.draft is None or self._populating:
            return
        from mdrk_builder.application.discharge_identity import synchronize_header
        try:
            current = deepcopy(self.draft)
            current.identity.full_name = self._identity_vars['full_name'].get().strip()
            current.identity.medical_record_number = self._identity_vars['record_number'].get().strip()
            current.identity.sex = self._identity_vars['sex'].get().strip()
            current.identity.birth_date = parse_optional_date(self._identity_vars['birth_date'].get())
            current.admission_datetime = parse_optional_datetime(self._identity_vars['admission'].get())
            current.discharge_datetime = parse_optional_datetime(self._identity_vars['discharge'].get())
        except ValueError:
            return
        widget = self._widgets['header_text']
        current.header_text = widget.get('1.0', 'end-1c')
        previous_header = current.header_text
        synchronize_header(current)
        if previous_header != self.draft.header_text and previous_header != current.header_text:
            self.draft.issues.append(ReviewIssue('manual_header_identity_change',
                'Реквизиты в вручную изменённой шапке обновлены из паспортных полей. Предыдущий текст для сверки: ' + previous_header,
                ReviewSeverity.WARNING, 'header_text'))
        if current.header_text != widget.get('1.0', 'end-1c'):
            widget.delete('1.0', 'end')
            widget.insert('1.0', current.header_text)
            self.draft.header_text = current.header_text
            self._mark_dirty('header_text')

    def _field_links(self, name: str) -> SourceLinks:
        if self.draft is None:
            return ()
        links = field_source_links(self.draft.field_sources, name, manual=name in self.draft.manual_fields)
        if links:
            return links
        if name == "radiation_exposure" and self.draft.radiation_exposure:
            return [("Шаблон: 0 мЗв при отсутствии извлечённых сведений; проверьте значение", None)]
        return []

    def _update_source_button(self, name: str) -> None:
        links = self._field_links(name)
        self._source_buttons[name].configure(
            text="Источник" if links else "Источник не указан", state="normal" if links else "disabled")

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
        labels = {field.name: field.label for _, fields in DISCHARGE_FIELD_GROUPS for field in fields}
        labels.update({"rehabilitation_diagnosis": "МКФ", "discharge_scales": "Шкалы при выписке",
                       "completed_program": "Программа реабилитации"})
        for index, (name, source) in enumerate(sorted(rows, key=lambda item: (item[0], str(item[1])))):
            self.source_tree.insert("", "end", iid=str(index), values=(labels.get(name, name), str(source)))

    def _refresh_warnings(self) -> None:
        self.warning_tree.delete(*self.warning_tree.get_children())
        if self.draft is None:
            return
        for index, issue in enumerate(self.draft.issues):
            self.warning_tree.insert("", "end", iid=str(index), values=(issue.message, issue.field, str(issue.source or "")))

    def _open_field_source(self, name: str) -> None:
        open_source_links(self._source_buttons[name], self._field_links(name), self._open_path)

    def _open_identity_source(self) -> None:
        if self.draft is None:
            return
        names = {"full_name": "identity.full_name", "record_number": "identity.medical_record_number",
                 "birth_date": "identity.birth_date", "sex": "identity.sex",
                 "admission": "admission_datetime", "discharge": "discharge_datetime"}
        links = []
        for name, key in names.items():
            entries = field_source_links(self.draft.field_sources, key, manual=name in self._dirty_identity)
            labels = {"full_name": "ФИО", "record_number": "Номер ИБ", "birth_date": "Дата рождения",
                      "sex": "Пол", "admission": "Поступление", "discharge": "Выписка"}
            links.extend((f"{labels[name]}: {label}", path) for label, path in entries or [("Источник не указан", None)])
        open_source_links(self.identity_source_button, links, self._open_path)

    def _open_selected_source(self, _event: tk.Event | None = None) -> None:
        selected = self.source_tree.selection()
        if selected:
            values = self.source_tree.item(selected[0], "values")
            self._open_path(Path(str(values[1])) if len(values) > 1 else None)

    def apply(self) -> bool:
        if self.draft is None:
            return False
        try:
            birth_date = parse_optional_date(self._identity_vars["birth_date"].get())
            admission = parse_optional_datetime(self._identity_vars["admission"].get())
            discharge = parse_optional_datetime(self._identity_vars["discharge"].get())
        except ValueError as exc:
            messagebox.showerror("Проверьте поля", str(exc), parent=self)
            return False
        if admission and discharge and discharge < admission:
            messagebox.showerror("Проверьте поля", "Дата выписки не может быть раньше поступления.", parent=self)
            return False
        self.draft.identity.full_name = self._identity_vars["full_name"].get().strip()
        self.draft.identity.medical_record_number = self._identity_vars["record_number"].get().strip()
        self.draft.identity.birth_date = birth_date
        self.draft.identity.sex = self._identity_vars["sex"].get().strip()
        self.draft.admission_datetime = admission
        self.draft.discharge_datetime = discharge
        text_values = {name: widget.get("1.0", "end-1c") for name, widget in self._widgets.items()}
        for name, value in text_values.items():
            if value != getattr(self.draft, name):
                self._mark_dirty(name)
        apply_discharge_form(self.draft, text_values)
        from mdrk_builder.application.discharge_identity import synchronize_header
        from mdrk_builder.application.discharge_validation import current_discharge_issues
        synchronize_header(self.draft)
        self.draft.issues = current_discharge_issues(self.draft)
        self._refresh_warnings()
        return True

    def save(self, *, defer=False):
        if self.draft is None or not self.apply():
            return None
        if self.draft.requires_period_rescan():
            messagebox.showerror("Нужен пересчёт", "Даты госпитализации изменены. Повторно считайте документы выписки перед сохранением.", parent=self)
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
            initialfile=f"Выписной эпикриз {_safe_patient_name(self.draft.identity.full_name)}.docx",
        )
        if not output:
            return None
        if defer:
            from functools import partial
            return partial(write_discharge_summary_docx, deepcopy(self.draft), Path(output), ignore_issues=bool(issues))
        return write_discharge_summary_docx(self.draft, Path(output), ignore_issues=bool(issues))
