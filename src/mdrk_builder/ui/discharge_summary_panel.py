from __future__ import annotations

import tkinter as tk
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from tkinter import filedialog, messagebox, scrolledtext, ttk

from mdrk_builder.application.admission_only_scales import (
    omit_admission_scales_from_discharge, without_admission_scale_items,
)
from mdrk_builder.application.discharge_extractors import update_header_period
from mdrk_builder.application.editing import mark_manual_changes, merge_issues, merge_rows, row_key
from mdrk_builder.application.workspace import DischargeWorkspaceState
from mdrk_builder.domain import (
    DischargeScaleRow, DischargeSummaryDraft, DischargeTeamFinding,
    IcfDomain, IcfSection, Procedure, ReviewIssue, ReviewSeverity, SpecialistRole,
)
from mdrk_builder.infrastructure.discharge_summary_writer import write_discharge_summary_docx
from mdrk_builder.ui.discharge_summary_dialog import DISCHARGE_FIELD_GROUPS, apply_discharge_form
from mdrk_builder.ui.document_controls import OpenPath, add_document_source_access, safe_patient_name
from mdrk_builder.ui.episode_adapter import (
    format_date, format_datetime, parse_optional_date, parse_optional_datetime, parse_qualifier,
    role_from_name, role_names,
)
from mdrk_builder.ui.inline_tree import InlineTreeEditor
from mdrk_builder.ui.formatted_text import FormattedText
from mdrk_builder.ui.discharge_table_fields import edit_field, field_rows
from mdrk_builder.ui.generation_review_dialog import confirm_generation_with_issues
from mdrk_builder.ui.icf_table import apply_icf_grid_style
from mdrk_builder.ui.source_access import (
    TableSourceAccess, SourceLinks, icf_source_links, row_source_links,
    field_source_links, open_source_links,
)


