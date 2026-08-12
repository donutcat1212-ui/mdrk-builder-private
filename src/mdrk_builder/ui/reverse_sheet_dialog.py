from __future__ import annotations

import re
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk

from mdrk_builder.domain import ReverseSheetDraft, ReverseSheetRow
from mdrk_builder.infrastructure.reverse_sheet_writer import write_reverse_sheet_docx
from mdrk_builder.ui.episode_adapter import (
    format_date,
    format_datetime,
    parse_optional_date,
    parse_optional_datetime,
)


class ReverseSheetRowDialog(simpledialog.Dialog):
    def __init__(self, parent: tk.Misc, row: ReverseSheetRow | None = None) -> None:
        self._source = row.source if row else None
        self._intervention = tk.StringVar(value=row.intervention if row else "")
        self._appointment = tk.StringVar(value=format_date(row.appointment_date) if row else "")
        self._performed = tk.StringVar(value=format_datetime(row.performed_at) if row else "")
        self._performer = tk.StringVar(value=row.performer if row else "")
        self.result: ReverseSheetRow | None = None
        super().__init__(parent, "Строка оборотного листа")

    def body(self, master: tk.Frame) -> tk.Widget:
        labels = (
            ("Медицинское вмешательство", self._intervention),
            ("Дата назначения (ДД.ММ.ГГГГ)", self._appointment),
            ("Дата исполнения (ДД.ММ.ГГГГ ЧЧ:ММ)", self._performed),
            ("Исполнитель", self._performer),
        )
        first: ttk.Entry | None = None
        for row_index, (label, variable) in enumerate(labels):
            ttk.Label(master, text=label).grid(row=row_index, column=0, sticky="w", padx=6, pady=5)
            entry = ttk.Entry(master, textvariable=variable, width=58)
            entry.grid(row=row_index, column=1, sticky="ew", padx=6, pady=5)
            first = first or entry
        master.columnconfigure(1, weight=1)
        return first or master

    def validate(self) -> bool:
        try:
            if not self._intervention.get().strip():
                raise ValueError("Укажите медицинское вмешательство")
            appointment = parse_optional_date(self._appointment.get())
            performed = parse_optional_datetime(self._performed.get())
        except ValueError as exc:
            messagebox.showerror("Проверьте строку", str(exc), parent=self)
            return False
        self.result = ReverseSheetRow(
            self._intervention.get().strip(),
            appointment,
            performed,
            self._performer.get().strip(),
            self._source,
        )
        return True


