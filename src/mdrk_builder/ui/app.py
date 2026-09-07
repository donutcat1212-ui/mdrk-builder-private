from __future__ import annotations

from mdrk_builder.application.editing import mark_manual_changes
from mdrk_builder.application.editing import merge_rows, merge_issues

import os
import re
import subprocess
import sys
import threading
import tkinter as tk
import traceback
from copy import deepcopy
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory, gettempdir
from tkinter import filedialog, messagebox, scrolledtext, ttk
from typing import TypeVar

from docx import Document

from mdrk_builder import __version__
from mdrk_builder.application.discharge_summary import scan_discharge_summary
from mdrk_builder.application.feedback import FeedbackStorageError, save_feedback
from mdrk_builder.application.identifiers import normalize_medical_record_number
from mdrk_builder.application.reverse_sheet import scan_reverse_sheet
from mdrk_builder.application.scanner import scan_patient_folder
from mdrk_builder.application.snapshot import build_snapshot
from mdrk_builder.application.validation import (
    acknowledge_issue,
    clear_issue_acknowledgements,
    current_issues,
    has_issue_acknowledgements,
)
from mdrk_builder.domain import (
    DischargeSummaryDraft,
    Episode,
    IcfDomain,
    IcfSection,
    MdrkKind,
    PatientIdentity,
    Procedure,
    ReviewIssue,
    ReviewSeverity,
    ReverseSheetDraft,
    ScaleMeasurement,
    SourceDocument,
    SpecialistFinding,
    SpecialistRole,
    move_icf_domain,
)
from mdrk_builder.infrastructure.docx_writer import canonical_template_path, write_mdrk_docx
from mdrk_builder.infrastructure.discharge_summary_writer import write_discharge_summary_docx
from mdrk_builder.ui.dialogs import (
    FeedbackDialog,
    FindingDialog,
    IcfDomainDialog,
    ProcedureDialog,
    ScaleDialog,
    install_edit_shortcuts,
)
from mdrk_builder.ui.inline_tree import InlineTreeEditor
from mdrk_builder.ui.icf_table import apply_icf_grid_style
from mdrk_builder.ui.source_access import TableSourceAccess, SourceLinks, icf_source_links, field_source_links, row_source_links, open_source_links, open_source_path
from mdrk_builder.ui.background_job import BackgroundJobRunner
from mdrk_builder.ui.discharge_summary_panel import DischargeSummaryPanel
from mdrk_builder.ui.reverse_sheet_panel import ReverseSheetPanel
from mdrk_builder.ui.episode_adapter import (
    EpisodeFormData,
    apply_episode_form_data,
    format_date,
    format_datetime,
    parse_episode_folder,
    parse_episode_form_data,
    parse_optional_datetime,
    parse_optional_meeting_datetime,
    parse_qualifier,
    role_from_name,
    role_names,
    sections_for,
)
from mdrk_builder.ui.generation_review_dialog import confirm_generation_with_issues


SEVERITY_LABELS = {
    ReviewSeverity.BLOCKING: "БЛОКИРУЕТ",
    ReviewSeverity.WARNING: "ПРЕДУПРЕЖДЕНИЕ",
    ReviewSeverity.INFO: "ИНФО",
}

MEETING_RESCAN_MESSAGE = (
    "Время заседания изменено. Нажмите «Повторить сканирование», чтобы "
    "заново собрать данные на этот момент. Ручные правки сохранятся."
)

# Kept as the single reusable safety wording for dialogs and documentation.
# It is intentionally not rendered as persistent chrome in the main window.
REVIEW_NOTICE_SHORT = (
    "Перед подписанием проверьте созданный DOCX: "
    "автоматический перенос может содержать пропуски или ошибки."
)

BackgroundResultT = TypeVar("BackgroundResultT")


def about_text() -> str:
    return (
        f"МДРК Builder {__version__}\n\n"
        "Локальный инструмент подготовки редактируемых проектов МДРК, "
        "выписного эпикриза и оборотного листа.\n\n"
        "Программа автоматически переносит и форматирует данные из выбранных "
        "документов. Результат может содержать пропуски или ошибки распознавания и должен быть "
        "проверен перед подписанием и включением в медицинскую документацию.\n\n"
        "Программа не выполняет диагностику, не назначает лечение и не заменяет "
        "профессиональное решение специалиста.\n\n"
        "Программное обеспечение предоставляется «как есть», без гарантии безошибочного "
        "формирования документа."
    )


from mdrk_builder.ui.workspace_state import WorkspacePersistence
from mdrk_builder.application.workspace import WorkspaceDraft, MdrkWorkspaceState


class MdrkBuilderApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.episode: Episode | None = None
        self._current_kind = MdrkKind.INITIAL
        self._previous_document = "mdrk1"
        self._background_jobs = BackgroundJobRunner(root, thread_factory=threading.Thread)
        from mdrk_builder.application.scan_session import ScanSession
        self._scan_session = ScanSession()
        self.root._mdrk_scan_session = self._scan_session
        self._scan_session.progress = lambda done,total,path: self._background_jobs.report_progress(f"Считывание {done}/{total}: {path.name if path else 'готово'}")
        self._background_jobs.on_progress = lambda text: self.status_var.set(text)
        self._active_job_folder: Path | None = None
        self._scanning = False
        self._setting_folder_field = False
        self._last_form_error = ""
        self._entry_variables: dict[str, tk.StringVar] = {}
        self._text_fields: dict[str, tk.Text] = {}
        self._scale_refs: list[tuple[int, int]] = []
        self._scale_pair_refs: dict[str, object] = {}
        self._issue_refs: dict[str, ReviewIssue] = {}
        self._manual_collections: set[str] = set()
        self._dirty_entry_fields: set[str] = set()
        self._dirty_section_fields: dict[MdrkKind, set[str]] = {
            MdrkKind.INITIAL: set(),
            MdrkKind.FINAL: set(),
        }
        self._pending_manual_state: dict[str, object] | None = None
        self._populating = False
        self._field_source_buttons: dict[str, ttk.Button] = {}
        self._field_source_paths: dict[str, Path] = {}
        self._icf_drag_item: str | None = None
        self._icf_drag_origin: tuple[int, int] | None = None
        self._icf_editor: InlineTreeEditor | None = None
        self._procedure_editor: InlineTreeEditor | None = None
        self._scale_editor: InlineTreeEditor | None = None
        self._loading_specialist = False
        self._displayed_specialist_finding: SpecialistFinding | None = None
        self.reverse_draft: ReverseSheetDraft | None = None
        self.discharge_draft: DischargeSummaryDraft | None = None

        self.folder_var = tk.StringVar()
        self.kind_var = tk.StringVar(value=MdrkKind.INITIAL.value)
        self.document_var = tk.StringVar(value="mdrk1")
        self.status_var = tk.StringVar(value="Выберите папку эпизода")

        self._configure_window()
        install_edit_shortcuts(self.root)
        from mdrk_builder.application.table_editor import SharedTableEditor
        from mdrk_builder.ui.edit_history import install_history
        self._table_editor = SharedTableEditor(
            episode=lambda: self.episode, discharge=lambda: self.discharge_workspace.draft,
            refresh_episode=self._refresh_shared_episode,
            refresh_discharge=lambda: self.discharge_workspace.refresh_tables(),
        )
        self._table_history = install_history(self,
            ("_commit_icf_cell", "_delete_icf", "_add_icf", "_edit_icf", "_move_icf_domain",
             "_commit_procedure_cell", "_add_procedure", "_edit_procedure", "_delete_procedure",
             "_commit_scale_cell", "_add_scale", "_edit_scale", "_delete_scale",
             "_add_finding", "_edit_finding", "_delete_finding", "_activate_icf_item",
             "_activate_procedure_item", "_activate_scale_item", "_finish_icf_pointer",
             "_commit_specialist_conclusion", "_restore_selected_source"),
            history=self._table_editor.history)
        self._build_menu()
        self._build_layout()
        self._workspace = WorkspacePersistence(
            root, capture=self._capture_workspace, restore=self._apply_workspace,
            folder=self._state_folder, busy=lambda: self._scanning, status=self.status_var.set,
        )
        self._update_field_sources()
        self.folder_var.trace_add("write", self._on_folder_field_changed)
        self._update_action_states()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.after(30000, self._workspace.autosave)
        self.root.after(1500, self._refresh_draft_indicator)

    def _capture_workspace(self):
        manual = self._capture_manual_state()
        mdrk = None
        if manual is not None:
            mdrk = MdrkWorkspaceState(
                manual['episode'], manual['baseline'], manual['entry_fields'],
                manual['section_fields'], manual['collections'],
                {key: variable.get() for key, variable in self._entry_variables.items()},
                {key: widget.get('1.0', 'end-1c') for key, widget in self._text_fields.items()},
            )
        return WorkspaceDraft(self._current_kind, self.document_var.get(), mdrk,
                              self.reverse_workspace.capture_state(), self.discharge_workspace.capture_state())

    def _state_folder(self):
        for model in (self.episode, self.discharge_workspace.draft, self.reverse_workspace.draft):
            if model is not None:
                return model.folder
        return None

    def _save_workspace(self, explicit=False):
        return self._workspace.save(explicit)

    def _restore_workspace(self, folder):
        return self._workspace.open(folder)

    def _confirm_leave(self):
        return self._workspace.confirm_leave()

    def _refresh_draft_indicator(self):
        self._workspace.refresh_indicator()

    def _clear_workspace(self):
        self._clear_manual_edits()
        self._scan_baseline = None
        self._invalidate_episode()
        self.reverse_workspace.clear()
        self.discharge_workspace.clear()
        self.reverse_draft = self.discharge_draft = None
        self._table_history.clear()
        self._workspace.saved = None

    def _apply_workspace(self, state):
        if state.mdrk and (state.mdrk.entries.keys() - self._entry_variables.keys() or state.mdrk.sections.keys() - self._text_fields.keys()):
            raise ValueError('Неизвестные поля МДРК в черновике')
        self.reverse_workspace.validate_state(state.reverse)
        self.discharge_workspace.validate_state(state.discharge)
        self._clear_workspace()
        self._current_kind = state.kind
        if state.mdrk:
            mdrk = state.mdrk
            self.episode = mdrk.episode
            self._scan_baseline = mdrk.baseline
            self._dirty_entry_fields = set(mdrk.entry_fields)
            self._dirty_section_fields = {kind: set(mdrk.section_fields.get(kind, ())) for kind in MdrkKind}
            self._manual_collections = set(mdrk.collections)
            self._populate_from_episode()
            self._populating = True
            try:
                for key, value in mdrk.entries.items():
                    self._entry_variables[key].set(value)
                for key, value in mdrk.sections.items():
                    widget = self._text_fields[key]
                    widget.delete('1.0', 'end')
                    widget.insert('1.0', value)
                    widget.edit_reset()
                    widget.edit_modified(False)
            finally:
                self._populating = False
        self.reverse_workspace.restore_state(state.reverse)
        self.discharge_workspace.restore_state(state.discharge)
        self.reverse_draft = self.reverse_workspace.draft
        self.discharge_draft = self.discharge_workspace.draft
        self.document_var.set(state.document)
        self._previous_document = state.document
        for panel in (self.mdrk_workspace, self.reverse_workspace, self.discharge_workspace):
            panel.pack_forget()
        panels = {'mdrk1': self.mdrk_workspace, 'mdrk2': self.mdrk_workspace,
                  'reverse': self.reverse_workspace, 'discharge': self.discharge_workspace}
        panels[state.document].pack(fill='both', expand=True)
        self.kind_var.set(state.kind.value)
        self._update_action_states()

    def _refresh_shared_episode(self):
        if self.episode is not None:
            self._manual_collections.update({'icf', 'procedures', 'findings'})
            self._refresh_all_trees()

    def _restore_selected_source(self):
        from mdrk_builder.application.editing import row_key
        focus = self.root.focus_get()
        if self.document_var.get() == "discharge":
            panel=self.discharge_workspace
            if focus is panel.icf_tree:
                panel._edit_discharge_icf("source");return
            if focus is panel.clinical_tree:
                panel._edit_clinical_row("source");return
            for key,widget in panel._widgets.items():
                if focus is widget and getattr(panel,"_baseline",None):
                    value=getattr(panel._baseline,key)
                    setattr(panel.draft,key,value);panel._dirty_fields.discard(key);panel.draft.manual_fields.discard(key)
                    widget.delete("1.0","end");widget.insert("1.0",value);return
        if self.document_var.get() == 'reverse':
            panel = self.reverse_workspace
            if panel.draft is not None and focus is panel.row_tree and panel.row_tree.selection():
                item = panel.row_tree.selection()[0]
                index = panel._row_refs[int(item)]
                old = panel.draft.rows[index]
                original = next((row for row in panel._baseline.rows if row_key(row) == row_key(old)), None)
                if original is not None:
                    panel._table_history.wrap(lambda: panel.draft.rows.__setitem__(index, deepcopy(original)))()
                    panel._rows_dirty = True
                    panel._populate()
            return
        baseline=getattr(self,"_scan_baseline",None)
        if self.episode is None or baseline is None:return
        for key,widget in self._text_fields.items():
            if focus is widget:
                value=getattr(sections_for(baseline,self._current_kind),key)
                widget.delete("1.0","end");widget.insert("1.0",value)
                self._dirty_section_fields[self._current_kind].discard(key)
                setattr(sections_for(self.episode,self._current_kind),key,value);return
        for tree,attr in ((self.icf_tree,"icf_domains"),(self.procedure_tree,"procedures"),(self.finding_tree,"findings")):
            if focus is tree and tree.selection() and tree.selection()[0].isdigit():
                index=int(tree.selection()[0]);rows=getattr(self.episode,attr)
                original=next((r for r in getattr(baseline,attr) if row_key(r)==row_key(rows[index])),None)
                if original is not None:rows[index]=deepcopy(original);self._refresh_all_trees()
                return

    def _active_history(self):
        document = self.document_var.get()
        if document == "discharge":
            return self.discharge_workspace._table_history
        if document == "reverse":
            return self.reverse_workspace._table_history
        return self._table_history

    def _configure_window(self) -> None:
        self.root.title(f"МДРК — сборщик документов  {__version__}")
        self.root.geometry("1180x790")
        self.root.minsize(980, 660)
        style = ttk.Style(self.root)
        if "clam" in style.theme_names():
            style.theme_use("clam")

        background = "#f5f7fa"
        surface = "#ffffff"
        border = "#d7dde5"
        text = "#1f2937"
        muted = "#5f6b7a"
        accent = "#2f6fcb"
        accent_hover = "#245fae"
        selection = "#e5eefc"
        font = ("Segoe UI", 10)

        self.root.configure(background=background)
        self.root.option_add("*Font", font)
        self.root.option_add("*Text.background", surface)
        self.root.option_add("*Text.foreground", text)
        self.root.option_add("*Text.insertBackground", text)
        self.root.option_add("*Text.selectBackground", "#b9d2f5")
        self.root.option_add("*Text.selectForeground", text)
        self.root.option_add("*Text.relief", "flat")
        self.root.option_add("*Text.borderWidth", 0)
        self.root.option_add("*Text.highlightThickness", 1)
        self.root.option_add("*Text.highlightBackground", border)
        self.root.option_add("*Text.highlightColor", accent)

        style.configure(".", font=font, background=background, foreground=text)
        style.configure("TFrame", background=background)
        style.configure("TLabel", background=background, foreground=text)
        style.configure("Muted.TLabel", foreground=muted)
        style.configure(
            "TLabelframe",
            background=surface,
            bordercolor=border,
            lightcolor=border,
            darkcolor=border,
            relief="solid",
        )
        style.configure("TLabelframe.Label", background=surface, foreground=text)
        style.configure(
            "TEntry",
            fieldbackground=surface,
            foreground=text,
            bordercolor=border,
            lightcolor=border,
            darkcolor=border,
            padding=(7, 5),
        )
        style.map("TEntry", bordercolor=[("focus", accent)])
        style.configure(
            "TButton",
            background=surface,
            foreground=text,
            bordercolor=border,
            lightcolor=border,
            darkcolor=border,
            padding=(10, 6),
            relief="solid",
        )
        style.map(
            "TButton",
            background=[("active", "#edf2f8"), ("pressed", selection)],
            bordercolor=[("focus", accent)],
        )
        style.configure(
            "Primary.TButton",
            background=accent,
            foreground="#ffffff",
            bordercolor=accent,
            lightcolor=accent,
            darkcolor=accent,
            padding=(13, 7),
        )
        style.map(
            "Primary.TButton",
            background=[("active", accent_hover), ("pressed", "#1f528f"), ("disabled", "#aeb9c8")],
            foreground=[("disabled", "#eef2f7")],
            bordercolor=[("disabled", "#aeb9c8")],
        )
        style.configure(
            "Document.Toolbutton",
            background=background,
            foreground="#374151",
            bordercolor=background,
            lightcolor=background,
            darkcolor=background,
            padding=(13, 8),
            relief="flat",
        )
        style.map(
            "Document.Toolbutton",
            background=[("selected", surface), ("active", "#edf2f8")],
            foreground=[("selected", "#254f88")],
            bordercolor=[("selected", border)],
        )
        style.configure("TNotebook", background=background, borderwidth=0, tabmargins=(0, 0, 0, 0))
        style.configure(
            "TNotebook.Tab",
            background=background,
            foreground="#4b5563",
            bordercolor=background,
            lightcolor=background,
            padding=(12, 7),
        )
        style.map(
            "TNotebook.Tab",
            background=[("selected", selection), ("active", "#edf2f8")],
            foreground=[("selected", "#254f88")],
            bordercolor=[("selected", selection)],
        )
        style.configure(
            "Treeview",
            background=surface,
            fieldbackground=surface,
            foreground=text,
            bordercolor=border,
            lightcolor=border,
            darkcolor=border,
            rowheight=29,
            relief="solid",
        )
        style.map("Treeview", background=[("selected", "#dbe9fb")], foreground=[("selected", text)])
        style.configure(
            "Treeview.Heading",
            background="#eef2f7",
            foreground="#374151",
            bordercolor=border,
            lightcolor=border,
            darkcolor=border,
            padding=(7, 6),
            relief="flat",
        )
        style.map("Treeview.Heading", background=[("active", "#e3e9f1")])
        style.configure("TPanedwindow", background=background)
        style.configure("TProgressbar", background=accent, troughcolor="#e5e9ef", bordercolor="#e5e9ef")

    def _build_menu(self) -> None:
        menu = tk.Menu(self.root)
        file_menu = tk.Menu(menu, tearoff=False)
        file_menu.add_command(label="Выбрать папку…", command=self._choose_folder, accelerator="Ctrl+O")
        file_menu.add_command(label="Повторить сканирование", command=self._rescan_current_document, accelerator="F5")
        file_menu.add_separator()
        file_menu.add_command(label="Сохранить документ", command=self._generate, accelerator="Ctrl+S")
        file_menu.add_command(label="Сохранить рабочий черновик", command=lambda: self._save_workspace(explicit=True))
        file_menu.add_command(label="Восстановить черновик", command=lambda: self._restore_workspace(Path(self.folder_var.get())))
        file_menu.add_separator()
        file_menu.add_command(label="Выход", command=self._on_close)
        menu.add_cascade(label="Файл", menu=file_menu)

        edit_menu = tk.Menu(menu, tearoff=False)
        edit_menu.add_command(label="Отменить табличную правку", command=lambda: self._active_history().undo())
        edit_menu.add_command(label="Повторить табличную правку", command=lambda: self._active_history().redo())
        edit_menu.add_separator()
        edit_menu.add_command(label="Вернуть выбранное из источника", command=self._restore_selected_source)
        menu.add_cascade(label="Правка", menu=edit_menu)

        help_menu = tk.Menu(menu, tearoff=False)
        help_menu.add_command(label="Обратная связь…", command=self._show_feedback)
        help_menu.add_separator()
        help_menu.add_command(label="О программе", command=self._show_about)
        menu.add_cascade(label="Справка", menu=help_menu)
        self.root.config(menu=menu)
        self.root.bind("<Control-o>", lambda _event: self._choose_folder())
        self.root.bind("<Control-s>", lambda _event: self._generate())
        self.root.bind("<F5>", lambda _event: self._rescan_current_document())
        for key, document in enumerate(("mdrk1", "mdrk2", "reverse", "discharge"), start=1):
            self.root.bind(
                f"<Alt-Key-{key}>",
                lambda _event, value=document: self._select_document(value),
            )

    def _build_layout(self) -> None:
        top = ttk.Frame(self.root, padding=6)
        top.pack(fill="x")
        ttk.Label(top, text="Папка эпизода:").grid(row=0, column=0, sticky="w")
        folder_entry = ttk.Entry(top, textvariable=self.folder_var)
        folder_entry.grid(row=0, column=1, sticky="ew", padx=(6, 4))
        ttk.Button(top, text="Обзор…", command=self._choose_folder).grid(row=0, column=2, padx=2)
        self.scan_button = ttk.Button(top, text="Повторить сканирование", command=self._rescan_current_document)
        self.scan_button.grid(row=0, column=3, padx=2)
        self.cancel_button = ttk.Button(top, text="Отменить", command=self._cancel_scan)
        self.cancel_button.grid(row=0,column=5,padx=2)
        top.columnconfigure(1, weight=1)

        document_bar = ttk.Frame(self.root, padding=(6, 3))
        document_bar.pack(fill="x")
        for value, label in (
            ("mdrk1", "МДРК-1"),
            ("mdrk2", "МДРК-2"),
            ("reverse", "Оборотный лист"),
            ("discharge", "Выписной эпикриз"),
        ):
            ttk.Radiobutton(
                document_bar,
                text=label,
                value=value,
                variable=self.document_var,
                command=self._on_document_changed,
                style="Document.Toolbutton",
            ).pack(side="left", padx=(0, 3), ipady=5)
        self.generate_button = ttk.Button(
            document_bar,
            text="Сохранить документ",
            command=self._generate,
            state="disabled",
            style="Primary.TButton",
        )
        self.generate_button.pack(side="right")

        self.mdrk_workspace = ttk.Frame(self.root)
        self.mdrk_workspace.pack(fill="both", expand=True)
        self.notebook = ttk.Notebook(self.mdrk_workspace)
        self.notebook.pack(fill="both", expand=True, padx=6, pady=(0, 5))
        self.notebook.enable_traversal()
        self._build_main_tab()
        self._build_icf_tab()
        self._build_specialists_tab()
        self._build_procedures_tab()
        self._build_sources_tab()
        self._build_issues_tab()
        self.issue_tree.bind("<Double-1>",self._go_to_selected_issue)
        self.issue_tree.bind("<Return>",self._go_to_selected_issue)
        self._build_table_source_access()

        self.reverse_workspace = ReverseSheetPanel(self.root, open_path=self._open_path)
        self.discharge_workspace = DischargeSummaryPanel(self.root, open_path=self._open_path, history=self._table_history)

        self.progress = ttk.Progressbar(top, mode="indeterminate", length=120)
        self.progress.grid(row=0, column=4, padx=(8, 0))
        self.progress.grid_remove()

    def _build_main_tab(self) -> None:
        tab = ttk.Frame(self.notebook, padding=7)
        self.notebook.add(tab, text="Основное")
        metadata = ttk.LabelFrame(tab, text="Шапка документа", padding=7)
        metadata.pack(fill="x")
        fields = (
            ("full_name", "ФИО пациента"),
            ("record_number", "Номер ИБ"),
            ("birth_date", "Дата рождения"),
            ("sex", "Пол"),
            ("admission", "Поступление"),
            ("meeting", "Заседание"),
            ("department", "Отделение"),
            ("stage", "Этап реабилитации"),
            ("duration", "Койко-дни"),
        )
        for index, (key, label) in enumerate(fields):
            row, group = divmod(index, 2)
            column = group * 2
            label_frame = ttk.Frame(metadata)
            label_frame.grid(row=row, column=column, sticky="ew", padx=(0, 5), pady=3)
            ttk.Label(label_frame, text=label).pack(side="left")
            if key == "duration":
                ttk.Button(label_frame, text="Авто", command=self._automatic_duration).pack(side="left")
            source_key = {
                "full_name": "identity.full_name",
                "record_number": "identity.medical_record_number",
                "birth_date": "identity.birth_date",
                "sex": "identity.sex",
                'admission': 'admission_datetime',
                'meeting': 'meeting_at',
                'department': 'department',
                'stage': 'stage',
                'duration': 'course_duration_days',
            }.get(key)
            if source_key:
                button = ttk.Button(
                    label_frame,
                    text="Источник",
                    command=lambda field_key=source_key: self._open_field_source(field_key),
                )
                button.pack(side="right")
                self._field_source_buttons[source_key] = button
            variable = tk.StringVar()
            self._entry_variables[key] = variable
            variable.trace_add(
                "write",
                lambda *_args, field_key=key: self._mark_entry_dirty(field_key),
            )
            ttk.Entry(metadata, textvariable=variable).grid(
                row=row, column=column + 1, sticky="ew", padx=(0, 12), pady=3
            )
        metadata.columnconfigure(1, weight=1)
        metadata.columnconfigure(3, weight=1)

        sections = ttk.Notebook(tab)
        sections.pack(fill="both", expand=True, pady=(7, 0))
        self._add_text_group(
            sections,
            "Клиника",
            (("clinical_diagnosis", "Клинический диагноз", 7), ("disease_history", "Анамнез заболевания", 7), ("life_history", "Анамнез жизни", 5)),
        )
        self._add_text_group(
            sections,
            "Исследования",
            (("laboratory_results", "Лабораторные исследования", 9), ("instrumental_results", "Инструментальные исследования", 9)),
        )
        self._add_text_group(
            sections,
            "План",
            (("rehabilitation_potential", "Реабилитационный потенциал", 2), ("limitations", "Ограничивающие факторы", 3), ("risks", "Факторы риска", 3), ("goal", "Цель", 4), ("tasks", "Задачи", 5)),
        )
        self._add_text_group(
            sections,
            "Режим и лечение",
            (("movement_regimen", "Двигательный режим", 2), ("diet", "Диета", 2), ("medication", "Медикаментозное лечение", 14)),
        )

    def _add_text_group(
        self, notebook: ttk.Notebook, title: str, fields: tuple[tuple[str, str, int], ...]
    ) -> None:
        frame = ttk.Frame(notebook, padding=6)
        notebook.add(frame, text=title)
        for index, (key, label, height) in enumerate(fields):
            row, column = divmod(index, 2)
            full_width = index == len(fields) - 1 and len(fields) % 2 == 1
            field_frame = ttk.Frame(frame)
            field_frame.grid(
                row=row,
                column=0 if full_width else column,
                columnspan=2 if full_width else 1,
                sticky="nsew",
                padx=(0, 6) if not full_width and column == 0 else (6, 0) if column else 0,
                pady=(3, 5),
            )
            label_row = ttk.Frame(field_frame)
            label_row.pack(fill="x", pady=(0, 2))
            ttk.Label(label_row, text=label).pack(side="left")
            source_key = f"sections.{key}"
            button = ttk.Button(
                label_row,
                text="Источник",
                command=lambda field_key=source_key: self._open_field_source(field_key),
            )
            button.pack(side="right")
            self._field_source_buttons[source_key] = button
            widget = scrolledtext.ScrolledText(field_frame, height=max(7, height), wrap="word", undo=True)
            widget.pack(fill="both", expand=True)
            widget.bind(
                "<KeyRelease>",
                lambda _event, field_key=key: self._mark_section_dirty(field_key),
            )
            for virtual_event in ("<<Paste>>", "<<Cut>>", "<<Undo>>", "<<Redo>>"):
                widget.bind(
                    virtual_event,
                    lambda _event, field_key=key: self._mark_section_dirty(field_key),
                    add="+",
                )
            self._text_fields[key] = widget
        for row in range((len(fields) + 1) // 2):
            frame.rowconfigure(row, weight=1)
        frame.columnconfigure(0, weight=1)
        frame.columnconfigure(1, weight=1)

    def _build_icf_tab(self) -> None:
        tab = ttk.Frame(self.notebook, padding=7)
        self.notebook.add(tab, text="МКФ")
        container = ttk.Frame(tab)
        container.pack(fill="both", expand=True)
        columns = (
            "code",
            "description",
            "q0",
            "q1",
            "q2",
            "q3",
            "q4",
            "initial",
            "final",
            "responsible",
            "dynamic",
        )
        self.icf_tree = ttk.Treeview(
            container,
            columns=columns,
            show="tree headings",
            selectmode="extended",
        )
        apply_icf_grid_style(self.icf_tree)
        vertical = ttk.Scrollbar(container, orient="vertical", command=self.icf_tree.yview)
        horizontal = ttk.Scrollbar(container, orient="horizontal", command=self.icf_tree.xview)
        self.icf_tree.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        self.icf_tree.grid(row=0, column=0, sticky="nsew")
        vertical.grid(row=0, column=1, sticky="ns")
        horizontal.grid(row=1, column=0, sticky="ew")
        container.rowconfigure(0, weight=1)
        container.columnconfigure(0, weight=1)
        self.icf_tree.heading("#0", text="Раздел")
        self.icf_tree.column("#0", width=185, minwidth=145, anchor="w")
        headings = {
            "code": "Код",
            "description": "МКФ категория",
            "q0": "0",
            "q1": "1",
            "q2": "2",
            "q3": "3",
            "q4": "4",
            "initial": "Исх.",
            "final": "Повт.",
            "responsible": "Ответственный специалист / уточнение",
            "dynamic": "+/−",
        }
        widths = {
            "code": 75,
            "description": 280,
            "q0": 34,
            "q1": 34,
            "q2": 34,
            "q3": 34,
            "q4": 34,
            "initial": 52,
            "final": 52,
            "responsible": 290,
            "dynamic": 45,
        }
        for column in columns:
            self.icf_tree.heading(column, text=headings[column])
            anchor = "center" if column.startswith("q") or column in {"initial", "final", "dynamic"} else "w"
            self.icf_tree.column(column, width=widths[column], minwidth=28, anchor=anchor)
        self.icf_tree.tag_configure("section", background="#e2e7ed")
        self.icf_tree.tag_configure("new", foreground="#1f63c5")
        self._icf_editor = InlineTreeEditor(
            self.icf_tree,
            editable_columns={"code", "description", "initial", "final", "responsible"},
            commit=self._commit_icf_cell,
            values=self._icf_editor_values,
            activate=self._activate_icf_item,
            is_data_row=str.isdigit,
        )
        self.icf_tree.bind("<ButtonPress-1>", self._start_icf_drag, add="+")
        self.icf_tree.bind("<ButtonRelease-1>", self._finish_icf_pointer, add="+")
        self._bind_tree_delete(self.icf_tree, self._delete_icf)

    def _build_sources_tab(self) -> None:
        tab = ttk.Frame(self.notebook, padding=7)
        self.notebook.add(tab, text="Источники")
        columns = ("role", "clinical_datetime", "document_type", "path")
        self.source_tree = self._create_tree_with_scrollbars(tab, columns)
        for column, heading, width in (
            ("role", "Роль", 250),
            ("clinical_datetime", "Клиническая дата", 155),
            ("document_type", "Тип документа", 190),
            ("path", "Путь", 610),
        ):
            self.source_tree.heading(column, text=heading)
            self.source_tree.column(column, width=width, minwidth=65, anchor="w")
        self.source_tree.tag_configure("used", background="#e3f3df")
        self.source_tree.tag_configure("excluded", background="#e4e4e4", foreground="#666666")
        self.source_tree.bind("<Double-1>", self._open_selected_source)

    def _build_procedures_tab(self) -> None:
        tab = ttk.Frame(self.notebook, padding=7)
        self.notebook.add(tab, text="Программа")
        columns = ("code", "name", "specialist", "count", "duration", "frequency")
        self.procedure_tree = self._create_tree_with_scrollbars(
            tab, columns, selectmode="extended"
        )
        headings = {
            "code": "Код",
            "name": "Процедура",
            "specialist": "Ответственный",
            "count": "Кол-во",
            "duration": "Мин.",
            "frequency": "Кратность",
        }
        widths = {"code": 120, "name": 420, "specialist": 220, "count": 70, "duration": 65, "frequency": 90}
        for column in columns:
            self.procedure_tree.heading(column, text=headings[column])
            self.procedure_tree.column(column, width=widths[column], minwidth=45, anchor="w")
        self.procedure_tree.bind("<Double-1>", lambda _event: self._edit_procedure())
        self._bind_tree_delete(self.procedure_tree, self._delete_procedure)
        self._procedure_editor = InlineTreeEditor(
            self.procedure_tree,
            editable_columns=set(columns),
            commit=self._commit_procedure_cell,
            activate=self._activate_procedure_item,
            is_data_row=str.isdigit,
        )

    def _build_scales_tab(self) -> None:
        tab = ttk.Frame(self.notebook, padding=7)
        self.notebook.add(tab, text="Шкалы")
        columns = ("role", "date", "name", "value", "source")
        self.scale_tree = self._create_tree_with_scrollbars(tab, columns, selectmode="extended")
        for column, heading, width in (
            ("role", "Специалист", 245),
            ("date", "Дата и время", 145),
            ("name", "Шкала/опросник", 410),
            ("value", "Результат", 210),
            ("source", "Источник", 260),
        ):
            self.scale_tree.heading(column, text=heading)
            self.scale_tree.column(column, width=width, minwidth=55, anchor="w")
        self.scale_tree.bind("<Double-1>", lambda _event: self._edit_scale())
        self._bind_tree_delete(self.scale_tree, self._delete_scale)
        buttons = ttk.Frame(tab)
        buttons.pack(fill="x", pady=(6, 0))
        ttk.Button(buttons, text="Добавить…", command=self._add_scale).pack(side="left")
        ttk.Button(buttons, text="Изменить…", command=self._edit_scale).pack(side="left", padx=4)
        ttk.Button(buttons, text="Удалить", command=self._delete_scale).pack(side="left")

    def _build_specialists_tab(self) -> None:
        tab = ttk.Frame(self.notebook, padding=7)
        self.notebook.add(tab, text="Специалисты")
        pane = ttk.Panedwindow(tab, orient="horizontal")
        pane.pack(fill="both", expand=True)

        left = ttk.Frame(pane)
        right = ttk.Frame(pane)
        pane.add(left, weight=1)
        pane.add(right, weight=4)

        finding_columns = ("role", "date", "scales", "conclusion")
        self.finding_tree = self._create_tree_with_scrollbars(left, finding_columns)
        self.finding_tree.configure(displaycolumns=("role", "date"))
        for column, heading, width in (
            ("role", "Специалист", 230),
            ("date", "Дата", 105),
            ("scales", "Шкал", 55),
            ("conclusion", "Заключение", 10),
        ):
            self.finding_tree.heading(column, text=heading)
            self.finding_tree.column(column, width=width, minwidth=45, anchor="w")
        self.finding_tree.bind("<<TreeviewSelect>>", self._on_specialist_selected)
        self._bind_tree_delete(self.finding_tree, self._delete_finding)

        self.specialist_header_var = tk.StringVar()
        header = ttk.Frame(right)
        header.pack(fill="x", pady=(0, 7))
        ttk.Label(header, textvariable=self.specialist_header_var, font=("TkDefaultFont", 12, "bold")).pack(side="left")
        self.specialist_source_button = ttk.Button(header, text="Источник", command=self._open_specialist_source)
        self.specialist_source_button.pack(side="right")

        ttk.Label(right, text="Шкалы и опросники").pack(anchor="w", pady=(0, 3))
        scale_columns = ("name", "initial", "final")
        self.scale_tree = self._create_tree_with_scrollbars(right, scale_columns, selectmode="extended")
        self.scale_tree.master.pack_configure(fill="x", expand=False)
        for column, heading, width in (
            ("name", "Шкала / опросник", 440),
            ("initial", "Исходное значение", 150),
            ("final", "Повторное значение", 160),
        ):
            self.scale_tree.heading(column, text=heading)
            self.scale_tree.column(column, width=width, minwidth=70, anchor="w")
        self.scale_tree.tag_configure("new", foreground="#1f63c5")
        self._scale_editor = InlineTreeEditor(
            self.scale_tree,
            editable_columns=set(scale_columns),
            commit=self._commit_scale_cell,
            activate=self._activate_scale_item,
            is_data_row=lambda item: item in self._scale_pair_refs,
        )
        self._bind_tree_delete(self.scale_tree, self._delete_scale)

        conclusion_bar = ttk.Frame(right)
        conclusion_bar.pack(fill="x", pady=(10, 3))
        ttk.Label(conclusion_bar, text="Заключение").pack(side="left")
        self.conclusion_source_button = ttk.Button(conclusion_bar, text="Источник", command=self._open_specialist_source)
        self.conclusion_source_button.pack(side="right")
        self.specialist_conclusion = scrolledtext.ScrolledText(right, height=12, wrap="word", undo=True)
        self.specialist_conclusion.pack(fill="both", expand=True)
        self.specialist_conclusion.bind("<FocusOut>", self._commit_specialist_conclusion)

    def _build_findings_tab(self) -> None:
        tab = ttk.Frame(self.notebook, padding=7)
        self.notebook.add(tab, text="Заключения")
        columns = ("role", "date", "scales", "conclusion")
        self.finding_tree = self._create_tree_with_scrollbars(
            tab, columns, selectmode="extended"
        )
        for column, heading, width in (
            ("role", "Специалист", 250),
            ("date", "Клиническая дата", 145),
            ("scales", "Шкал", 55),
            ("conclusion", "Заключение", 620),
        ):
            self.finding_tree.heading(column, text=heading)
            self.finding_tree.column(column, width=width, minwidth=45, anchor="w")
        self.finding_tree.bind("<Double-1>", lambda _event: self._edit_finding())
        self._bind_tree_delete(self.finding_tree, self._delete_finding)
        buttons = ttk.Frame(tab)
        buttons.pack(fill="x", pady=(6, 0))
        ttk.Button(buttons, text="Добавить…", command=self._add_finding).pack(side="left")
        ttk.Button(buttons, text="Изменить…", command=self._edit_finding).pack(side="left", padx=4)
        ttk.Button(buttons, text="Удалить", command=self._delete_finding).pack(side="left")

    def _build_issues_tab(self) -> None:
        tab = ttk.Frame(self.notebook, padding=7)
        self.issues_tab = tab
        self.notebook.add(tab, text="Предупреждения")
        columns = ("message", "field", "source")
        self.issue_tree = self._create_tree_with_scrollbars(tab, columns)
        for column, heading, width in (
            ("message", "Сообщение", 580),
            ("field", "Поле", 170),
            ("source", "Источник", 310),
        ):
            self.issue_tree.heading(column, text=heading)
            self.issue_tree.column(column, width=width, minwidth=60, anchor="w")
        self.issue_tree.tag_configure("blocking", background="#ffd6d6")
        self.issue_tree.tag_configure("warning", background="#fff4c2")
        self.issue_tree.tag_configure("info", background="#e6f1ff")
        self.issue_tree.bind("<Double-1>", self._open_selected_issue_source)
        buttons = ttk.Frame(tab)
        buttons.pack(fill="x", pady=(6, 0))
        ttk.Button(
            buttons,
            text="Игнорировать выбранное…",
            command=self._acknowledge_selected_issue,
        ).pack(side="left")
        ttk.Button(
            buttons,
            text="Сбросить игнорирование",
            command=self._reset_issue_acknowledgements,
        ).pack(side="left", padx=4)
        ttk.Button(
            buttons,
            text="Обновить после правок",
            command=self._refresh_issues,
        ).pack(side="left")

    @staticmethod
    def _create_tree_with_scrollbars(
        parent: ttk.Frame,
        columns: tuple[str, ...],
        *,
        selectmode: str = "browse",
    ) -> ttk.Treeview:
        container = ttk.Frame(parent)
        container.pack(fill="both", expand=True)
        tree = ttk.Treeview(
            container,
            columns=columns,
            show="headings",
            selectmode=selectmode,
        )
        vertical = ttk.Scrollbar(container, orient="vertical", command=tree.yview)
        horizontal = ttk.Scrollbar(container, orient="horizontal", command=tree.xview)
        tree.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        tree.grid(row=0, column=0, sticky="nsew")
        vertical.grid(row=0, column=1, sticky="ns")
        horizontal.grid(row=1, column=0, sticky="ew")
        container.rowconfigure(0, weight=1)
        container.columnconfigure(0, weight=1)
        return tree

    @staticmethod
    def _bind_tree_delete(tree: ttk.Treeview, command: Callable[[], None]) -> None:
        def delete_selected(_event: tk.Event) -> str:
            command()
            return "break"

        tree.bind("<Delete>", delete_selected)
        tree.bind("<KP_Delete>", delete_selected)

    def _set_folder_field(self, value: str) -> None:
        self._setting_folder_field = True
        try:
            self.folder_var.set(value)
        finally:
            self._setting_folder_field = False

    def _mark_entry_dirty(self, key: str) -> None:
        if not self._populating:
            self._dirty_entry_fields.add(key)
            if self.episode is not None and key == "duration":
                self.episode.course_duration_manual = True
            if key in {"admission", "meeting"}:
                self.root.after_idle(self._refresh_duration)
            source_key = {
                "full_name": "identity.full_name",
                "record_number": "identity.medical_record_number",
                "birth_date": "identity.birth_date",
                "sex": "identity.sex",
                'admission': 'admission_datetime',
                'meeting': 'meeting_at',
                'department': 'department',
                'stage': 'stage',
                'duration': 'course_duration_days',
            }.get(key)
            if source_key and (button := self._field_source_buttons.get(source_key)):
                button.configure(text="Ручная правка", state="normal")

    def _refresh_duration(self):
        if self.episode is None or self.episode.course_duration_manual:
            return
        from mdrk_builder.application.editing import hospitalization_days
        from mdrk_builder.ui.episode_adapter import parse_optional_datetime
        try:
            admission = parse_optional_datetime(self._entry_variables['admission'].get())
            end = self.episode.discharge_datetime or (
                parse_optional_datetime(self._entry_variables['meeting'].get())
                if self._current_kind is MdrkKind.FINAL else self.episode.final_meeting_at
            )
        except ValueError:
            return
        value = hospitalization_days(admission, end)
        self.episode.course_duration_days = value
        self._populating = True
        try:
            self._entry_variables['duration'].set('' if value is None else str(value))
        finally:
            self._populating = False

    def _automatic_duration(self):
        if self.episode:
            self.episode.course_duration_manual = False
            self._dirty_entry_fields.discard('duration')
            self._refresh_duration()
            self.status_var.set('Койко-дни: автоматически по датам; до подтверждения выписки — по дате МДРК-2')

    def _mark_section_dirty(self, key: str) -> None:
        if not self._populating:
            self._dirty_section_fields[self._current_kind].add(key)
            source_key = f"sections.{key}"
            if button := self._field_source_buttons.get(source_key):
                button.configure(text="Ручная правка", state="normal")

    def _clear_manual_edits(self) -> None:
        getattr(self, "_dirty_entry_fields", set()).clear()
        for fields in getattr(self, "_dirty_section_fields", {}).values():
            fields.clear()
        getattr(self, "_manual_collections", set()).clear()
        self._pending_manual_state = None

    def _mark_collection_dirty(self, name: str) -> None:
        if not hasattr(self, "_manual_collections"):
            self._manual_collections = set()
        self._manual_collections.add(name)

    def _capture_manual_state(self) -> dict[str, object] | None:
        if not self.episode:
            return None
        try:
            form = self._parsed_form_data()
        except (AttributeError, KeyError, ValueError):
            form = None
        previous = deepcopy(self.episode)
        if form is not None:
            previous.identity.full_name = form.full_name
            previous.identity.medical_record_number = form.medical_record_number
            previous.identity.birth_date = form.birth_date
            previous.identity.sex = form.sex
            previous.admission_datetime = form.admission_datetime
            previous.department = form.department
            previous.stage = form.stage
            previous.course_duration_days = form.course_duration_days
            if self._current_kind is MdrkKind.INITIAL:
                previous.initial_meeting_at = form.meeting_at
            else:
                previous.final_meeting_at = form.meeting_at
            target_sections = sections_for(previous, self._current_kind)
            for key, value in form.section_values:
                setattr(target_sections, key, value)
        return {
            "episode": previous,
            "baseline": deepcopy(getattr(self, "_scan_baseline", self.episode)),
            "entry_fields": set(getattr(self, "_dirty_entry_fields", set())),
            "section_fields": {
                kind: set(fields)
                for kind, fields in getattr(self, "_dirty_section_fields", {}).items()
            },
            "collections": set(getattr(self, "_manual_collections", set())),
        }

    def _merge_manual_state(self, episode: Episode) -> None:
        state = self._pending_manual_state
        self._pending_manual_state = None
        if not state:
            self._clear_manual_edits()
            return
        previous = state["episode"]
        if not isinstance(previous, Episode):
            return
        episode.course_duration_manual = previous.course_duration_manual
        episode.discharge_datetime = previous.discharge_datetime or episode.discharge_datetime
        entry_fields = state["entry_fields"]
        if isinstance(entry_fields, set):
            entry_mapping = {
                "full_name": (episode.identity, "full_name", previous.identity.full_name),
                "record_number": (
                    episode.identity,
                    "medical_record_number",
                    previous.identity.medical_record_number,
                ),
                "birth_date": (episode.identity, "birth_date", previous.identity.birth_date),
                "sex": (episode.identity, "sex", previous.identity.sex),
                "admission": (episode, "admission_datetime", previous.admission_datetime),
                "department": (episode, "department", previous.department),
                "stage": (episode, "stage", previous.stage),
                "duration": (episode, "course_duration_days", previous.course_duration_days),
            }
            for key in entry_fields:
                if key in entry_mapping:
                    owner, attribute, value = entry_mapping[key]
                    setattr(owner, attribute, value)
                    source_key = {
                        "full_name": "identity.full_name",
                        "record_number": "identity.medical_record_number",
                        "birth_date": "identity.birth_date",
                        "sex": "identity.sex",
                    }.get(key)
                    if source_key:
                        for mapping, old in ((episode.field_sources, previous.field_sources),
                                             (episode.initial_field_sources, previous.initial_field_sources)):
                            for name in list(mapping):
                                if name == source_key or name.startswith(source_key + "."):
                                    mapping.pop(name)
                            mapping.update({name: path for name, path in old.items()
                                            if name == source_key or name.startswith(source_key + ".")})
        if 'meeting' in entry_fields:
            episode.initial_meeting_at = previous.initial_meeting_at
            episode.final_meeting_at = previous.final_meeting_at
        if not episode.course_duration_manual:
            from mdrk_builder.application.scanner import _update_course_duration
            _update_course_duration(episode)
        section_fields = state["section_fields"]
        if isinstance(section_fields, dict):
            for kind in (MdrkKind.INITIAL, MdrkKind.FINAL):
                keys = section_fields.get(kind, set())
                old_sections = sections_for(previous, kind)
                new_sections = sections_for(episode, kind)
                for key in keys:
                    setattr(new_sections, key, getattr(old_sections, key))
                    source_key = f"sections.{key}"
                    mapping = episode.initial_field_sources if kind is MdrkKind.INITIAL else episode.field_sources
                    old = previous.initial_field_sources if kind is MdrkKind.INITIAL else previous.field_sources
                    for name in list(mapping):
                        if name == source_key or name.startswith(source_key + "."):
                            mapping.pop(name)
                    mapping.update({name: path for name, path in old.items()
                                    if name == source_key or name.startswith(source_key + ".")})
        baseline = state.get("baseline")
        collections = state["collections"]
        if isinstance(collections, set):
            if "icf" in collections:
                episode.icf_domains, notes = merge_rows(getattr(baseline, "icf_domains", []), previous.icf_domains, episode.icf_domains)
                episode.issues.extend(merge_issues(notes))
            if "procedures" in collections:
                episode.procedures, notes = merge_rows(getattr(baseline, "procedures", []), previous.procedures, episode.procedures)
                episode.issues.extend(merge_issues(notes))
            if "findings" in collections:
                episode.findings, notes = merge_rows(getattr(baseline, "findings", []), previous.findings, episode.findings)
                episode.issues.extend(merge_issues(notes))

    def _on_folder_field_changed(self, *_args: str) -> None:
        if self._setting_folder_field:
            return
        if hasattr(self, "discharge_workspace") and self._state_folder():
            if not self._save_workspace(explicit=True):
                self._set_folder_field(str(self._state_folder()))
                return
        if hasattr(self, 'discharge_workspace'):
            self._clear_workspace()
        else:
            self._clear_manual_edits()
            self._invalidate_episode()
        self._update_action_states()
        self.status_var.set("Папка изменена. Предыдущий рабочий черновик сохранён; выполните сканирование заново.")

    def _folder_field_matches(self, expected: Path) -> bool:
        try:
            return parse_episode_folder(self.folder_var.get()) == expected.resolve()
        except (OSError, ValueError):
            return False

    def _invalidate_episode(self) -> None:
        for editor in (getattr(self, '_icf_editor', None), getattr(self, '_procedure_editor', None), getattr(self, '_scale_editor', None)):
            if editor is not None:
                editor.cancel()
        self.episode = None
        self._last_form_error = ""
        previous_populating = getattr(self, "_populating", False)
        self._populating = True
        try:
            for variable in self._entry_variables.values():
                variable.set("")
            for widget in self._text_fields.values():
                widget.delete("1.0", "end")
                widget.edit_reset()
        finally:
            self._populating = previous_populating
        for tree in (
            self.source_tree,
            self.icf_tree,
            self.scale_tree,
            self.procedure_tree,
            self.finding_tree,
            self.issue_tree,
        ):
            self._clear_tree(tree)
        self._scale_refs = []
        self._scale_pair_refs = {}
        self._displayed_specialist_finding = None
        self._issue_refs = {}
        if hasattr(self, 'specialist_conclusion'):
            self.specialist_conclusion.delete('1.0', 'end')
            self.specialist_conclusion.edit_reset()
        self._update_field_sources()
        self._update_action_states()

    def _choose_folder(self) -> None:
        selected = filedialog.askdirectory(title="Выберите папку эпизода")
        if selected:
            if not self._confirm_leave():
                return
            self._clear_workspace()
            self._set_folder_field(selected)
            if not self._restore_workspace(Path(selected)):
                self._start_scan()

    def _on_document_changed(self) -> None:
        document = self.document_var.get()
        previous = getattr(self, "_previous_document", "mdrk1")
        if previous in {"mdrk1", "mdrk2"} and self.episode:
            state = self._capture_manual_state()
        elif previous == "discharge" and self.discharge_workspace.draft:
            if not self.discharge_workspace.apply():
                self.document_var.set(previous)
                return
        from mdrk_builder.application.shared_edits import transfer_identity
        source = None
        fields = set()
        if previous in {'mdrk1', 'mdrk2'} and self.episode:
            source = state['episode'] if state else self.episode
            fields = set(self._dirty_entry_fields)
        elif previous == 'discharge':
            source = self.discharge_workspace.draft
            fields = set(self.discharge_workspace._dirty_identity)
        elif previous == 'reverse' and self.reverse_workspace.draft:
            if not self.reverse_workspace.apply():
                self.document_var.set(previous)
                return
            source = self.reverse_workspace.draft
            fields = set(self.reverse_workspace._header_dirty)
        if source:
            shared = fields & {'full_name', 'record_number', 'birth_date', 'sex', 'admission', 'discharge'}
            for panel, dirty in ((self.reverse_workspace, '_header_dirty'), (self.discharge_workspace, '_dirty_identity')):
                if panel.draft is not None and panel.draft is not source:
                    transfer_identity(source, panel.draft, shared)
                    getattr(panel, dirty).update(shared & set(getattr(panel, '_header_vars', getattr(panel, '_identity_vars', {}))))
                    panel._populate()
            if self.episode is not None and source is not self.episode:
                transfer_identity(source, self.episode, shared)
                self._dirty_entry_fields.update(shared - {'discharge'})
                if previous not in {'mdrk1', 'mdrk2'}:
                    self._populate_from_episode()
        self._previous_document = document
        self.mdrk_workspace.pack_forget()
        self.reverse_workspace.pack_forget()
        self.discharge_workspace.pack_forget()
        if document in {"mdrk1", "mdrk2"}:
            self.mdrk_workspace.pack(fill="both", expand=True)
            self.kind_var.set(
                MdrkKind.INITIAL.value if document == "mdrk1" else MdrkKind.FINAL.value
            )
            self._on_kind_changed()
        elif document == "reverse":
            self.reverse_workspace.pack(fill="both", expand=True)
            if self.reverse_draft is None:
                self._start_reverse_sheet_scan()
        elif document == "discharge":
            self.discharge_workspace.pack(fill="both", expand=True)
            if self.discharge_draft is None:
                self._start_discharge_summary_scan()
        self._update_action_states()

    def _select_document(self, document: str) -> str:
        self.document_var.set(document)
        self._on_document_changed()
        return "break"

    def _rescan_current_document(self) -> None:
        document = self.document_var.get()
        if document == "reverse":
            self._start_reverse_sheet_scan()
        elif document == "discharge":
            self._start_discharge_summary_scan()
        else:
            self._start_scan()

    _open_path = staticmethod(open_source_path)

    def _build_table_source_access(self) -> None:
        self._table_sources = {}
        for name, tree in (
            ("icf", self.icf_tree), ("procedure", self.procedure_tree),
            ("scale", self.scale_tree), ("finding", self.finding_tree),
            ("issue", self.issue_tree), ("source", self.source_tree),
        ):
            bar = ttk.Frame(tree.master.master)
            bar.pack(fill="x", pady=(0, 5), before=tree.master)
            if name == "icf":
                ttk.Label(bar, text="Источники строки: ПКМ или Shift+F10").pack(side="left")
            self._table_sources[name] = TableSourceAccess(
                tree, bar,
                links=lambda item, table=name: self._table_source_links(table, item),
                open_path=self._open_path,
                show_button=name != "icf",
            )

    def _table_source_links(self, table: str, item: str) -> SourceLinks:
        if self.episode is None:
            return ()
        if table == "scale":
            row = self._scale_pair_refs.get(item)
            if row is None:
                return ()
            links = list(row_source_links(row.initial, "Исходное значение")) if row.initial else [("Исходное значение", None)]
            if self._current_kind is MdrkKind.FINAL:
                links.extend(row_source_links(row.current, "Итоговое значение") if row.current else [("Итоговое значение", None)])
            return links
        if table == "issue":
            issue = self._issue_refs.get(item)
            return [("Источник предупреждения", issue.source)] if issue else ()
        if not item.isdigit():
            return ()
        index = int(item)
        if table == "icf" and index < len(self.episode.icf_domains):
            return icf_source_links(self.episode.icf_domains[index], include_final=self._current_kind is MdrkKind.FINAL)
        if table == "procedure" and index < len(self.episode.procedures):
            return row_source_links(self.episode.procedures[index], "Лист назначений")
        if table == "finding" and index < len(self.episode.findings):
            return row_source_links(self.episode.findings[index], "Документ специалиста")
        if table == "source" and index < len(self.episode.sources):
            return [("Исходный документ", self.episode.sources[index].path)]
        return ()

    def _open_selected_source(self, _event: tk.Event | None = None) -> None:
        if not self.episode:
            return
        selected = self.source_tree.selection()
        if selected and selected[0].isdigit():
            self._open_path(self.episode.sources[int(selected[0])].path)

    def _go_to_selected_issue(self, event=None):
        from mdrk_builder.ui.workspace_search import reveal
        issue=self._selected_issue()
        if issue is None:return
        field=issue.field
        name=field.split(".")[-1]
        if name in self._text_fields:
            reveal(self._text_fields[name]);return
        for prefix,tree in (("icf",self.icf_tree),("procedures",self.procedure_tree),("scales",self.scale_tree)):
            if field.startswith(prefix):
                parts=field.split(".");item=parts[1] if len(parts)>1 else next(iter(tree.get_children()),None)
                reveal(tree,item);return

    def _open_selected_issue_source(self, _event: tk.Event | None = None) -> None:
        selected = self.issue_tree.selection()
        if selected and (issue := self._issue_refs.get(selected[0])) is not None:
            self._open_path(issue.source)

    def _open_specialist_source(self) -> None:
        finding = self._selected_specialist_finding()
        open_source_links(self.specialist_source_button, row_source_links(finding) if finding else (), self._open_path)

    def _field_source_links(self, field_key: str) -> SourceLinks:
        if self.episode is None:
            return ()
        source_map = self.episode.field_sources
        if field_key.startswith("sections.") and self._current_kind is MdrkKind.INITIAL:
            source_map = self.episode.initial_field_sources
        entry_keys = {"identity.full_name": "full_name", "identity.medical_record_number": "record_number",
                      "identity.birth_date": "birth_date", "identity.sex": "sex", "admission_datetime": "admission",
                      "meeting_at": "meeting", "department": "department", "stage": "stage", "course_duration_days": "duration"}
        manual = (entry_keys.get(field_key) in self._dirty_entry_fields or
                  field_key.removeprefix("sections.") in self._dirty_section_fields[self._current_kind])
        links = list(field_source_links(source_map, field_key, manual=manual))
        if links:
            return links
        if field_key in {"department", "stage", "sections.rehabilitation_potential"}:
            return [("Шаблон: значение по умолчанию, не извлечено из документа", None)]
        if field_key in {"sections.goal", "sections.tasks"} and self._current_kind is MdrkKind.FINAL:
            return [("Шаблон: установленная формулировка для МДРК-2", None)]
        if field_key in {"meeting_at", "course_duration_days"}:
            explanation = ("Расчёт: заседание определяется по поступлению и датам документов; ручная дата имеет приоритет"
                           if field_key == "meeting_at" else "Расчёт: разность дат итогового заседания и поступления; один день при совпадении")
            return [(explanation, None), *field_source_links(source_map, "admission_datetime"),
                    *field_source_links(source_map, "final_meeting_at")]
        return ()

    def _open_field_source(self, field_key: str) -> None:
        open_source_links(self._field_source_buttons[field_key], self._field_source_links(field_key), self._open_path)

    def _update_field_sources(self) -> None:
        self._field_source_paths = {}
        for field_key, button in getattr(self, "_field_source_buttons", {}).items():
            links = self._field_source_links(field_key)
            if not links:
                button.configure(text="Источник не указан", state="disabled")
                continue
            paths = [path for _, path in links if path is not None]
            if paths:
                self._field_source_paths[field_key] = paths[0]
            button.configure(text="Источник", state="normal")

    def _start_scan(self) -> None:
        if self._scanning:
            return
        try:
            folder = parse_episode_folder(self.folder_var.get())
        except (OSError, ValueError) as exc:
            messagebox.showerror("Папка не найдена", str(exc))
            return
        try:
            meeting_variable = self._entry_variables.get("meeting")
            entered_meeting = parse_optional_meeting_datetime(
                meeting_variable.get() if meeting_variable is not None else ""
            )
            record_variable = self._entry_variables.get("record_number")
            entered_record_number = (
                record_variable.get().strip() if record_variable is not None else ""
            )
            admission_variable = self._entry_variables.get("admission")
            entered_admission = parse_optional_datetime(
                admission_variable.get() if admission_variable is not None else ""
            )
        except ValueError as exc:
            messagebox.showerror("Проверьте даты и реквизиты", str(exc))
            return
        if not folder.is_dir():
            messagebox.showerror("Папка не найдена", "Выберите существующую папку эпизода.")
            return
        scan_overrides: dict[str, datetime | str] = {}
        if self.episode is not None:
            record_number_changed = (
                bool(entered_record_number)
                and normalize_medical_record_number(entered_record_number)
                != normalize_medical_record_number(
                    self.episode.materialized_medical_record_number
                    or self.episode.identity.medical_record_number
                )
            )
            admission_changed = (
                entered_admission is not None
                and entered_admission
                != (
                    self.episode.materialized_admission_datetime
                    or self.episode.admission_datetime
                )
            )
            metadata_changed = record_number_changed or admission_changed
            if entered_record_number:
                scan_overrides["medical_record_number_override"] = entered_record_number
            if entered_admission is not None and (
                not record_number_changed or admission_changed
            ):
                scan_overrides["admission_datetime_override"] = entered_admission

            if metadata_changed:
                current_meeting = self.episode.meeting_at(self._current_kind)
                if entered_meeting is not None and entered_meeting != current_meeting:
                    override_name = (
                        "initial_meeting_at"
                        if self._current_kind is MdrkKind.INITIAL
                        else "final_meeting_at"
                    )
                    scan_overrides[override_name] = entered_meeting
            else:
                initial_meeting = self.episode.initial_meeting_at
                final_meeting = self.episode.final_meeting_at
                if self._current_kind is MdrkKind.INITIAL:
                    initial_meeting = entered_meeting
                else:
                    final_meeting = entered_meeting
                if initial_meeting is not None:
                    scan_overrides["initial_meeting_at"] = initial_meeting
                if final_meeting is not None:
                    scan_overrides["final_meeting_at"] = final_meeting
        elif entered_meeting is not None:
            override_name = (
                "initial_meeting_at"
                if self._current_kind is MdrkKind.INITIAL
                else "final_meeting_at"
            )
            scan_overrides[override_name] = entered_meeting
        self._pending_manual_state = self._capture_manual_state()
        self._invalidate_episode()
        self._set_folder_field(str(folder))
        if hasattr(self, "_scan_session"):
            self._scan_session.begin(folder)
        self._active_job_folder = folder
        self._set_scanning(True)
        self.status_var.set("Сканирование исходных документов…")
        self._start_background_job(
            lambda: scan_patient_folder(folder, scan_session=getattr(self,"_scan_session",None), **scan_overrides),
            self._finish_scan,
            thread_name="mdrk-folder-scan",
        )

    def _finish_scan(self, episode: Episode | None, error: Exception | None) -> None:
        self._set_scanning(False)
        scan_folder = self._active_job_folder
        self._active_job_folder = None
        if error is not None:
            state = self._pending_manual_state
            if state and state.get("episode"):
                self.episode = state["episode"]
                self._pending_manual_state = None
                self._populate_from_episode()
            self.status_var.set("Сканирование завершилось ошибкой; предыдущие правки сохранены")
            messagebox.showerror("Ошибка сканирования", str(error))
            return
        if (
            episode is None
            or scan_folder is None
            or not self._folder_field_matches(scan_folder)
            or episode.folder.resolve() != scan_folder
        ):
            self._invalidate_episode()
            self.status_var.set("Результат отброшен: папка изменилась во время сканирования.")
            return
        source_baseline = deepcopy(episode)
        old_baseline = getattr(self, '_scan_baseline', None)
        from mdrk_builder.application.editing import change_summary
        summary = change_summary(old_baseline, source_baseline)
        self._merge_manual_state(episode)
        self._scan_baseline = source_baseline
        if hasattr(self, "_table_history"):
            self._table_history.clear()
        self.episode = episode
        if hasattr(self, '_table_editor'):
            changed = self._table_editor.episode_loaded(self.discharge_workspace.source_baseline)
            self._manual_collections.update(changed)
        self._set_folder_field(str(episode.folder))
        self._populate_from_episode()
        self._update_action_states()
        self.status_var.set(
            f"Готово: {len(episode.sources)} источников, {len(episode.icf_domains)} доменов, "
            f"{len(episode.procedures)} процедур. {summary}"
        )

    def _start_reverse_sheet_scan(self) -> None:
        if self._scanning:
            return
        folder = self._auxiliary_scan_folder()
        if folder is None:
            return
        if hasattr(self, "_scan_session"):
            self._scan_session.begin(folder)
        self._active_job_folder = folder
        self._set_scanning(True)
        self.status_var.set("Сбор оборотного листа из документов консультаций…")
        self._start_background_job(
            lambda: scan_reverse_sheet(folder, scan_session=getattr(self,"_scan_session",None)),
            self._finish_reverse_sheet_scan,
            thread_name="mdrk-reverse-sheet-scan",
        )

    def _finish_reverse_sheet_scan(
        self,
        draft: ReverseSheetDraft | None,
        error: Exception | None,
    ) -> None:
        self._set_scanning(False)
        scan_folder = self._active_job_folder
        self._active_job_folder = None
        if error is not None:
            self.status_var.set("Сбор оборотного листа завершился ошибкой")
            messagebox.showerror("Ошибка сканирования", str(error))
            return
        if draft is None or scan_folder is None or not self._folder_field_matches(scan_folder):
            self.status_var.set("Результат оборотного листа отброшен: папка изменилась.")
            return
        self.status_var.set(
            f"Оборотный лист: найдено строк — {len(draft.rows)}, требует проверки — {len(draft.issues)}"
        )
        from mdrk_builder.application.shared_edits import transfer_identity
        state = self._capture_manual_state()
        if state:
            transfer_identity(state['episode'], draft, self._dirty_entry_fields)
        if self.discharge_workspace.draft:
            transfer_identity(self.discharge_workspace.draft, draft, self.discharge_workspace._dirty_identity)
        if self.reverse_draft is None:
            self.reverse_workspace.load(draft)
        elif self.reverse_workspace.merge_scan(draft) is False:
            self.status_var.set("Результат сканирования не применён. Исправьте поля оборотного листа и повторите сканирование.")
            return
        self.reverse_draft = self.reverse_workspace.draft
        if getattr(self.reverse_workspace, "_last_change_summary", ""):
            self.status_var.set(self.status_var.get() + ". " + self.reverse_workspace._last_change_summary)
        self._update_action_states()

    def _start_discharge_summary_scan(self) -> None:
        if self._scanning:
            return
        folder = self._auxiliary_scan_folder()
        if folder is None:
            return

        overrides = {}
        if getattr(self, "episode", None) is not None and "admission" in self._dirty_entry_fields:
            overrides["admission_datetime_override"] = self.episode.admission_datetime
        panel = self.discharge_workspace
        if panel.draft is not None and panel.draft.folder.resolve() == folder.resolve():
            if not panel.apply():
                return
            for control, field in (("admission", "admission_datetime"), ("discharge", "discharge_datetime")):
                if control in panel._dirty_identity or field in panel.draft.manual_fields:
                    overrides[field + "_override"] = getattr(panel.draft, field)
        if hasattr(self, "_scan_session"):
            self._scan_session.begin(folder)
        self._active_job_folder = folder
        self._set_scanning(True)
        self.status_var.set("Сбор выписного эпикриза из документов эпизода…")
        self._start_background_job(
            lambda: scan_discharge_summary(folder, scan_session=getattr(self,"_scan_session",None), **overrides),
            self._finish_discharge_summary_scan,
            thread_name="mdrk-discharge-summary-scan",
        )

    def _finish_discharge_summary_scan(
        self,
        draft: DischargeSummaryDraft | None,
        error: Exception | None,
    ) -> None:
        self._set_scanning(False)
        scan_folder = self._active_job_folder
        self._active_job_folder = None
        if error is not None:
            self.status_var.set("Сбор выписного эпикриза завершился ошибкой")
            messagebox.showerror("Ошибка сканирования", str(error))
            return

        if draft is None or scan_folder is None or not self._folder_field_matches(scan_folder):
            self.status_var.set("Результат выписного эпикриза отброшен: папка изменилась.")
            return
        from mdrk_builder.application.shared_edits import transfer_episode_edits
        state = self._capture_manual_state()
        if state:
            transfer_episode_edits(state["episode"], getattr(self, "_scan_baseline", None), draft, self._dirty_entry_fields)
        blockers = len(
            [issue for issue in draft.issues if issue.severity is ReviewSeverity.BLOCKING]
        )
        warnings = len(
            [issue for issue in draft.issues if issue.severity is ReviewSeverity.WARNING]
        )
        self.status_var.set(
            "Выписной эпикриз собран: "
            f"блокирующих проблем — {blockers}, предупреждений — {warnings}"
        )
        if self.discharge_draft is None:
            self.discharge_workspace.load(draft)
        elif self.discharge_workspace.merge_scan(draft) is False:
            self.status_var.set("Результат сканирования не применён. Исправьте поля выписки и повторите сканирование.")
            return
        self.discharge_draft = self.discharge_workspace.draft
        if getattr(self.discharge_workspace, "_last_change_summary", ""):
            self.status_var.set(self.status_var.get() + ". " + self.discharge_workspace._last_change_summary)
        self._update_action_states()

    def _auxiliary_scan_folder(self) -> Path | None:
        try:
            folder = parse_episode_folder(self.folder_var.get())
        except (OSError, ValueError) as exc:
            messagebox.showerror("Папка не найдена", str(exc))
            return None
        if not folder.is_dir():
            messagebox.showerror("Папка не найдена", "Выберите существующую папку эпизода.")
            return None
        return folder

    def _start_background_job(
        self,
        operation: Callable[[], BackgroundResultT],
        on_finished: Callable[[BackgroundResultT | None, Exception | None], None],
        *,
        thread_name: str,
    ) -> None:
        runner = getattr(self, "_background_jobs", None)
        if runner is None:
            runner = BackgroundJobRunner(self.root, thread_factory=threading.Thread)
            self._background_jobs = runner
        runner.start(operation, on_finished, thread_name=thread_name)

    def _cancel_scan(self):
        if self._scanning:
            self._scan_session.cancelled.set()
            self.status_var.set("Отмена после завершения текущего файла…")

    def _set_scanning(self, value: bool) -> None:
        self._scanning = value
        if hasattr(self, "cancel_button"):
            self.cancel_button.configure(state="normal" if value else "disabled")
        self._update_action_states()
        if value:
            self.progress.grid()
            self.progress.start(12)
        else:
            self.progress.stop()
            self.progress.grid_remove()

    def _update_action_states(self) -> None:
        self.scan_button.configure(state="disabled" if self._scanning else "normal")
        reverse_button = getattr(self, "reverse_sheet_button", None)
        if reverse_button is not None:
            reverse_button.configure(state="disabled" if self._scanning else "normal")
        discharge_button = getattr(self, "discharge_summary_button", None)
        if discharge_button is not None:
            discharge_button.configure(state="disabled" if self._scanning else "normal")
        document_variable = getattr(self, "document_var", None)
        document = document_variable.get() if document_variable is not None else "mdrk1"
        has_document = {
            "reverse": getattr(self, "reverse_draft", None) is not None,
            "discharge": getattr(self, "discharge_draft", None) is not None,
        }.get(document, self.episode is not None)
        self.generate_button.configure(
            state="normal" if has_document and not self._scanning else "disabled"
        )

    def _populate_from_episode(self) -> None:
        if not self.episode:
            return
        episode = self.episode
        self._populating = True
        values = {
            "full_name": episode.identity.full_name,
            "record_number": episode.identity.medical_record_number,
            "birth_date": format_date(episode.identity.birth_date),
            "sex": episode.identity.sex,
            "admission": format_datetime(episode.admission_datetime),
            "meeting": format_datetime(episode.meeting_at(self._current_kind)),
            "department": episode.department,
            "stage": episode.stage,
            "duration": "" if episode.course_duration_days is None else str(episode.course_duration_days),
        }
        try:
            for key, value in values.items():
                self._entry_variables[key].set(value)
            current_sections = sections_for(episode, self._current_kind)
            for key, widget in self._text_fields.items():
                widget.delete("1.0", "end")
                widget.insert("1.0", getattr(current_sections, key))
                widget.edit_reset()
            self._update_field_sources()
            self._refresh_all_trees()
        finally:
            self._populating = False

    def _parsed_form_data(self) -> EpisodeFormData:
        entry_values = {
            key: variable.get() for key, variable in self._entry_variables.items()
        }
        section_values = {
            key: widget.get("1.0", "end-1c")
            for key, widget in self._text_fields.items()
        }
        return parse_episode_form_data(entry_values, section_values)

    def _apply_form(self, kind: MdrkKind | None = None) -> bool:
        if not self.episode:
            messagebox.showwarning("Нет данных", "Сначала просканируйте папку эпизода.")
            return False
        target_kind = kind or self._current_kind
        try:
            form = self._parsed_form_data()
        except ValueError as exc:
            self._last_form_error = str(exc)
            self._render_form_error()
            messagebox.showerror("Проверьте поля", str(exc))
            return False
        if form.meeting_at != self.episode.meeting_at(target_kind):
            self._last_form_error = MEETING_RESCAN_MESSAGE
            self.status_var.set("Время заседания изменено: нужно повторное сканирование.")
            messagebox.showerror("Нужно повторное сканирование", MEETING_RESCAN_MESSAGE)
            return False
        apply_episode_form_data(self.episode, target_kind, form)
        for key in self._dirty_entry_fields:
            source_key = {
                "full_name": "identity.full_name",
                "record_number": "identity.medical_record_number",
                "birth_date": "identity.birth_date",
                "sex": "identity.sex",
                'admission': 'admission_datetime',
                'meeting': 'meeting_at',
                'department': 'department',
                'stage': 'stage',
                'duration': 'course_duration_days',
            }.get(key)
            if source_key:
                self.episode.field_sources.pop(source_key, None)
                self.episode.initial_field_sources.pop(source_key, None)
        for key in self._dirty_section_fields[target_kind]:
            source_key = f"sections.{key}"
            self.episode.field_sources.pop(source_key, None)
            self.episode.initial_field_sources.pop(source_key, None)
        self._last_form_error = ""
        return True

    def _selected_kind(self) -> MdrkKind:
        return MdrkKind(self.kind_var.get())

    def _on_kind_changed(self) -> None:
        requested_kind = self._selected_kind()
        if self.episode:
            try:
                form = self._parsed_form_data()
            except ValueError as exc:
                self.kind_var.set(self._current_kind.value)
                if hasattr(self, "document_var"):
                    self.document_var.set("mdrk1" if self._current_kind is MdrkKind.INITIAL else "mdrk2")
                self._last_form_error = str(exc)
                self.status_var.set("Снимок не переключён: исправьте поля.")
                messagebox.showerror("Снимок не переключён", str(exc))
                return
            if form.meeting_at != self.episode.meeting_at(self._current_kind):
                self.kind_var.set(self._current_kind.value)
                if hasattr(self, "document_var"):
                    self.document_var.set("mdrk1" if self._current_kind is MdrkKind.INITIAL else "mdrk2")
                self._last_form_error = MEETING_RESCAN_MESSAGE
                self.status_var.set(
                    "Снимок не переключён: нужно повторное сканирование."
                )
                messagebox.showerror(
                    "Нужно повторное сканирование",
                    MEETING_RESCAN_MESSAGE,
                )
                return
            apply_episode_form_data(self.episode, self._current_kind, form)
        self._current_kind = requested_kind
        if self.episode:
            self._populate_from_episode()

    def _generate(self) -> None:
        if self._scanning:
            return
        document_variable = getattr(self, "document_var", None)
        document = document_variable.get() if document_variable is not None else "mdrk1"
        if document == "reverse":
            self._save_auxiliary_document(self.reverse_workspace.save)
            return
        if document == "discharge":
            self._save_auxiliary_document(self.discharge_workspace.save)
            return
        if not self.episode:
            return
        if not self._folder_field_matches(self.episode.folder):
            self._invalidate_episode()
            self.status_var.set("Папка не совпадает со сканированным эпизодом.")
            messagebox.showerror(
                "Нужно повторное сканирование",
                "Папка в поле не совпадает с папкой загруженного эпизода.",
            )
            return
        if not self._apply_form(self._current_kind):
            return
        kind = self._current_kind
        self._refresh_issues()
        review_issues = [
            issue
            for issue in current_issues(self.episode, kind)
            if issue.severity in {ReviewSeverity.BLOCKING, ReviewSeverity.WARNING}
        ]
        document_name = "МДРК 1" if kind is MdrkKind.INITIAL else "МДРК 2"
        if not confirm_generation_with_issues(
            self.root,
            review_issues,
            document_name=document_name,
        ):
            return
        default_name = self._default_output_name(kind)
        output = filedialog.asksaveasfilename(
            title="Сохранить МДРК",
            defaultextension=".docx",
            filetypes=(("Документ Word", "*.docx"),),
            initialfile=default_name,
        )
        if not output:
            return
        from functools import partial
        operation = partial(write_mdrk_docx, deepcopy(self.episode), kind, Path(output), ignore_issues=bool(review_issues))
        self._start_save_job(operation)

    def _start_save_job(self, operation):
        self._set_scanning(True)
        self.cancel_button.configure(state="disabled")
        self.status_var.set("Сохранение DOCX…")
        self._start_background_job(operation,self._finish_save,thread_name="mdrk-save")

    def _finish_save(self, created, error):
        self._set_scanning(False)
        if error is not None:
            messagebox.showerror("Не удалось создать DOCX",str(error),parent=self.root)
            self.status_var.set("Документ не сохранён; правки остаются в программе")
            return
        if created is None:return
        self._save_workspace()
        self.status_var.set(f"DOCX создан: {created}")
        window=tk.Toplevel(self.root);window.title("Документ сохранён")
        ttk.Label(window,text=str(created),wraplength=650,padding=12).pack()
        bar=ttk.Frame(window,padding=8);bar.pack(fill="x")
        ttk.Button(bar,text="Открыть документ",command=lambda:self._open_path(created)).pack(side="left",padx=4)
        ttk.Button(bar,text="Показать в папке",command=lambda:self._open_path(created.parent)).pack(side="left",padx=4)
        ttk.Button(bar,text="Закрыть",command=window.destroy).pack(side="right",padx=4)

    def _save_auxiliary_document(self, save):
        if self._scanning:return
        try:
            operation=save(defer=True)
        except Exception as exc:
            messagebox.showerror("Не удалось подготовить DOCX",str(exc),parent=self.root)
            return
        if operation is not None:self._start_save_job(operation)

    def _default_output_name(self, kind: MdrkKind) -> str:
        patient = self.episode.identity.full_name if self.episode else "пациент"
        safe_patient = re.sub(r"[^0-9A-Za-zА-Яа-яЁё-]+", " ", patient).strip() or "пациент"
        number = "1" if kind is MdrkKind.INITIAL else "2"
        return f"МДРК {number} {safe_patient}.docx"

    def _refresh_all_trees(self) -> None:
        self._refresh_sources()
        self._refresh_icf()
        self._refresh_scales()
        self._refresh_procedures()
        self._refresh_findings()
        self._refresh_issues()

    @staticmethod
    def _clear_tree(tree: ttk.Treeview) -> None:
        tree.delete(*tree.get_children())

    def _refresh_icf(self) -> None:
        self._clear_tree(self.icf_tree)
        grouped_ui = hasattr(self.icf_tree, "tag_configure")
        display_columns = ("code", "description", "role", "initial", "note")
        if grouped_ui:
            display_columns = (
                "code",
                "description",
                "q0",
                "q1",
                "q2",
                "q3",
                "q4",
                "initial",
                "responsible",
            )
            if self._current_kind is MdrkKind.FINAL:
                display_columns = (*display_columns[:-1], "final", "responsible", "dynamic")
        elif self._current_kind is MdrkKind.FINAL:
            display_columns = (
                "code",
                "description",
                "role",
                "initial",
                "final",
                "dynamic",
                "note",
            )
        self.icf_tree.configure(displaycolumns=display_columns)
        if not self.episode:
            return
        visible_domains = build_snapshot(self.episode, self._current_kind).icf_domains
        visible_by_key = {}
        for domain in visible_domains:
            visible_by_key.setdefault(domain.key, []).append(domain)
        visible_rows = []
        for index, domain in enumerate(self.episode.icf_domains):
            if visible_by_key.get(domain.key):
                visible_rows.append((index, visible_by_key[domain.key].pop(0)))
        if grouped_ui:
            grouped: dict[IcfSection, list[tuple[int, IcfDomain]]] = {
                section: [] for section in IcfSection
            }
            for index, domain in visible_rows:
                grouped[domain.section].append((index, domain))
            for section in IcfSection:
                section_id = f"section:{section.value}"
                self.icf_tree.insert(
                    "",
                    "end",
                    iid=section_id,
                    text=section.display_name,
                    open=True,
                    tags=("section",),
                    values=("",) * 11,
                )
                for index, domain in grouped[section]:
                    initial = domain.initial.display() if domain.initial else ""
                    final = (
                        domain.final.display()
                        if self._current_kind is MdrkKind.FINAL and domain.final
                        else ""
                    )
                    qualifier_marks = tuple(
                        "●"
                        if domain.initial is not None
                        and not domain.initial.facilitator
                        and domain.initial.value == value
                        else ""
                        for value in range(5)
                    )
                    responsible = domain.note or (
                        ""
                        if domain.specialist is SpecialistRole.OTHER
                        else domain.specialist.display_name
                    )
                    self.icf_tree.insert(
                        section_id,
                        "end",
                        iid=str(index),
                        values=(
                            domain.code,
                            domain.description,
                            *qualifier_marks,
                            initial,
                            final,
                            responsible,
                            domain.dynamic_marker or "",
                        ),
                    )
            personal_id = f"section:{IcfSection.PERSONAL_FACTORS.value}"
            self.icf_tree.insert(
                personal_id,
                "end",
                iid="new:icf",
                text="",
                tags=("new",),
                values=("＋ Новая строка МКФ…", "", "", "", "", "", "", "", "", "", ""),
            )
            return
        for index, domain in visible_rows:
            self.icf_tree.insert(
                "",
                "end",
                iid=str(index),
                values=(
                    domain.code,
                    domain.description,
                    (
                        ""
                        if domain.specialist is SpecialistRole.OTHER
                        else domain.specialist.display_name
                    ),
                    domain.initial.display() if domain.initial else "",
                    (
                        domain.final.display()
                        if self._current_kind is MdrkKind.FINAL and domain.final
                        else ""
                    ),
                    (
                        domain.dynamic_marker
                        if self._current_kind is MdrkKind.FINAL
                        and domain.dynamic_marker is not None
                        else ""
                    ),
                    domain.note,
                ),
            )

    def _refresh_sources(self) -> None:
        self._clear_tree(self.source_tree)
        if not self.episode:
            return
        used_paths = set(self.episode.initial_field_sources.values())
        used_paths.update(self.episode.field_sources.values())
        used_paths.update(finding.source for finding in self.episode.findings if finding.source)
        used_paths.update(
            measurement.source
            for finding in self.episode.findings
            for measurement in finding.scales
            if measurement.source
        )
        used_paths.update(domain.initial_source for domain in self.episode.icf_domains if domain.initial_source)
        used_paths.update(domain.final_source for domain in self.episode.icf_domains if domain.final_source)
        used_paths.update(procedure.source for procedure in self.episode.procedures if procedure.source)
        for index, source in enumerate(self.episode.sources):
            tags = (
                ("excluded",)
                if not self.episode.source_is_active(source)
                else (("used",) if source.path in used_paths else ())
            )
            self.source_tree.insert(
                "",
                "end",
                iid=str(index),
                tags=tags,
                values=(
                    source.role.display_name,
                    format_datetime(source.clinical_datetime),
                    source.document_type,
                    str(source.path),
                ),
            )

    def _refresh_procedures(self) -> None:
        self._clear_tree(self.procedure_tree)
        if not self.episode:
            return
        from mdrk_builder.application.procedures import select_procedures
        kind = self._selected_kind()
        self.procedure_tree.heading("count", text="Назначено" if kind is MdrkKind.INITIAL else "Выполнено")
        projected = select_procedures(self.episode.procedures, self.episode.admission_datetime, self.episode.meeting_at(kind), kind)
        for index, procedure in enumerate(projected):
            self.procedure_tree.insert(
                "",
                "end",
                iid=str(index),
                values=(
                    procedure.code,
                    procedure.name,
                    procedure.specialist,
                    "" if procedure.actual_count is None else procedure.actual_count,
                    "" if procedure.duration_minutes is None else procedure.duration_minutes,
                    procedure.frequency,
                ),
            )
        if hasattr(self.procedure_tree, "tag_configure"):
            self.procedure_tree.tag_configure("new", foreground="#1f63c5")
            self.procedure_tree.insert(
                "",
                "end",
                iid="new:procedure",
                tags=("new",),
                values=("", "＋ Новая строка…", "", "", "", ""),
            )

    def _refresh_scales(self) -> None:
        if hasattr(self, "specialist_header_var"):
            self._refresh_specialist_detail()
            return
        self._clear_tree(self.scale_tree)
        self._scale_refs = []
        if not self.episode:
            return
        selected_rows = build_snapshot(self.episode, self._current_kind).scale_rows
        selected_keys = {
            (
                measurement.specialist,
                " ".join(measurement.name.casefold().split()),
                measurement.value,
                measurement.measured_at,
                measurement.source,
            )
            for row in selected_rows
            for measurement in (row.initial, row.current)
            if measurement is not None
        }
        rendered_keys: set[tuple[object, ...]] = set()
        for finding_index, finding in enumerate(self.episode.findings):
            for scale_index, measurement in enumerate(finding.scales):
                effective_at = measurement.measured_at or finding.source_datetime
                key = (
                    measurement.specialist,
                    " ".join(measurement.name.casefold().split()),
                    measurement.value,
                    effective_at,
                    measurement.source,
                )
                if key not in selected_keys or key in rendered_keys:
                    continue
                rendered_keys.add(key)
                row_index = len(self._scale_refs)
                self._scale_refs.append((finding_index, scale_index))
                self.scale_tree.insert(
                    "",
                    "end",
                    iid=str(row_index),
                    values=(
                        measurement.specialist.display_name,
                        format_datetime(effective_at),
                        measurement.name,
                        measurement.value,
                        str(measurement.source) if measurement.source else "",
                    ),
                )

    def _refresh_findings(self) -> None:
        self._clear_tree(self.finding_tree)
        if not self.episode:
            return
        selected_ids = {
            id(finding)
            for finding in build_snapshot(self.episode, self._current_kind).findings
        }
        for index, finding in enumerate(self.episode.findings):
            if id(finding) not in selected_ids:
                continue
            conclusion = " ".join(finding.conclusion.split())
            self.finding_tree.insert(
                "",
                "end",
                iid=str(index),
                values=(
                    finding.role.display_name,
                    format_datetime(finding.source_datetime),
                    len(finding.scales),
                    conclusion,
                ),
            )
        if hasattr(self, "specialist_header_var"):
            children = self.finding_tree.get_children()
            if children and not self.finding_tree.selection():
                self.finding_tree.selection_set(children[0])
                self.finding_tree.focus(children[0])
            self._refresh_specialist_detail()

    def _refresh_issues(self) -> None:
        if not self.episode:
            self._clear_tree(self.issue_tree)
            self._issue_refs = {}
            return
        if not self._apply_form_without_messages():
            self._render_form_error()
            return
        self._clear_tree(self.issue_tree)
        self._issue_refs = {}
        for index, issue in enumerate(current_issues(self.episode, self._current_kind)):
            row_id = str(index)
            self._issue_refs[row_id] = issue
            self.issue_tree.insert(
                "",
                "end",
                iid=row_id,
                tags=(issue.severity.value,),
                values=(
                    issue.message,
                    issue.field,
                    str(issue.source) if issue.source else "",
                ),
            )

    def _selected_issue(self) -> ReviewIssue | None:
        selected = self.issue_tree.selection()
        if not selected:
            return None
        return self._issue_refs.get(selected[0])

    def _acknowledge_selected_issue(self) -> None:
        if not self.episode:
            messagebox.showwarning("Нет данных", "Сначала просканируйте папку эпизода.")
            return
        issue = self._selected_issue()
        if issue is None:
            messagebox.showwarning(
                "Проблема не выбрана",
                "Выберите любую строку в списке предупреждений.",
            )
            return
        if not self._apply_form(self._current_kind):
            return
        if issue.acknowledged:
            messagebox.showinfo(
                "Уже игнорируется",
                "Эта проблема уже игнорируется для текущих данных.",
            )
            return

        source = str(issue.source) if issue.source else "не указан"
        field = issue.field or "не указано"
        confirmed = messagebox.askyesno(
            "Игнорировать проблему",
            f"Уровень: {SEVERITY_LABELS[issue.severity]}\n"
            f"Сообщение: {issue.message}\n"
            f"Поле: {field}\n"
            f"Источник: {source}\n\n"
            "Проблема останется видимой в списке. Если она блокирующая, "
            "то перестанет мешать созданию DOCX. Остальные проверки продолжат действовать.\n\n"
            "Игнорировать её?",
        )
        if not confirmed:
            return
        try:
            acknowledge_issue(self.episode, issue, self._current_kind)
        except ValueError as exc:
            messagebox.showerror("Не удалось игнорировать", str(exc))
            return
        self._refresh_issues()
        self.status_var.set("Проблема игнорируется для текущих данных.")

    def _reset_issue_acknowledgements(self) -> None:
        if not self.episode:
            messagebox.showinfo("Игнорировать нечего", "Сначала просканируйте папку эпизода.")
            return
        if not has_issue_acknowledgements(self.episode):
            messagebox.showinfo("Игнорирований нет", "Нет проигнорированных проблем.")
            return
        if not messagebox.askyesno(
            "Сбросить игнорирование",
            "Снова учитывать все проигнорированные проблемы? "
            "Блокирующие проблемы снова запретят создание DOCX.",
        ):
            return
        clear_issue_acknowledgements(self.episode)
        self._refresh_issues()
        self.status_var.set("Игнорирование проблем сброшено.")

    def _render_form_error(self) -> None:
        self._clear_tree(self.issue_tree)
        self._issue_refs = {}
        self.issue_tree.insert(
            "",
            "end",
            iid="form_error",
            tags=(ReviewSeverity.BLOCKING.value,),
            values=(
                self._last_form_error or "Неверные данные в форме",
                "ui.form",
                "",
            ),
        )
        self.status_var.set("Проверьте поля: обнаружена блокирующая ошибка.")

    def _apply_form_without_messages(self) -> bool:
        if not self.episode:
            return False
        try:
            form = self._parsed_form_data()
        except ValueError as exc:
            self._last_form_error = str(exc)
            return False
        if form.meeting_at != self.episode.meeting_at(self._current_kind):
            self._last_form_error = MEETING_RESCAN_MESSAGE
            return False
        apply_episode_form_data(self.episode, self._current_kind, form)
        self._last_form_error = ""
        return True

    @staticmethod
    def _selected_index(tree: ttk.Treeview) -> int | None:
        selection = tree.selection()
        return int(selection[0]) if selection else None

    @staticmethod
    def _selected_indices(tree: ttk.Treeview) -> list[int]:
        return sorted(int(item_id) for item_id in tree.selection() if str(item_id).isdigit())

    def _activate_icf_item(self, item_id: str) -> bool:
        if item_id != "new:icf" or not self.episode:
            return item_id.startswith("section:")
        domain = IcfDomain("", "", SpecialistRole.OTHER)
        self.episode.icf_domains.append(domain)
        self._mark_collection_dirty("icf")
        index = len(self.episode.icf_domains) - 1
        self._refresh_icf()
        self.icf_tree.selection_set(str(index))
        self.icf_tree.see(str(index))
        if self._icf_editor is not None:
            self.root.after(1, lambda: self._icf_editor.edit(str(index), "code"))
        return True

    def _icf_editor_values(self, _item_id: str, column: str) -> tuple[str, ...] | None:
        if column == "responsible":
            return ("", *role_names())
        return None

    def _commit_icf_cell(self, item_id: str, column: str, value: str) -> None:
        if not self.episode or not item_id.isdigit():
            return
        domain = self.episode.icf_domains[int(item_id)]
        previous = deepcopy(domain)
        cleaned = value.strip()
        if column == "code":
            domain.code = cleaned
        elif column == "description":
            domain.description = cleaned
        elif column == "initial":
            domain.initial = parse_qualifier(cleaned)
        elif column == "final":
            domain.final = parse_qualifier(cleaned)
        elif column == "responsible":
            if not cleaned:
                domain.specialist = SpecialistRole.OTHER
                domain.note = ""
            elif cleaned in role_names():
                domain.specialist = role_from_name(cleaned)
                domain.note = ""
            else:
                domain.specialist = SpecialistRole.OTHER
                domain.note = cleaned
        mark_manual_changes(previous, domain)
        self._mark_collection_dirty("icf")
        self._refresh_icf()
        self._refresh_issues()
        if self.icf_tree.exists(item_id):
            self.icf_tree.selection_set(item_id)

    def _start_icf_drag(self, event: tk.Event) -> None:
        item_id = self.icf_tree.identify_row(event.y)
        self._icf_drag_item = item_id if item_id.isdigit() else None
        self._icf_drag_origin = (event.x, event.y) if self._icf_drag_item else None

    def _finish_icf_pointer(self, event: tk.Event) -> None:
        source_id = self._icf_drag_item
        origin = self._icf_drag_origin
        self._icf_drag_item = None
        self._icf_drag_origin = None
        target_id = self.icf_tree.identify_row(event.y)
        column = self.icf_tree.identify_column(event.x)
        if source_id and origin and abs(event.x - origin[0]) + abs(event.y - origin[1]) >= 6:
            self._move_icf_domain(int(source_id), target_id)
            return
        if not target_id.isdigit() or not column.startswith("#"):
            return
        try:
            column_name = tuple(self.icf_tree.cget("columns"))[int(column[1:]) - 1]
        except (IndexError, TypeError, ValueError):
            return
        if str(column_name) in {"q0", "q1", "q2", "q3", "q4"} and self.episode:
            self.episode.icf_domains[int(target_id)].initial = parse_qualifier(str(column_name)[1:])
            self.episode.icf_domains[int(target_id)].manual_fields.add("initial")
            self._mark_collection_dirty("icf")
            self._refresh_icf()
            self._refresh_issues()

    def _move_icf_domain(self, source_index: int, target_id: str) -> None:
        if not self.episode or source_index >= len(self.episode.icf_domains):
            return
        if target_id.startswith("section:"):
            section = IcfSection(target_id.split(":", 1)[1])
            target_index = None
        elif target_id.isdigit():
            target_index = int(target_id)
            section = self.episode.icf_domains[target_index].section
        else:
            return
        previous_section = self.episode.icf_domains[source_index].section
        new_index = move_icf_domain(
            self.episode.icf_domains,
            source_index,
            section,
            before_index=target_index,
        )
        if previous_section != section:
            self.episode.icf_domains[new_index].manual_fields.add("section_override")
        self._mark_collection_dirty("icf")
        self._refresh_icf()
        self.icf_tree.selection_set(str(new_index))
        self.icf_tree.see(str(new_index))

    def _activate_procedure_item(self, item_id: str) -> bool:
        if item_id != "new:procedure" or not self.episode:
            return False
        self.episode.procedures.append(Procedure("", "", None))
        self._mark_collection_dirty("procedures")
        index = len(self.episode.procedures) - 1
        self._refresh_procedures()
        self.procedure_tree.selection_set(str(index))
        if self._procedure_editor is not None:
            self.root.after(1, lambda: self._procedure_editor.edit(str(index), "name"))
        return True

    def _commit_procedure_cell(self, item_id: str, column: str, value: str) -> None:
        if not self.episode or not item_id.isdigit():
            return
        procedure = self.episode.procedures[int(item_id)]
        previous = deepcopy(procedure)
        cleaned = value.strip()
        if column == "code":
            procedure.code = cleaned
        elif column == "name":
            procedure.name = cleaned
        elif column == "specialist":
            procedure.specialist = cleaned
        elif column in {"count", "duration"}:
            if cleaned and (not cleaned.isdigit() or int(cleaned) < 0):
                raise ValueError("Введите неотрицательное целое число")
            parsed = int(cleaned) if cleaned else None
            if column == "count":
                if self._selected_kind() is MdrkKind.INITIAL:
                    procedure.planned_count = parsed
                else:
                    procedure.actual_count = parsed
            else:
                procedure.duration_minutes = parsed
        elif column == "frequency":
            if self._selected_kind() is MdrkKind.INITIAL:
                procedure.planned_frequency = cleaned
            else:
                procedure.frequency = cleaned
        mark_manual_changes(previous, procedure)
        self._mark_collection_dirty("procedures")
        self._refresh_procedures()
        self._refresh_issues()

    def _on_specialist_selected(self, _event: tk.Event | None = None) -> None:
        self._commit_specialist_conclusion()
        self._refresh_specialist_detail()

    def _selected_specialist_finding(self) -> SpecialistFinding | None:
        if not self.episode:
            return None
        selected = self.finding_tree.selection()
        if not selected or not selected[0].isdigit():
            return None
        index = int(selected[0])
        return self.episode.findings[index] if index < len(self.episode.findings) else None

    def _refresh_specialist_detail(self) -> None:
        self._clear_tree(self.scale_tree)
        self._scale_pair_refs = {}
        finding = self._selected_specialist_finding()
        self._displayed_specialist_finding = finding
        if finding is None or not self.episode:
            self.specialist_header_var.set("")
            self._loading_specialist = True
            self.specialist_conclusion.delete("1.0", "end")
            self.specialist_conclusion.edit_reset()
            self._loading_specialist = False
            self.specialist_source_button.configure(state="disabled")
            self.conclusion_source_button.configure(state="disabled")
            return
        self.specialist_header_var.set(
            f"{finding.role.display_name} · {format_datetime(finding.source_datetime)}".rstrip(" ·")
        )
        source_state = "normal" if finding.source else "disabled"
        self.specialist_source_button.configure(state=source_state)
        self.conclusion_source_button.configure(state=source_state)
        self._loading_specialist = True
        self.specialist_conclusion.delete("1.0", "end")
        self.specialist_conclusion.insert("1.0", finding.conclusion)
        self.specialist_conclusion.edit_reset()
        self._loading_specialist = False
        rows = [
            row
            for row in build_snapshot(self.episode, self._current_kind).scale_rows
            if row.role is finding.role
        ]
        for index, row in enumerate(rows):
            item_id = f"scale:{index}"
            self._scale_pair_refs[item_id] = row
            self.scale_tree.insert(
                "",
                "end",
                iid=item_id,
                values=(
                    row.name,
                    row.initial.value if row.initial else "",
                    row.current.value if self._current_kind is MdrkKind.FINAL and row.current else "",
                ),
            )
        self.scale_tree.insert(
            "",
            "end",
            iid="new:scale",
            tags=("new",),
            values=("＋ Новая шкала…", "", ""),
        )

    def _activate_scale_item(self, item_id: str) -> bool:
        if item_id != "new:scale" or not self.episode:
            return False
        finding = self._selected_specialist_finding()
        if finding is None:
            return True
        measurement = ScaleMeasurement(
            name="",
            value="",
            measured_at=self.episode.initial_meeting_at,
            specialist=finding.role,
        )
        finding.scales.append(measurement)
        self._mark_collection_dirty("findings")
        self._refresh_specialist_detail()
        blank_item = next(
            (
                item_id
                for item_id, row in self._scale_pair_refs.items()
                if getattr(row, "initial", None) is measurement
                or getattr(row, "current", None) is measurement
            ),
            None,
        )
        if blank_item and self._scale_editor is not None:
            self.scale_tree.selection_set(blank_item)
            self.root.after(1, lambda: self._scale_editor.edit(blank_item, "name"))
        return True

    def _live_scale_measurements(self, displayed):
        return [measurement for finding in self.episode.findings for measurement in finding.scales
                if measurement is displayed or (
                    measurement.source == displayed.source
                    and measurement.specialist == displayed.specialist
                    and measurement.name == displayed.name
                    and measurement.value == displayed.value
                    and (measurement.measured_at or finding.source_datetime) == displayed.measured_at)]

    def _edit_scale_measurement(self, displayed, field: str, value: str) -> None:
        for measurement in self._live_scale_measurements(displayed):
            if getattr(measurement, field) != value:
                previous = deepcopy(measurement)
                setattr(measurement, field, value)
                mark_manual_changes(previous, measurement)

    def _commit_scale_cell(self, item_id: str, column: str, value: str) -> None:
        if not self.episode or item_id not in self._scale_pair_refs:
            return
        row = self._scale_pair_refs[item_id]
        cleaned = value.strip()
        finding = self._selected_specialist_finding()
        if finding is None:
            return
        if column == "name":
            if not cleaned:
                raise ValueError("Введите название шкалы")
            if row.initial:
                self._edit_scale_measurement(row.initial, "name", cleaned)
            if row.current and row.current is not row.initial:
                self._edit_scale_measurement(row.current, "name", cleaned)
        elif column == "initial":
            if row.initial:
                self._edit_scale_measurement(row.initial, "value", cleaned)
            else:
                finding.scales.append(
                    ScaleMeasurement(row.name, cleaned, self.episode.initial_meeting_at, finding.role)
                )
        elif column == "final":
            if self._current_kind is not MdrkKind.FINAL:
                return
            if row.current and row.current is not row.initial:
                self._edit_scale_measurement(row.current, "value", cleaned)
            else:
                finding.scales.append(
                    ScaleMeasurement(row.name, cleaned, self.episode.final_meeting_at, finding.role)
                )
        self._mark_collection_dirty("findings")
        self._refresh_specialist_detail()
        self._refresh_issues()

    def _commit_specialist_conclusion(self, _event: tk.Event | None = None) -> None:
        if self._loading_specialist:
            return
        finding = getattr(self, "_displayed_specialist_finding", None)
        if finding is None or self.episode is None or not any(
            current is finding for current in self.episode.findings
        ):
            return
        value = self.specialist_conclusion.get("1.0", "end-1c")
        if value != finding.conclusion:
            finding.conclusion = value
            finding.manual_fields.add("conclusion")
            self._mark_collection_dirty("findings")
            self._refresh_issues()

    def _add_icf(self) -> None:
        if not self.episode:
            return
        dialog = IcfDomainDialog(self.root, kind=self._current_kind)
        if dialog.result:
            self.episode.icf_domains.append(dialog.result)
            self._mark_collection_dirty("icf")
            self._refresh_icf()
            self._refresh_issues()

    def _edit_icf(self) -> None:
        if not self.episode or (index := self._selected_index(self.icf_tree)) is None:
            return
        dialog = IcfDomainDialog(
            self.root,
            self.episode.icf_domains[index],
            kind=self._current_kind,
        )
        if dialog.result:
            self.episode.icf_domains[index] = dialog.result
            self._mark_collection_dirty("icf")
            self._refresh_icf()
            self._refresh_issues()

    def _delete_icf(self) -> None:
        if not self.episode or not (indices := self._selected_indices(self.icf_tree)):
            return
        noun = (
            "выбранный домен МКФ"
            if len(indices) == 1
            else f"выбранные домены МКФ ({len(indices)})"
        )
        if messagebox.askyesno("Удалить домен", f"Удалить {noun}?"):
            for index in reversed(indices):
                self.episode.icf_domains.pop(index)
            self._mark_collection_dirty("icf")
            self._refresh_icf()
            self._refresh_issues()

    def _add_procedure(self) -> None:
        if not self.episode:
            return
        dialog = ProcedureDialog(self.root, planned=self._selected_kind() is MdrkKind.INITIAL)
        if dialog.result:
            self.episode.procedures.append(dialog.result)
            self._mark_collection_dirty("procedures")
            self._refresh_procedures()
            self._refresh_issues()

    def _add_scale(self) -> None:
        if not self.episode:
            return
        dialog = ScaleDialog(self.root)
        if dialog.result:
            self._append_scale(dialog.result)
            self._mark_collection_dirty("findings")
            self._refresh_scales()
            self._refresh_findings()
            self._refresh_issues()

    def _edit_scale(self) -> None:
        if not self.episode or (row_index := self._selected_index(self.scale_tree)) is None:
            return
        finding_index, scale_index = self._scale_refs[row_index]
        measurement = self.episode.findings[finding_index].scales[scale_index]
        dialog = ScaleDialog(self.root, measurement)
        if not dialog.result:
            return
        if dialog.result.specialist is self.episode.findings[finding_index].role:
            self.episode.findings[finding_index].scales[scale_index] = dialog.result
        else:
            self.episode.findings[finding_index].scales.pop(scale_index)
            self._append_scale(dialog.result)
        self._refresh_scales()
        self._mark_collection_dirty("findings")
        self._refresh_findings()
        self._refresh_issues()

    def _delete_scale(self) -> None:
        if hasattr(self, "specialist_header_var"):
            if not self.episode:
                return
            selected = [
                item_id
                for item_id in self.scale_tree.selection()
                if item_id in self._scale_pair_refs
            ]
            measurements = {
                id(measurement)
                for item_id in selected
                for displayed in (
                    getattr(self._scale_pair_refs[item_id], "initial", None),
                    getattr(self._scale_pair_refs[item_id], "current", None),
                )
                if displayed is not None
                for measurement in self._live_scale_measurements(displayed)
            }
            if not measurements:
                return
            if messagebox.askyesno("Удалить шкалу", "Удалить выбранные значения шкал?"):
                for finding in self.episode.findings:
                    finding.scales[:] = [
                        measurement
                        for measurement in finding.scales
                        if id(measurement) not in measurements
                    ]
                self._mark_collection_dirty("findings")
                self._refresh_specialist_detail()
                self._refresh_issues()
            return
        if not self.episode or not (row_indices := self._selected_indices(self.scale_tree)):
            return
        noun = (
            "выбранное измерение шкалы"
            if len(row_indices) == 1
            else f"выбранные измерения шкал ({len(row_indices)})"
        )
        if messagebox.askyesno("Удалить измерение", f"Удалить {noun}?"):
            references = sorted(
                (self._scale_refs[row_index] for row_index in row_indices),
                reverse=True,
            )
            for finding_index, scale_index in references:
                self.episode.findings[finding_index].scales.pop(scale_index)
            self._mark_collection_dirty("findings")
            self._refresh_scales()
            self._refresh_findings()
            self._refresh_issues()

    def _append_scale(self, measurement: ScaleMeasurement) -> None:
        if not self.episode:
            return
        finding = next(
            (item for item in reversed(self.episode.findings) if item.role is measurement.specialist),
            None,
        )
        if finding is None:
            finding = SpecialistFinding(role=measurement.specialist)
            self.episode.findings.append(finding)
        finding.scales.append(measurement)

    def _edit_procedure(self) -> None:
        if not self.episode or (index := self._selected_index(self.procedure_tree)) is None:
            return
        dialog = ProcedureDialog(self.root, self.episode.procedures[index], planned=self._selected_kind() is MdrkKind.INITIAL)
        if dialog.result:
            self.episode.procedures[index] = dialog.result
            self._mark_collection_dirty("procedures")
            self._refresh_procedures()
            self._refresh_issues()

    def _delete_procedure(self) -> None:
        if not self.episode or not (indices := self._selected_indices(self.procedure_tree)):
            return
        noun = (
            "выбранную процедуру"
            if len(indices) == 1
            else f"выбранные процедуры ({len(indices)})"
        )
        if messagebox.askyesno("Удалить процедуру", f"Удалить {noun}?"):
            for index in reversed(indices):
                self.episode.procedures.pop(index)
            self._mark_collection_dirty("procedures")
            self._refresh_procedures()
            self._refresh_issues()

    def _add_finding(self) -> None:
        if not self.episode:
            return
        dialog = FindingDialog(self.root)
        if dialog.result:
            self.episode.findings.append(dialog.result)
            self._mark_collection_dirty("findings")
            self._refresh_findings()
            self._refresh_scales()
            self._refresh_issues()

    def _edit_finding(self) -> None:
        if not self.episode or (index := self._selected_index(self.finding_tree)) is None:
            return
        dialog = FindingDialog(self.root, self.episode.findings[index])
        if dialog.result:
            for measurement in dialog.result.scales:
                measurement.specialist = dialog.result.role
            self.episode.findings[index] = dialog.result
            self._mark_collection_dirty("findings")
            self._refresh_findings()
            self._refresh_scales()
            self._refresh_issues()

    def _delete_finding(self) -> None:
        if not self.episode or not (indices := self._selected_indices(self.finding_tree)):
            return
        scale_count = sum(len(self.episode.findings[index].scales) for index in indices)
        question = (
            "Удалить выбранное заключение специалиста?"
            if len(indices) == 1
            else f"Удалить выбранные заключения специалистов ({len(indices)})?"
        )
        if scale_count:
            question += (
                f"\n\nВместе с ним будут удалены измерения шкал: {scale_count}."
            )
        if messagebox.askyesno("Удалить заключение", question):
            for index in reversed(indices):
                self.episode.findings.pop(index)
            self._mark_collection_dirty("findings")
            self._refresh_findings()
            self._refresh_scales()
            self._refresh_issues()

    def _show_about(self) -> None:
        messagebox.showinfo(
            "О программе",
            about_text(),
        )

    def _show_feedback(self) -> None:
        initial = None
        while True:
            dialog = FeedbackDialog(self.root, initial=initial)
            submission = dialog.result
            if submission is None:
                return
            try:
                result = save_feedback(submission, app_version=__version__)
            except (FeedbackStorageError, ValueError):
                retry = messagebox.askretrycancel(
                    "Не удалось сохранить отзыв",
                    "Папка программы недоступна для записи. "
                    "Текст не был сохранён. Повторить?",
                    parent=self.root,
                )
                if not retry:
                    return
                initial = submission
                continue

            if result.queued:
                messagebox.showwarning(
                    "Отзыв сохранён",
                    "Файл issues.txt сейчас занят. Отзыв не потерян: он сохранён "
                    "рядом с программой и будет добавлен в issues.txt при следующей отправке.",
                    parent=self.root,
                )
            else:
                messagebox.showinfo(
                    "Спасибо",
                    "Отзыв добавлен в issues.txt рядом с программой.",
                    parent=self.root,
                )
            return

    def _on_close(self) -> None:
        if self._scanning:
            messagebox.showwarning(
                "Сканирование выполняется",
                "Дождитесь завершения сканирования: сейчас закрытие может оставить Microsoft Word открытым.",
            )
            return
        if self._confirm_leave():
            self.root.destroy()


def _generate_smoke_document(directory: Path) -> Path:
    _write_smoke_report("phase=template")
    template = canonical_template_path()
    if not template.is_file():
        raise FileNotFoundError(f"Канонический шаблон не найден: {template}")

    episode = Episode(folder=directory)
    episode.identity.full_name = "АЛЬФА БЕТА ГАММА"
    episode.identity.medical_record_number = "SMOKE-1"
    episode.admission_datetime = datetime(2026, 1, 1, 9, 0)
    episode.initial_meeting_at = datetime(2026, 1, 2, 8, 0)
    episode.initial_sections.clinical_diagnosis = "АБСТРАКТНЫЙ МАРКЕР"
    episode.sections.clinical_diagnosis = "АБСТРАКТНЫЙ МАРКЕР"
    episode.sources.append(
        SourceDocument(directory / "smoke-source.docx", role=SpecialistRole.NEUROLOGIST)
    )
    _write_smoke_report("phase=write_docx")
    output = write_mdrk_docx(
        episode,
        MdrkKind.INITIAL,
        directory / "smoke-output.docx",
    )
    if not output.is_file() or output.stat().st_size == 0:
        raise RuntimeError("Тестовый DOCX не был создан")
    _write_smoke_report("phase=reopen_docx")
    reopened = Document(output)
    if not reopened.paragraphs:
        raise RuntimeError("Тестовый DOCX не содержит ожидаемых абзацев")

    _write_smoke_report("phase=write_discharge_docx")
    discharge = DischargeSummaryDraft(
        folder=directory,
        identity=PatientIdentity(
            full_name=episode.identity.full_name,
            birth_date=episode.identity.birth_date,
            sex=episode.identity.sex,
            medical_record_number=episode.identity.medical_record_number,
        ),
        admission_datetime=episode.admission_datetime,
        source_paths=tuple(source.path for source in episode.sources),
        header_text="Сведения о пациенте: АБСТРАКТНЫЙ МАРКЕР",
        clinical_diagnosis="АБСТРАКТНЫЙ МАРКЕР",
        radiation_exposure="0 мЗв",
        recommendations="ПРОВЕРИТЬ ПЕРЕД ПОДПИСАНИЕМ",
    )
    discharge_output = write_discharge_summary_docx(
        discharge,
        directory / "smoke-discharge-output.docx",
    )
    _write_smoke_report("phase=reopen_discharge_docx")
    discharge_document = Document(discharge_output)
    if not discharge_document.tables or not discharge_document.paragraphs:
        raise RuntimeError("Тестовый выписной эпикриз не содержит ожидаемой разметки")
    return output


def smoke_test(*, include_ui: bool = False) -> int:
    with TemporaryDirectory(prefix="mdrk-builder-smoke-") as temporary:
        temporary_path = Path(temporary)
        _generate_smoke_document(temporary_path)

        if include_ui:
            _write_smoke_report("phase=tk_init")
            root = tk.Tk()
            try:
                root.withdraw()
                application = MdrkBuilderApp(root)
                smoke_discharge = DischargeSummaryDraft(
                    folder=temporary_path,
                )
                application.discharge_workspace.load(smoke_discharge)
                application.discharge_draft = smoke_discharge
                _write_smoke_report("phase=app_constructed")
                _assert_consistent_geometry_managers(root)
                root.update_idletasks()
                root.update()
                _write_smoke_report("phase=idle_updated")
            finally:
                root.destroy()
                _write_smoke_report("phase=ui_destroyed")
    return 0


def _assert_consistent_geometry_managers(widget: tk.Misc) -> None:
    if widget.pack_slaves() and widget.grid_slaves():
        raise RuntimeError(
            f"В одном контейнере смешаны pack и grid: {widget.winfo_pathname(widget.winfo_id())}"
        )
    for child in widget.winfo_children():
        _assert_consistent_geometry_managers(child)


def _write_smoke_report(message: str, *, reset: bool = False) -> None:
    report_path = os.environ.get("MDRK_BUILDER_SMOKE_REPORT")
    if not report_path:
        return
    mode = "w" if reset else "a"
    try:
        with Path(report_path).open(mode, encoding="utf-8") as report:
            report.write(f"{message}\n")
    except OSError:
        return


def _run_smoke(*, include_ui: bool) -> int:
    _write_smoke_report("phase=start", reset=True)
    try:
        result = smoke_test(include_ui=include_ui)
        _write_smoke_report("status=ok")
        return result
    except Exception:
        try:
            _write_smoke_report(traceback.format_exc())
        except OSError:
            pass
        return 1


def _write_crash_report(error_text: str) -> Path | None:
    base = Path(os.environ.get("LOCALAPPDATA", gettempdir()))
    report = base / "MDRK Builder" / "logs" / "startup-error.log"
    try:
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text(error_text, encoding="utf-8")
    except OSError:
        return None
    return report


def _run_gui() -> int:
    root: tk.Tk | None = None
    try:
        root = tk.Tk()
        MdrkBuilderApp(root)
        root.mainloop()
        return 0
    except Exception:
        report = _write_crash_report(traceback.format_exc())
        message = "Программа не смогла запуститься."
        if report is not None:
            message += f"\n\nТехнический отчёт сохранён:\n{report}"
        if root is not None:
            try:
                messagebox.showerror("Ошибка запуска МДРК Builder", message, parent=root)
            except Exception:
                pass
            try:
                root.destroy()
            except Exception:
                pass
        return 1


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if "--smoke-test-ui" in arguments:
        return _run_smoke(include_ui=True)
    if "--smoke-test" in arguments:
        return _run_smoke(include_ui=False)
    return _run_gui()


if __name__ == "__main__":
    raise SystemExit(main())
