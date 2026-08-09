from __future__ import annotations

import tkinter as tk
from tkinter import messagebox, simpledialog, ttk

from mdrk_builder.domain import IcfDomain, Procedure, ScaleMeasurement, SpecialistFinding
from mdrk_builder.ui.episode_adapter import (
    format_datetime,
    format_qualifier,
    parse_optional_datetime,
    parse_optional_nonnegative_int,
    parse_qualifier,
    role_from_name,
    role_names,
)


class IcfDomainDialog(simpledialog.Dialog):
    def __init__(self, parent: tk.Misc, domain: IcfDomain | None = None) -> None:
        self.domain = domain
        self.result: IcfDomain | None = None
        self._variables: dict[str, tk.StringVar] = {}
        super().__init__(parent, "Домен МКФ")

    def body(self, master: tk.Frame) -> tk.Widget:
        values = {
            "code": self.domain.code if self.domain else "",
            "description": self.domain.description if self.domain else "",
            "role": self.domain.specialist.display_name if self.domain else "",
            "initial": format_qualifier(self.domain.initial) if self.domain else "",
            "final": format_qualifier(self.domain.final) if self.domain else "",
            "note": self.domain.note if self.domain else "",
        }
        labels = (
            ("code", "Код"),
            ("description", "Описание"),
            ("role", "Ответственный специалист"),
            ("initial", "Исходная оценка"),
            ("final", "Повторная оценка"),
            ("note", "Уточнение"),
        )
        first: tk.Widget | None = None
        for row, (key, label) in enumerate(labels):
            ttk.Label(master, text=label).grid(row=row, column=0, sticky="w", padx=6, pady=4)
            variable = tk.StringVar(value=values[key])
            self._variables[key] = variable
            if key == "role":
                widget: tk.Widget = ttk.Combobox(
                    master, textvariable=variable, values=role_names(), state="readonly", width=46
                )
            else:
                widget = ttk.Entry(master, textvariable=variable, width=49)
            widget.grid(row=row, column=1, sticky="ew", padx=6, pady=4)
            first = first or widget
        master.columnconfigure(1, weight=1)
        return first or master

    def validate(self) -> bool:
        try:
            if not self._variables["code"].get().strip():
                raise ValueError("Введите код МКФ")
            if not self._variables["description"].get().strip():
                raise ValueError("Введите описание домена")
            role_from_name(self._variables["role"].get())
            parse_qualifier(self._variables["initial"].get())
            parse_qualifier(self._variables["final"].get())
        except ValueError as exc:
            messagebox.showerror("Проверьте данные", str(exc), parent=self)
            return False
        return True

    def apply(self) -> None:
        previous = self.domain
        self.result = IcfDomain(
            code=self._variables["code"].get().strip(),
            description=self._variables["description"].get().strip(),
            specialist=role_from_name(self._variables["role"].get()),
            initial=parse_qualifier(self._variables["initial"].get()),
            final=parse_qualifier(self._variables["final"].get()),
            note=self._variables["note"].get().strip(),
            initial_source=previous.initial_source if previous else None,
            final_source=previous.final_source if previous else None,
        )


class ProcedureDialog(simpledialog.Dialog):
    def __init__(self, parent: tk.Misc, procedure: Procedure | None = None) -> None:
        self.procedure = procedure
        self.result: Procedure | None = None
        self._variables: dict[str, tk.StringVar] = {}
        super().__init__(parent, "Реабилитационная процедура")

    def body(self, master: tk.Frame) -> tk.Widget:
        values = {
            "code": self.procedure.code if self.procedure else "",
            "name": self.procedure.name if self.procedure else "",
            "specialist": self.procedure.specialist if self.procedure else "",
            "count": "" if not self.procedure or self.procedure.actual_count is None else str(self.procedure.actual_count),
            "duration": "" if not self.procedure or self.procedure.duration_minutes is None else str(self.procedure.duration_minutes),
            "frequency": self.procedure.frequency if self.procedure else "",
        }
        labels = (
            ("code", "Код услуги"),
            ("name", "Название"),
            ("specialist", "Ответственный специалист"),
            ("count", "Количество"),
            ("duration", "Продолжительность, мин"),
            ("frequency", "Кратность"),
        )
        first: tk.Widget | None = None
        for row, (key, label) in enumerate(labels):
            ttk.Label(master, text=label).grid(row=row, column=0, sticky="w", padx=6, pady=4)
            variable = tk.StringVar(value=values[key])
            self._variables[key] = variable
            widget = ttk.Entry(master, textvariable=variable, width=59)
            widget.grid(row=row, column=1, sticky="ew", padx=6, pady=4)
            first = first or widget
        master.columnconfigure(1, weight=1)
        return first or master

    def validate(self) -> bool:
        try:
            if not self._variables["name"].get().strip():
                raise ValueError("Введите название процедуры")
            parse_optional_nonnegative_int(self._variables["count"].get(), "Количество")
            parse_optional_nonnegative_int(self._variables["duration"].get(), "Продолжительность")
        except ValueError as exc:
            messagebox.showerror("Проверьте данные", str(exc), parent=self)
            return False
        return True

    def apply(self) -> None:
        previous = self.procedure
        self.result = Procedure(
            code=self._variables["code"].get().strip(),
            name=self._variables["name"].get().strip(),
            specialist=self._variables["specialist"].get().strip(),
            actual_count=parse_optional_nonnegative_int(self._variables["count"].get(), "Количество"),
            duration_minutes=parse_optional_nonnegative_int(
                self._variables["duration"].get(), "Продолжительность"
            ),
            frequency=self._variables["frequency"].get().strip(),
            planned_count=previous.planned_count if previous else None,
            source=previous.source if previous else None,
        )