class DischargeSummaryPanel(ttk.Frame):
    def __init__(self, parent: tk.Misc, *, open_path: OpenPath, history=None) -> None:
        super().__init__(parent)
        self.draft: DischargeSummaryDraft | None = None
        self._baseline = None
        self._open_path = open_path
        self._widgets: dict[str, tk.Text] = {}
        self._source_buttons: dict[str, ttk.Button] = {}
        self._dirty_fields: set[str] = set()
        self._dirty_identity: set[str] = set()
        self._populating = False
        self.on_dates_changed = lambda: None
        self.combine_statuses = tk.BooleanVar(value=False)
        self._identity_vars = {
            name: tk.StringVar()
            for name in ("full_name", "record_number", "birth_date", "sex", "admission", "discharge")
        }
        for name, variable in self._identity_vars.items():
            variable.trace_add("write", lambda *_args, field=name: self._mark_identity_dirty(field))
        from mdrk_builder.ui.edit_history import install_history
        methods = ("_edit_discharge_icf", "_edit_clinical_row", "_commit_icf_cell", "_commit_clinical_cell")
        self._table_history = install_history(self, methods, lambda: self.draft, self._restore_table_state, history=history)
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
                widget = FormattedText(holder, height=max(7, field.height), wrap="word", undo=True)
                widget.pack(fill="both", expand=True)
                widget.bind("<<Modified>>", lambda _event, name=field.name: self._on_text_modified(name))
                self._widgets[field.name] = widget
                from mdrk_builder.ui.quick_phrases import add_quick_phrases
                add_quick_phrases(bar, widget, field.name)
                if field.name == "neurological_status":
                    ttk.Checkbutton(holder, text="Объединить с локальным статусом в документе",
                                    variable=self.combine_statuses, command=self._change_status_mode).pack(anchor="w")
                if field.name == "signatures":
                    from mdrk_builder.ui.signature_fields import SignatureFields
                    self.signature_fields = SignatureFields(holder, widget)
            for row in range(row_offset, row_offset + (len(fields) + 1) // 2):
                tab.rowconfigure(row, weight=1)
            tab.columnconfigure(0, weight=1)
            tab.columnconfigure(1, weight=1)

        self._build_icf_tab()
        self._build_clinical_data_tab()
        from mdrk_builder.ui.discharge_specialists import DischargeSpecialists
        self.specialists = DischargeSpecialists(self)
        self.notebook.insert(2, self.specialists, text="Специалисты")

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
        for field in ('icf_domains', 'completed_procedures', 'team_findings', 'admission_scale_rows', 'discharge_scale_rows'):
            setattr(self.draft, field, deepcopy(getattr(draft, field)))
        self.refresh_tables()

    def refresh_tables(self):
        self._refresh_icf()
        self._refresh_clinical_data()
        self._refresh_live_issues()

    def capture_state(self):
        if self.draft is None:
            return None
        return deepcopy(DischargeWorkspaceState(
            self.draft, self._baseline, self._dirty_fields, self._dirty_identity,
            {key: variable.get() for key, variable in self._identity_vars.items()},
            {key: widget.get('1.0', 'end-1c') for key, widget in self._widgets.items()},
        ))

    @property
    def source_baseline(self):
        return self._baseline

    def validate_state(self, state):
        if state is not None and (state.entries.keys() - self._identity_vars.keys() or state.text.keys() - self._widgets.keys()):
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
                self._identity_vars[key].set(value)
            for key, value in state.text.items():
                widget = self._widgets[key]
                widget.delete('1.0', 'end')
                widget.insert('1.0', without_admission_scale_items(value))
                widget.edit_reset()
                widget.edit_modified(False)
        finally:
            self._populating = False
        self._dirty_fields = set(state.dirty_fields)
        self._dirty_identity = set(state.dirty_identity)

    def clear(self):
        self.draft = self._baseline = None
        if hasattr(self, "specialists"):
            self.specialists.refresh()
        self._table_history.clear()
        self._dirty_fields.clear()
        self._dirty_identity.clear()
        self._last_change_summary = ''
        self._populating = True
        try:
            for variable in self._identity_vars.values():
                variable.set('')
            for widget in self._widgets.values():
                widget.delete('1.0', 'end')
                widget.edit_reset()
                widget.edit_modified(False)
        finally:
            self._populating = False
        for tree in (self.icf_tree, self.clinical_tree, self.source_tree, self.warning_tree):
            tree.delete(*tree.get_children())
        for button in (*self._source_buttons.values(), self.identity_source_button):
            button.configure(text='Источник не указан', state='disabled')

    def load(self, draft: DischargeSummaryDraft) -> None:
        omit_admission_scales_from_discharge(draft)
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
        if "combine_admission_statuses" in previous.manual_fields:
            draft.combine_admission_statuses = previous.combine_admission_statuses
            draft.manual_fields.add("combine_admission_statuses")
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
        ttk.Label(tab, text="Врачебные шкалы и выполненные процедуры. Источники: ПКМ или Shift+F10.").pack(fill="x", before=bar)
        self.clinical_tree = ttk.Treeview(tab, columns=("value",), show="tree headings")
        self.clinical_tree.heading("#0", text="Раздел / показатель")
        self.clinical_tree.heading("value", text="Значение")
        self.clinical_tree.column("#0", width=350)
        self.clinical_tree.column("value", width=650)
        self.clinical_tree.pack(fill="both", expand=True)
        self.clinical_detail = scrolledtext.ScrolledText(tab, height=6, wrap="word", state="disabled")
        self.clinical_detail.pack(fill="x", pady=(6, 0))
        self.clinical_tree.bind("<<TreeviewSelect>>", self._show_clinical_detail)
        self._clinical_editor = InlineTreeEditor(
            self.clinical_tree, editable_columns={"value"}, commit=self._commit_clinical_cell,
            is_data_row=lambda item: ":field:" in item,
            activate=self._activate_clinical_row,
            values=lambda item, _column: role_names() if item.endswith(":field:role") else None,
            multiline=lambda item, _column: item.endswith(":field:conclusion"),
        )
        for action, label in (("add","Добавить"),("edit","Изменить"),("delete","Удалить"),("source","Вернуть из источника")):
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
        if self.draft is not None:
            omit_admission_scales_from_discharge(self.draft)
        if hasattr(self, "specialists"):
            self.specialists.refresh()
        self.clinical_tree.delete(*self.clinical_tree.get_children())
        self._show_clinical_detail()
        self._clinical_links = {}
        if self.draft is None:
            return
        groups = (("admission", "Шкалы при поступлении", self.draft.admission_scale_rows),
                  ("discharge", "Шкалы при выписке", self.draft.discharge_scale_rows),
                  ("program", "Выполненная программа", self.draft.completed_procedures))
        for key, label, rows in groups:
            self.clinical_tree.insert("", "end", iid=key, text=label, open=True)
            for index, row in enumerate(rows):
                item = f"{key}:{index}"
                if key == "program":
                    title = f"{row.code} {row.name}".strip()
                    value = f"{row.specialist}; количество: {row.actual_count if row.actual_count is not None else 'не указано'}; длительность: {row.duration_minutes if row.duration_minutes is not None else 'не указана'}; кратность: {row.frequency}"
                else:
                    title, value = f"{row.role.display_name}: {row.name}", row.value
                self.clinical_tree.insert(key, "end", iid=item, text=title, values=(value,))
                links = list(row_source_links(row))
                if key == "program":
                    links.append(("Расчёт: количество — ячейки с +; кратность — по датам выполнения", None))
                self._clinical_links[item] = links
                for name, label, text in field_rows(row):
                    field_id = f"{item}:field:{name}"
                    self.clinical_tree.insert(item, "end", iid=field_id, text=label, values=(text,))
                    self._clinical_links[field_id] = links

    def _build_icf_tab(self) -> None:
        tab = ttk.Frame(self.notebook, padding=7)
        self.notebook.insert(1, tab, text="МКФ")
        bar = ttk.Frame(tab)
        bar.pack(fill="x", pady=(0, 7))
        self.icf_status = tk.StringVar(value="Выберите папку эпизода и выполните сканирование.")
        ttk.Label(bar, textvariable=self.icf_status, wraplength=680).pack(side="left")
        self.icf_source_button = ttk.Button(
            bar, text="Дополнительный источник МДРК",
            command=lambda: self._open_path(self.draft.final_mdrk_source if self.draft else None),
        )
        self.icf_source_button.state(["disabled"])
        table = ttk.Frame(tab)
        table.pack(fill="both", expand=True)
        columns = ("code", "description", "initial", "final", "responsible", "note", "dynamic")
        self.icf_tree = ttk.Treeview(table, columns=columns, show="tree headings")
        apply_icf_grid_style(self.icf_tree)
        self.icf_tree.heading("#0", text="Раздел")
        self.icf_tree.column("#0", width=185, minwidth=140)
        for name, label, width in (
            ("code", "Код", 75),
            ("description", "МКФ категория", 300),
            ("initial", "Исх.", 60),
            ("final", "Повт.", 60),
            ("responsible", "Ответственный специалист", 220),
            ("note", "Уточнение", 220),
            ("dynamic", "+/−", 50),
        ):
            self.icf_tree.heading(name, text=label)
            self.icf_tree.column(name, width=width, minwidth=width,
                                 anchor="w" if name in {"description", "responsible", "note"} else "center")
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
        self._icf_editor = InlineTreeEditor(
            self.icf_tree, editable_columns={"code", "description", "initial", "final", "responsible", "note"},
            commit=self._commit_icf_cell, is_data_row=lambda item: item.startswith("domain:"),
            values=lambda _item, column: ("", *role_names()) if column == "responsible" else None,
        )
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
        if self.draft.final_mdrk_source:
            self.icf_source_button.pack(side="right")
        else:
            self.icf_source_button.pack_forget()
        self.icf_status.set(
            "МКФ эпизода. Двойной щелчок — правка; источники доступны для каждой строки."
            if domains else
            "В документах эпизода не найдены оценки МКФ. Добавьте строки или проверьте источники."
        )
        for index, domain in enumerate(domains):
            group = domain.section.value
            if not self.icf_tree.exists(group):
                self.icf_tree.insert("", "end", iid=group, text=domain.section.display_name,
                                     open=True, tags=("section",))
            personal = domain.section is IcfSection.PERSONAL_FACTORS
            responsible = (
                domain.specialist.display_name if domain.specialist is not SpecialistRole.OTHER else ""
            )
            self.icf_tree.insert(group, "end", iid=f"domain:{index}", values=(
                domain.code, domain.description,
                domain.initial.display() if domain.initial is not None and not personal else "",
                domain.final.display() if domain.final is not None and not personal else "",
                responsible if not personal else "",
                domain.note,
                (domain.dynamic_marker or "") if not personal else "",
            ))

    def _populate(self) -> None:
        self.combine_statuses.set(self.draft.combine_admission_statuses if self.draft else False)
        if self.draft is None:
            return
        omit_admission_scales_from_discharge(self.draft)
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

    def _change_status_mode(self):
        if self.draft is not None:
            self.draft.combine_admission_statuses = self.combine_statuses.get()
            self.draft.manual_fields.add("combine_admission_statuses")

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
            if name in {"admission", "discharge"}:
                self.on_dates_changed()
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
        if name == "signatures" and "signatures.treating_physician" in self.draft.field_sources:
            links.append(("Шаблон: заведующий отделением; имя можно изменить отдельно", None))
        if links:
            return links
        if name == "radiation_exposure" and self.draft.radiation_exposure:
            return [("Шаблон: 0 мЗв при отсутствии извлечённых сведений; проверьте значение", None)]
        defaults = {"rehabilitation_potential": "средний", "goal_result": "достигнут в полном объёме"}
        if name in defaults and getattr(self.draft, name) == defaults[name]:
            return [("Шаблон: согласованное значение по умолчанию; врач может изменить", None)]
        if name == "signatures" and self.draft.signatures:
            return [("Шаблон: заведующий отделением; лечащий врач — из текущей первички при наличии", None)]
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
            ("Внешний МДРК (дополнительный)", self.draft.final_mdrk_source),
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
        if hasattr(self, "specialists"):
            self.specialists.commit()
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
            initialfile=f"Выписной эпикриз {safe_patient_name(self.draft.identity.full_name)}.docx",
        )
        if not output:
            return None
        if defer:
            from functools import partial
            return partial(write_discharge_summary_docx, deepcopy(self.draft), Path(output), ignore_issues=bool(issues))
        return write_discharge_summary_docx(self.draft, Path(output), ignore_issues=bool(issues))

    def _commit_icf_cell(self, item, column, value):
        if self.draft is None or not item.startswith("domain:"):
            return
        index = int(item.split(":")[1])
        rows = list(self.draft.icf_domains)
        old = rows[index]
        row = deepcopy(old)
        value = value.strip()
        if column in {"initial", "final"}:
            setattr(row, column, parse_qualifier(value))
        elif column == "responsible":
            row.specialist = role_from_name(value) if value else SpecialistRole.OTHER
        else:
            setattr(row, column, value)
        mark_manual_changes(old, row)
        rows[index] = row
        self.draft.icf_domains = tuple(rows)
        self._refresh_icf()
        self.icf_tree.selection_set(item)
        self._refresh_live_issues()

    def _edit_discharge_icf(self, action="edit"):
        if self.draft is None:
            return
        selected = self.icf_tree.selection()
        index = int(selected[0].split(":")[1]) if selected and selected[0].startswith("domain:") else None
        rows = list(self.draft.icf_domains)
        if action != "add" and index is None:
            return
        if action == "edit":
            self._icf_editor.edit(selected[0], "code")
            return
        if action == "add":
            index = len(rows)
            rows.append(IcfDomain("", "", SpecialistRole.OTHER, manual_fields={"initial", "final"}))
        elif action == "delete":
            if not messagebox.askyesno("Удалить строку", "Удалить выбранный домен?", parent=self):
                return
            rows.pop(index)
        elif action == "source":
            old = rows[index]
            original = next((r for r in self._baseline.icf_domains if row_key(r) == row_key(old)), None)
            if original is None:
                return
            rows[index] = deepcopy(original)
        self.draft.icf_domains = tuple(rows)
        self._refresh_icf()
        self._refresh_live_issues()
        if action == "add":
            item = f"domain:{index}"
            self.icf_tree.selection_set(item)
            self.icf_tree.see(item)
            self.after_idle(lambda: self._icf_editor.edit(item, "code"))

    def _activate_clinical_row(self, item):
        if ":field:" in item or item.count(":") < 1:
            return False
        self.clinical_tree.item(item, open=True)
        fields = [child for child in self.clinical_tree.get_children(item) if ":field:" in child]
        if fields:
            self.clinical_tree.selection_set(fields[0])
            self.after_idle(lambda: self._clinical_editor.edit(fields[0], "value"))
        return True

    def _commit_clinical_cell(self, item, _column, value):
        row_id, name = item.rsplit(":field:", 1)
        self._edit_clinical_row("cell", row_id=row_id, field=name, value=value)
        if self.clinical_tree.exists(item):
            parent = self.clinical_tree.parent(item)
            while parent:
                self.clinical_tree.item(parent, open=True)
                parent = self.clinical_tree.parent(parent)
            self.clinical_tree.selection_set(item)
            self.clinical_tree.see(item)

    def _edit_clinical_row(self, action="edit", *, row_id=None, field=None, value=None):
        if self.draft is None:
            return
        if hasattr(self, "specialists"):
            self.specialists.commit()
        selected = self.clinical_tree.selection()
        if row_id is None:
            if not selected:
                return
            row_id = selected[0].split(":field:")[0]
        parts = row_id.split(":")
        group = parts[0]
        mapping = {"team": "team_findings", "program": "completed_procedures",
                   "admission": "admission_scale_rows", "discharge": "discharge_scale_rows"}
        if group not in mapping:
            return
        if action == "edit":
            self._activate_clinical_row(row_id)
            return
        attr = mapping[group]
        rows = list(getattr(self.draft, attr))
        index = int(parts[1]) if len(parts) > 1 else None
        child = len(parts) == 4 and parts[2] == "scale"
        if action == "add_scale":
            if group != "team" or index is None:
                return
            child, action = True, "add"
        if action != "add" and index is None:
            return
        parent = rows[index] if index is not None else None
        target = list(parent.scales) if child else rows
        target_index = (int(parts[3]) if len(parts) == 4 else None) if child else index
        old = target[target_index] if target_index is not None else None
        if action == "delete":
            if not messagebox.askyesno("Удалить строку", "Удалить выбранную строку?", parent=self):
                return
            target.pop(target_index)
            new = None
        elif action == "source":
            originals = getattr(self._baseline, attr)
            if child:
                original_parent = next((r for r in originals if row_key(r) == row_key(parent)), None)
                originals = original_parent.scales if original_parent else ()
            new = next((deepcopy(r) for r in originals if row_key(r) == row_key(old)), None)
            if new is None:
                return
            target[target_index] = new
        elif action == "cell":
            new = edit_field(old, field, value)
            target[target_index] = new
        elif action == "add":
            if group == "program":
                new = Procedure("", "", None, manual_fields={"name"})
            elif group == "team" and not child:
                new = DischargeTeamFinding(SpecialistRole.OTHER, "", manual_fields={"conclusion"})
            else:
                new = DischargeScaleRow(parent.role if child else SpecialistRole.OTHER, "", manual_fields={"value"})
            target_index = len(target)
            target.append(new)
        else:
            return
        from mdrk_builder.application.shared_edits import synchronize_scale_rows, synchronize_discharge_point, remove_scale_rows
        if child:
            rows[index] = replace(parent, scales=tuple(target))
            if old is not None and action != "add":
                remove_scale_rows(self.draft, old)
            if new is not None:
                synchronize_scale_rows(self.draft, new)
        setattr(self.draft, attr, tuple(rows))
        if group in {"admission", "discharge"}:
            synchronize_discharge_point(self.draft, attr, None if action == "add" else old, new)
        elif group == "team" and not child and action == "delete":
            for row in old.scales:
                remove_scale_rows(self.draft, row)
        self._refresh_clinical_data()
        self._refresh_live_issues()
        if action == "add" and self.clinical_tree.exists(group):
            item = f"{group}:{index}:scale:{target_index}" if child else f"{group}:{target_index}"
            self.clinical_tree.item(group, open=True)
            if child:
                self.clinical_tree.item(f"{group}:{index}", open=True)
            self._activate_clinical_row(item)