class ReverseSheetDialog(tk.Toplevel):
    def __init__(self, parent: tk.Misc, draft: ReverseSheetDraft) -> None:
        super().__init__(parent)
        self.draft = draft
        self.title("Оборотный лист назначений")
        self.geometry("1080x670")
        self.minsize(850, 540)
        self.transient(parent)
        self.protocol("WM_DELETE_WINDOW", self.destroy)

        self._full_name = tk.StringVar(value=draft.identity.full_name)
        self._birth_date = tk.StringVar(value=format_date(draft.identity.birth_date))
        self._record_number = tk.StringVar(value=draft.identity.medical_record_number)
        self._admission = tk.StringVar(value=format_datetime(draft.admission_datetime))
        self._build()
        self._refresh_rows()
        self.grab_set()

    def _build(self) -> None:
        shell = ttk.Frame(self, padding=12)
        shell.pack(fill="both", expand=True)

        ttk.Label(
            shell,
            text="Оборотный лист назначений и их выполнения",
            font=("TkDefaultFont", 14, "bold"),
        ).pack(anchor="w")
        ttk.Label(
            shell,
            text=(
                "Шапка берётся только из первичной консультации невролога. "
                "Пустые даты не вычисляются автоматически."
            ),
            foreground="#555555",
        ).pack(anchor="w", pady=(2, 10))

        header = ttk.LabelFrame(shell, text="Шапка", padding=8)
        header.pack(fill="x")
        fields = (
            ("ФИО", self._full_name),
            ("Дата рождения", self._birth_date),
            ("Номер медкарты", self._record_number),
            ("Поступление", self._admission),
        )
        for index, (label, variable) in enumerate(fields):
            row, pair = divmod(index, 2)
            ttk.Label(header, text=label).grid(row=row, column=pair * 2, sticky="w", padx=(0, 6), pady=4)
            ttk.Entry(header, textvariable=variable).grid(
                row=row, column=pair * 2 + 1, sticky="ew", padx=(0, 16), pady=4
            )
        header.columnconfigure(1, weight=1)
        header.columnconfigure(3, weight=1)

        if self.draft.issues:
            issue_bar = ttk.Frame(shell)
            issue_bar.pack(fill="x", pady=(9, 2))
            ttk.Label(
                issue_bar,
                text=f"Требует проверки: {len(self.draft.issues)}. Неподтверждённые поля оставлены пустыми.",
                foreground="#9A5B00",
            ).pack(side="left")
            ttk.Button(issue_bar, text="Показать причины…", command=self._show_issues).pack(
                side="left", padx=(8, 0)
            )

        table_frame = ttk.Frame(shell)
        table_frame.pack(fill="both", expand=True, pady=(8, 6))
        columns = ("intervention", "appointment", "performed", "performer")
        self.tree = ttk.Treeview(table_frame, columns=columns, show="headings", selectmode="browse")
        headings = (
            ("intervention", "Медицинское вмешательство", 370),
            ("appointment", "Дата назначения", 125),
            ("performed", "Дата и время исполнения", 165),
            ("performer", "Исполнитель", 210),
        )
        for name, label, width in headings:
            self.tree.heading(name, text=label)
            self.tree.column(name, width=width, minwidth=90, anchor="w" if name in {"intervention", "performer"} else "center")
        scrollbar = ttk.Scrollbar(table_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        self.tree.bind("<Double-1>", lambda _event: self._edit_row())
        self.tree.bind("<<TreeviewSelect>>", lambda _event: self._update_row_actions())

        controls = ttk.Frame(shell)
        controls.pack(fill="x")
        ttk.Button(controls, text="Добавить", command=self._add_row).pack(side="left", padx=(0, 4))
        self.edit_button = ttk.Button(controls, text="Изменить", command=self._edit_row, state="disabled")
        self.edit_button.pack(side="left", padx=4)
        self.delete_button = ttk.Button(controls, text="Удалить", command=self._delete_row, state="disabled")
        self.delete_button.pack(side="left", padx=4)
        ttk.Button(controls, text="Закрыть", command=self.destroy).pack(side="right", padx=(4, 0))
        ttk.Button(controls, text="Создать DOCX…", command=self._generate).pack(side="right", padx=4)

    def _refresh_rows(self) -> None:
        self.tree.delete(*self.tree.get_children())
        for index, row in enumerate(self.draft.rows):
            self.tree.insert(
                "",
                "end",
                iid=str(index),
                values=(
                    row.intervention,
                    format_date(row.appointment_date),
                    format_datetime(row.performed_at),
                    row.performer,
                ),
            )
        self._update_row_actions()

    def _update_row_actions(self) -> None:
        state = "normal" if self.tree.selection() else "disabled"
        self.edit_button.configure(state=state)
        self.delete_button.configure(state=state)

    def _show_issues(self) -> None:
        details = []
        for index, issue in enumerate(self.draft.issues, start=1):
            source = f"\nИсточник: {issue.source.name}" if issue.source else ""
            details.append(f"{index}. {issue.message}{source}")
        messagebox.showwarning(
            "Поля, требующие проверки",
            "\n\n".join(details),
            parent=self,
        )

    def _selected_index(self) -> int | None:
        selected = self.tree.selection()
        return int(selected[0]) if selected else None

    def _sort_rows(self) -> None:
        self.draft.rows.sort(
            key=lambda row: (
                row.performed_at is None,
                row.performed_at or datetime.max,
                row.intervention.casefold(),
            )
        )

    def _add_row(self) -> None:
        dialog = ReverseSheetRowDialog(self)
        if dialog.result is not None:
            self.draft.rows.append(dialog.result)
            self._sort_rows()
            self._refresh_rows()

    def _edit_row(self) -> None:
        index = self._selected_index()
        if index is None:
            return
        dialog = ReverseSheetRowDialog(self, self.draft.rows[index])
        if dialog.result is not None:
            self.draft.rows[index] = dialog.result
            self._sort_rows()
            self._refresh_rows()
            new_index = self.draft.rows.index(dialog.result)
            self.tree.selection_set(str(new_index))
            self.tree.focus(str(new_index))
            self.tree.see(str(new_index))

    def _delete_row(self) -> None:
        index = self._selected_index()
        if index is None:
            return
        row = self.draft.rows[index]
        if not messagebox.askyesno(
            "Удалить строку?",
            f"Удалить «{row.intervention}» из оборотного листа?",
            parent=self,
        ):
            return
        del self.draft.rows[index]
        self._refresh_rows()

    def _apply_header(self) -> bool:
        try:
            self.draft.identity.full_name = self._full_name.get().strip()
            self.draft.identity.birth_date = parse_optional_date(self._birth_date.get())
            self.draft.identity.medical_record_number = self._record_number.get().strip()
            self.draft.admission_datetime = parse_optional_datetime(self._admission.get())
        except ValueError as exc:
            messagebox.showerror("Проверьте шапку", str(exc), parent=self)
            return False
        return True

    def _generate(self) -> None:
        if not self._apply_header():
            return
        undated = [row.intervention for row in self.draft.rows if row.performed_at is None]
        if undated:
            messagebox.showerror(
                "Не заполнена хронология",
                "Для следующих строк не указана дата исполнения:\n\n"
                + "\n".join(f"• {value}" for value in undated)
                + "\n\nЗаполните дату либо удалите строку перед созданием DOCX.",
                parent=self,
            )
            return
        patient = re.sub(r"[^0-9A-Za-zА-Яа-яЁё_-]+", "_", self.draft.identity.full_name).strip("_")
        output = filedialog.asksaveasfilename(
            parent=self,
            title="Сохранить оборотный лист",
            defaultextension=".docx",
            filetypes=(("Документ Word", "*.docx"),),
            initialfile=f"Оборотный_лист_{patient or 'пациент'}.docx",
        )
        if not output:
            return
        try:
            created = write_reverse_sheet_docx(self.draft, Path(output))
        except Exception as exc:
            messagebox.showerror("Не удалось создать DOCX", str(exc), parent=self)
            return
        messagebox.showinfo(
            "Готово",
            f"Документ создан:\n{created}\n\nПроверьте даты и подписи перед использованием.",
            parent=self,
        )
