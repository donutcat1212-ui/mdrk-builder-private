from __future__ import annotations

import re
import tkinter as tk
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from tkinter import filedialog, messagebox, scrolledtext, ttk

from mdrk_builder.domain import DischargeSummaryDraft, ReviewIssue, ReviewSeverity
from mdrk_builder.infrastructure.discharge_summary_writer import (
    write_discharge_summary_docx,
)
from mdrk_builder.ui.episode_adapter import (
    format_date,
    format_datetime,
)
from mdrk_builder.ui.generation_review_dialog import confirm_generation_with_issues


@dataclass(frozen=True, slots=True)
class DischargeTextField:
    name: str
    label: str
    height: int = 4


DISCHARGE_FIELD_GROUPS: tuple[tuple[str, tuple[DischargeTextField, ...]], ...] = (
    (
        "Диагноз",
        (
            DischargeTextField("clinical_diagnosis", "Заключительный клинический диагноз", 7),
        ),
    ),
    (
        "Состояние при поступлении",
        (
            DischargeTextField("complaints", "Жалобы"),
            DischargeTextField("disease_history", "Анамнез заболевания", 6),
            DischargeTextField("life_history", "Анамнез жизни", 5),
            DischargeTextField("provided_documents", "Предоставленные документы", 3),
            DischargeTextField("physical_exam", "Физикальное обследование", 6),
            DischargeTextField("neurological_status", "Неврологический статус", 8),
            DischargeTextField("local_status", "Локальный статус", 4),
        ),
    ),
    (
        "Обследования и консультации",
        (
            DischargeTextField("laboratory_results", "Лабораторные исследования", 8),
            DischargeTextField("instrumental_results", "Инструментальные исследования", 8),
            DischargeTextField("other_consultations", "Консультации узких специалистов", 7),
        ),
    ),
    (
        "Лечение",
        (
            DischargeTextField("medications", "Лекарственные препараты — место для ручного заполнения", 4),
            DischargeTextField("movement_regimen", "Двигательный режим", 3),
            DischargeTextField("diet", "Диета", 3),
            DischargeTextField("transfusions", "Трансфузии — место для ручного заполнения", 3),
            DischargeTextField("operations", "Оперативные вмешательства", 3),
            DischargeTextField("additional_information", "Дополнительные сведения", 8),
        ),
    ),
    (
        "Состояние и результат при выписке",
        (
            DischargeTextField("discharge_condition", "Состояние при выписке", 8),
            DischargeTextField("discharge_neurological_status", "Неврологический статус при выписке", 7),
            DischargeTextField("risks", "Факторы риска", 4),
            DischargeTextField("limitations", "Ограничивающие факторы", 4),
            DischargeTextField("rehabilitation_potential", "Реабилитационный потенциал", 4),
            DischargeTextField("goal_result", "Результат достижения цели", 4),
        ),
    ),
    (
        "Заключение",
        (
            DischargeTextField("work_capacity", "Трудоспособность", 5),
            DischargeTextField("radiation_exposure", "Лучевая нагрузка", 3),
            DischargeTextField("recommendations", "Рекомендации", 14),
            DischargeTextField("signatures", "Подписи", 5),
        ),
    ),
)


DISCHARGE_TEXT_FIELDS = tuple(
    field for _group_name, group_fields in DISCHARGE_FIELD_GROUPS for field in group_fields
)


def blocking_discharge_issues(draft: DischargeSummaryDraft) -> tuple[ReviewIssue, ...]:
    return draft.blocking_issues()


def warning_discharge_issues(draft: DischargeSummaryDraft) -> tuple[ReviewIssue, ...]:
    return tuple(
        issue for issue in draft.issues if issue.severity is ReviewSeverity.WARNING
    )


def apply_discharge_form(
    draft: DischargeSummaryDraft,
    text_values: Mapping[str, str],
) -> None:
    for field in DISCHARGE_TEXT_FIELDS:
        if field.name not in text_values:
            continue
        value = text_values[field.name]
        if value == getattr(draft, field.name):
            continue
        setattr(draft, field.name, value)
        draft.field_sources.pop(field.name, None)