class FindingDialog(simpledialog.Dialog):
    def __init__(self, parent: tk.Misc, finding: SpecialistFinding | None = None) -> None:
        self.finding = finding
        self.result: SpecialistFinding | None = None
        self._role = tk.StringVar(value=finding.role.display_name if finding else "")
        self._when = tk.StringVar(value=format_datetime(finding.source_datetime) if finding else "")
        self._conclusion: tk.Text | None = None
        super().__init__(parent, "Заключение специалиста")

    def body(self, master: tk.Frame) -> tk.Widget:
        ttk.Label(master, text="Специалист").grid(row=0, column=0, sticky="w", padx=6, pady=4)
        role = ttk.Combobox(master, textvariable=self._role, values=role_names(), state="readonly", width=46)
        role.grid(row=0, column=1, sticky="ew", padx=6, pady=4)
        ttk.Label(master, text="Дата и время").grid(row=1, column=0, sticky="w", padx=6, pady=4)
        ttk.Entry(master, textvariable=self._when, width=49).grid(
            row=1, column=1, sticky="ew", padx=6, pady=4
        )
        ttk.Label(master, text="Заключение").grid(row=2, column=0, sticky="nw", padx=6, pady=4)
        self._conclusion = tk.Text(master, width=58, height=10, wrap="word")
        self._conclusion.grid(row=2, column=1, sticky="nsew", padx=6, pady=4)
        if self.finding:
            self._conclusion.insert("1.0", self.finding.conclusion)
        master.columnconfigure(1, weight=1)
        master.rowconfigure(2, weight=1)
        return role

    def validate(self) -> bool:
        try:
            role_from_name(self._role.get())
            parse_optional_datetime(self._when.get())
        except ValueError as exc:
            messagebox.showerror("Проверьте данные", str(exc), parent=self)
            return False
        return True

    def apply(self) -> None:
        previous = self.finding
        self.result = SpecialistFinding(
            role=role_from_name(self._role.get()),
            conclusion=self._conclusion.get("1.0", "end-1c").strip() if self._conclusion else "",
            source_datetime=parse_optional_datetime(self._when.get()),
            source=previous.source if previous else None,
            scales=list(previous.scales) if previous else [],
        )


class ScaleDialog(simpledialog.Dialog):
    def __init__(self, parent: tk.Misc, measurement: ScaleMeasurement | None = None) -> None:
        self.measurement = measurement
        self.result: ScaleMeasurement | None = None
        self._variables = {
            "role": tk.StringVar(value=measurement.specialist.display_name if measurement else ""),
            "when": tk.StringVar(value=format_datetime(measurement.measured_at) if measurement else ""),
            "name": tk.StringVar(value=measurement.name if measurement else ""),
            "value": tk.StringVar(value=measurement.value if measurement else ""),
        }
        super().__init__(parent, "Измерение шкалы")

    def body(self, master: tk.Frame) -> tk.Widget:
        labels = (
            ("role", "Специалист"),
            ("when", "Дата и время"),
            ("name", "Шкала/опросник"),
            ("value", "Результат"),
        )
        first: tk.Widget | None = None
        for row, (key, label) in enumerate(labels):
            ttk.Label(master, text=label).grid(row=row, column=0, sticky="w", padx=6, pady=4)
            if key == "role":
                widget: tk.Widget = ttk.Combobox(
                    master,
                    textvariable=self._variables[key],
                    values=role_names(),
                    state="readonly",
                    width=46,
                )
            else:
                widget = ttk.Entry(master, textvariable=self._variables[key], width=49)
            widget.grid(row=row, column=1, sticky="ew", padx=6, pady=4)
            first = first or widget
        master.columnconfigure(1, weight=1)
        return first or master

    def validate(self) -> bool:
        try:
            role_from_name(self._variables["role"].get())
            parse_optional_datetime(self._variables["when"].get())
            if not self._variables["name"].get().strip():
                raise ValueError("Введите название шкалы")
            if not self._variables["value"].get().strip():
                raise ValueError("Введите результат")
        except ValueError as exc:
            messagebox.showerror("Проверьте данные", str(exc), parent=self)
            return False
        return True

    def apply(self) -> None:
        self.result = ScaleMeasurement(
            name=self._variables["name"].get().strip(),
            value=self._variables["value"].get().strip(),
            measured_at=parse_optional_datetime(self._variables["when"].get()),
            specialist=role_from_name(self._variables["role"].get()),
            source=self.measurement.source if self.measurement else None,
        )