class DischargeSummaryDialog(tk.Toplevel):
    def __init__(self, parent: tk.Misc, draft: DischargeSummaryDraft) -> None:
        super().__init__(parent)
        self.draft = draft
        self.title("Выписной эпикриз")
        self.geometry("1120x800")
        self.minsize(880, 620)
        self.transient(parent)
        self.protocol("WM_DELETE_WINDOW", self.destroy)

        self._text_widgets: dict[str, tk.Text] = {}
        self._source_labels: dict[str, ttk.Label] = {}
        self._status = tk.StringVar(value=self._status_text())
        self._build()
        self.grab_set()

    def _build(self) -> None:
        shell = ttk.Frame(self, padding=10)
        shell.pack(fill="both", expand=True)

        heading = ttk.Frame(shell)
        heading.pack(fill="x")
        ttk.Label(
            heading,
            text="Выписной эпикриз",
            font=("TkDefaultFont", 15, "bold"),
        ).pack(side="left")
        ttk.Label(heading, textvariable=self._status).pack(side="right")
        ttk.Label(
            shell,
            text=(
                "Проверьте автоматически перенесённые данные. ФИО, номер карты и даты ниже "
                "автоматически формируют шапку и колонтитул и не редактируются здесь; "
                "текстовые блоки редактируются здесь. МКФ, шкалы и программа "
                "переносятся структурно и окончательно проверяются в созданном DOCX."
            ),
            foreground="#555555",
            wraplength=1040,
            justify="left",
        ).pack(fill="x", pady=(3, 8))

        issue_bar = ttk.Frame(shell)
        issue_bar.pack(fill="x", pady=(0, 8))
        blockers = blocking_discharge_issues(self.draft)
        warnings = warning_discharge_issues(self.draft)
        issue_color = "#A40000" if blockers else "#9A5B00" if warnings else "#246A24"
        ttk.Label(
            issue_bar,
            text=f"Блокирующие: {len(blockers)}; предупреждения: {len(warnings)}",
            foreground=issue_color,
        ).pack(side="left")
        ttk.Button(
            issue_bar,
            text="Проблемы и источники…",
            command=self._show_issues_and_sources,
        ).pack(side="left", padx=(8, 0))

        content = ttk.Frame(shell)
        content.pack(fill="both", expand=True)
        canvas = tk.Canvas(content, highlightthickness=0)
        vertical = ttk.Scrollbar(content, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=vertical.set)
        canvas.pack(side="left", fill="both", expand=True)
        vertical.pack(side="right", fill="y")

        form = ttk.Frame(canvas, padding=(2, 2, 8, 6))
        form_window = canvas.create_window((0, 0), window=form, anchor="nw")
        form.bind(
            "<Configure>",
            lambda _event: canvas.configure(scrollregion=canvas.bbox("all")),
        )
        canvas.bind(
            "<Configure>",
            lambda event: canvas.itemconfigure(form_window, width=event.width),
        )

        self._build_identity(form)
        for group_name, group_fields in DISCHARGE_FIELD_GROUPS:
            group = ttk.LabelFrame(form, text=group_name, padding=8)
            group.pack(fill="x", pady=(8, 0))
            for field in group_fields:
                self._add_text_field(group, field)

        controls = ttk.Frame(shell)
        controls.pack(fill="x", pady=(9, 0))
        ttk.Button(controls, text="Закрыть", command=self.destroy).pack(side="right")
        ttk.Button(
            controls,
            text="Создать DOCX…",
            command=self._generate,
        ).pack(side="right", padx=(0, 6))

    def _build_identity(self, parent: ttk.Frame) -> None:
        identity_frame = ttk.LabelFrame(
            parent,
            text="Пациент и даты — только для проверки",
            padding=8,
        )
        identity_frame.pack(fill="x")
        identity = self.draft.identity
        fields = (
            ("ФИО", identity.full_name),
            ("Номер медкарты", identity.medical_record_number),
            ("Дата рождения", format_date(identity.birth_date)),
            ("Пол", identity.sex),
            ("Поступление", format_datetime(self.draft.admission_datetime)),
            ("Выписка", format_datetime(self.draft.discharge_datetime)),
        )
        for index, (label, value) in enumerate(fields):
            row, pair = divmod(index, 2)
            column = pair * 2
            ttk.Label(identity_frame, text=label).grid(
                row=row, column=column, sticky="w", padx=(0, 6), pady=4
            )
            ttk.Label(
                identity_frame,
                text=value or "—",
                anchor="w",
                relief="sunken",
                padding=(5, 3),
            ).grid(
                row=row,
                column=column + 1,
                sticky="ew",
                padx=(0, 14),
                pady=4,
            )
        identity_frame.columnconfigure(1, weight=1)
        identity_frame.columnconfigure(3, weight=1)

        provenance = []
        if self.draft.discharge_source is not None:
            provenance.append(f"выписной эпикриз: {self.draft.discharge_source}")
        if self.draft.primary_neurologist_source is not None:
            provenance.append(
                f"первичный осмотр невролога: {self.draft.primary_neurologist_source}"
            )
        ttk.Label(
            identity_frame,
            text="Источники: " + ("; ".join(provenance) if provenance else "не определены"),
            foreground="#555555",
            wraplength=1000,
            justify="left",
        ).grid(row=3, column=0, columnspan=4, sticky="w", pady=(5, 0))

    def _add_text_field(
        self,
        parent: ttk.LabelFrame,
        field: DischargeTextField,
    ) -> None:
        label_row = ttk.Frame(parent)
        label_row.pack(fill="x", pady=(5, 2))
        ttk.Label(label_row, text=field.label).pack(side="left")
        source = self.draft.field_sources.get(field.name)
        if source is not None:
            source_label = ttk.Label(
                label_row,
                text=f"Источник: {source.name}",
                foreground="#5A5A5A",
            )
            source_label.pack(side="right")
            self._source_labels[field.name] = source_label
        elif field.name in {"medications", "transfusions"}:
            ttk.Label(
                label_row,
                text="Оставлено пустым для ручного заполнения",
                foreground="#6A5A00",
            ).pack(side="right")

        widget = scrolledtext.ScrolledText(
            parent,
            height=field.height,
            wrap="word",
            undo=True,
            maxundo=-1,
        )
        widget.pack(fill="x")
        widget.insert("1.0", getattr(self.draft, field.name))
        self._text_widgets[field.name] = widget

    def _status_text(self) -> str:
        source_count = len(self.draft.immutable_sources())
        scale_count = len(self.draft.admission_scale_rows) + len(
            self.draft.discharge_scale_rows
        )
        return (
            f"Источники: {source_count}; замечания: {len(self.draft.issues)}; "
            f"МКФ: {len(self.draft.icf_domains)}; шкалы: {scale_count}; "
            f"процедуры: {len(self.draft.completed_procedures)}"
        )

    def _show_issues_and_sources(self) -> None:
        dialog = tk.Toplevel(self)
        dialog.title("Проблемы и происхождение данных")
        dialog.geometry("950x560")
        dialog.minsize(720, 420)
        dialog.transient(self)

        shell = ttk.Frame(dialog, padding=10)
        shell.pack(fill="both", expand=True)
        notebook = ttk.Notebook(shell)
        notebook.pack(fill="both", expand=True)

        issues_tab = ttk.Frame(notebook, padding=6)
        notebook.add(issues_tab, text=f"Проблемы ({len(self.draft.issues)})")
        issue_columns = ("severity", "message", "field", "source")
        issue_tree = ttk.Treeview(issues_tab, columns=issue_columns, show="headings")
        for name, label, width in (
            ("severity", "Уровень", 110),
            ("message", "Сообщение", 430),
            ("field", "Поле", 150),
            ("source", "Источник", 220),
        ):
            issue_tree.heading(name, text=label)
            issue_tree.column(name, width=width, minwidth=70, anchor="w")
        issue_scroll = ttk.Scrollbar(issues_tab, orient="vertical", command=issue_tree.yview)
        issue_tree.configure(yscrollcommand=issue_scroll.set)
        issue_tree.pack(side="left", fill="both", expand=True)
        issue_scroll.pack(side="right", fill="y")
        severity_labels = {
            ReviewSeverity.BLOCKING: "БЛОКИРУЕТ",
            ReviewSeverity.WARNING: "ПРЕДУПРЕЖДЕНИЕ",
            ReviewSeverity.INFO: "ИНФО",
        }
        for index, issue in enumerate(self.draft.issues):
            issue_tree.insert(
                "",
                "end",
                iid=str(index),
                values=(
                    severity_labels[issue.severity],
                    issue.message,
                    issue.field,
                    str(issue.source) if issue.source else "",
                ),
            )

        sources_tab = ttk.Frame(notebook, padding=6)
        notebook.add(sources_tab, text="Происхождение полей")
        source_columns = ("field", "source")
        source_tree = ttk.Treeview(sources_tab, columns=source_columns, show="headings")
        source_tree.heading("field", text="Поле")
        source_tree.heading("source", text="Источник")
        source_tree.column("field", width=250, minwidth=120, anchor="w")
        source_tree.column("source", width=650, minwidth=250, anchor="w")
        source_scroll = ttk.Scrollbar(
            sources_tab, orient="vertical", command=source_tree.yview
        )
        source_tree.configure(yscrollcommand=source_scroll.set)
        source_tree.pack(side="left", fill="both", expand=True)
        source_scroll.pack(side="right", fill="y")
        labels = {field.name: field.label for field in DISCHARGE_TEXT_FIELDS}
        for index, (name, source) in enumerate(sorted(self.draft.field_sources.items())):
            source_tree.insert(
                "",
                "end",
                iid=str(index),
                values=(labels.get(name, name), str(source)),
            )

        ttk.Button(shell, text="Закрыть", command=dialog.destroy).pack(
            anchor="e", pady=(8, 0)
        )

    def _apply_form(self) -> bool:
        apply_discharge_form(
            self.draft,
            {
                name: widget.get("1.0", "end-1c")
                for name, widget in self._text_widgets.items()
            },
        )
        for name, label in self._source_labels.items():
            if name not in self.draft.field_sources:
                label.configure(
                    text="Изменено вручную — источник снят",
                    foreground="#6A5A00",
                )
        return True

    def _generate(self) -> None:
        if not self._apply_form():
            return

        review_issues = tuple(
            issue
            for issue in self.draft.issues
            if issue.severity in {ReviewSeverity.BLOCKING, ReviewSeverity.WARNING}
        )
        if not confirm_generation_with_issues(
            self,
            review_issues,
            document_name="Выписной эпикриз",
        ):
            return

        patient = re.sub(
            r"[^0-9A-Za-zА-Яа-яЁё_-]+", "_", self.draft.identity.full_name
        ).strip("_")
        output = filedialog.asksaveasfilename(
            parent=self,
            title="Сохранить выписной эпикриз",
            defaultextension=".docx",
            filetypes=(("Документ Word", "*.docx"),),
            initialfile=f"Выписной_эпикриз_{patient or 'пациент'}.docx",
        )
        if not output:
            return
        try:
            created = write_discharge_summary_docx(
                self.draft,
                Path(output),
                ignore_issues=bool(review_issues),
            )
        except Exception as exc:
            messagebox.showerror("Не удалось создать DOCX", str(exc), parent=self)
            return
        messagebox.showinfo(
            "Готово",
            f"Документ создан:\n{created}\n\nПроверьте его перед подписанием.",
            parent=self,
        )
