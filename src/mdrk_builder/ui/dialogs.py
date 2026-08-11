from __future__ import annotations

import sys
import tkinter as tk
from tkinter import messagebox, simpledialog, ttk
from typing import Any

from mdrk_builder.domain import (
    IcfDomain,
    MdrkKind,
    Procedure,
    ScaleMeasurement,
    SpecialistFinding,
    SpecialistRole,
)
from mdrk_builder.ui.episode_adapter import (
    format_datetime,
    format_qualifier,
    parse_optional_datetime,
    parse_optional_nonnegative_int,
    parse_qualifier,
    role_from_name,
    role_names,
)


_LATIN_CONTROL_SHORTCUTS = {
    "a": "select_all",
    "c": "copy",
    "v": "paste",
    "x": "cut",
}

_RUSSIAN_CONTROL_SHORTCUTS = {
    # Physical A/C/V/X keys in the standard Russian keyboard layout.
    "cyrillic_ef": "select_all",
    "cyrillic_es": "copy",
    "cyrillic_em": "paste",
    "cyrillic_che": "cut",
    "ф": "select_all",
    "с": "copy",
    "м": "paste",
    "ч": "cut",
}

_WINDOWS_VIRTUAL_KEY_SHORTCUTS = {
    65: "select_all",
    67: "copy",
    86: "paste",
    88: "cut",
}

_CLIPBOARD_VIRTUAL_EVENTS = {
    "copy": "<<Copy>>",
    "paste": "<<Paste>>",
    "cut": "<<Cut>>",
}


def install_edit_shortcuts(root: tk.Misc) -> None:
    """Install one app-wide dispatcher, including editors in child dialogs.

    Tk already implements the Latin clipboard shortcuts through virtual events.
    The dispatcher leaves those bindings alone and only bridges keysyms produced
    by the Russian Windows keyboard layout to the same virtual events.
    """

    root.bind_all("<Control-KeyPress>", _dispatch_control_shortcut, add="+")


def _dispatch_control_shortcut(event: Any) -> str | None:
    action, is_native_latin = _shortcut_action(event)
    if action is None:
        return None

    widget = event.widget
    if _is_treeview(widget):
        if action == "copy":
            return _copy_tree_selection(widget)
        if action == "select_all":
            children = widget.get_children()
            if children:
                widget.selection_set(children)
            return "break"
        return None

    if action == "select_all":
        return _select_all_text(widget)

    # Standard Latin C/V/X already trigger <<Copy>>, <<Paste>> and <<Cut>>.
    # Generating them again would duplicate a paste, so only bridge aliases.
    if is_native_latin:
        return None
    try:
        widget.event_generate(_CLIPBOARD_VIRTUAL_EVENTS[action])
    except (AttributeError, tk.TclError):
        return None
    return "break"


def _shortcut_action(event: Any) -> tuple[str | None, bool]:
    keysym = str(getattr(event, "keysym", "")).casefold()
    if keysym in _LATIN_CONTROL_SHORTCUTS:
        return _LATIN_CONTROL_SHORTCUTS[keysym], True
    if keysym in _RUSSIAN_CONTROL_SHORTCUTS:
        return _RUSSIAN_CONTROL_SHORTCUTS[keysym], False
    if sys.platform == "win32":
        return _WINDOWS_VIRTUAL_KEY_SHORTCUTS.get(getattr(event, "keycode", None)), False
    return None, False


def _is_treeview(widget: Any) -> bool:
    try:
        return widget.winfo_class() == "Treeview"
    except (AttributeError, tk.TclError):
        return False


def _copy_tree_selection(tree: Any) -> str | None:
    selected = set(tree.selection())
    if not selected:
        return "break"
    rows = [
        "\t".join(str(value) for value in tree.item(item_id, "values"))
        for item_id in tree.get_children()
        if item_id in selected
    ]
    if not rows:
        return "break"
    tree.clipboard_clear()
    tree.clipboard_append("\n".join(rows))
    return "break"


def _select_all_text(widget: Any) -> str | None:
    try:
        widget_class = widget.winfo_class()
        if widget_class == "Text":
            widget.tag_add("sel", "1.0", "end-1c")
            widget.mark_set("insert", "end-1c")
            widget.see("insert")
        elif widget_class in {"Entry", "TEntry", "TCombobox", "Spinbox", "TSpinbox"}:
            widget.selection_range(0, "end")
            widget.icursor("end")
        else:
            return None
    except (AttributeError, tk.TclError):
        return None
    return "break"


class IcfDomainDialog(simpledialog.Dialog):
    def __init__(
        self,
        parent: tk.Misc,
        domain: IcfDomain | None = None,
        kind: MdrkKind = MdrkKind.FINAL,
    ) -> None:
        self.domain = domain
        self.kind = kind
        self.result: IcfDomain | None = None
        self._variables: dict[str, tk.StringVar] = {}
        super().__init__(parent, "Домен МКФ")

    def body(self, master: tk.Frame) -> tk.Widget:
        values = {
            "code": self.domain.code if self.domain else "",
            "description": self.domain.description if self.domain else "",
            "role": (
                self.domain.specialist.display_name
                if self.domain and self.domain.specialist is not SpecialistRole.OTHER
                else ""
            ),
            "initial": format_qualifier(self.domain.initial) if self.domain else "",
            "final": format_qualifier(self.domain.final) if self.domain else "",
            "note": self.domain.note if self.domain else "",
        }
        labels = [
            ("code", "Код"),
            ("description", "Описание"),
            ("role", "Ответственный специалист"),
            ("initial", "Исходная оценка"),
            ("note", "Уточнение"),
        ]
        if self.kind is MdrkKind.FINAL:
            labels.insert(4, ("final", "Повторная оценка"))
        first: tk.Widget | None = None
        for row, (key, label) in enumerate(labels):
            ttk.Label(master, text=label).grid(row=row, column=0, sticky="w", padx=6, pady=4)
            variable = tk.StringVar(value=values[key])
            self._variables[key] = variable
            if key == "role":
                widget: tk.Widget = ttk.Combobox(
                    master,
                    textvariable=variable,
                    values=(
                        "",
                        *(
                            role.display_name
                            for role in SpecialistRole
                            if role is not SpecialistRole.OTHER
                        ),
                    ),
                    state="readonly",
                    width=46,
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
            role_value = self._variables["role"].get().strip()
            if role_value:
                role_from_name(role_value)
            parse_qualifier(self._variables["initial"].get())
            if "final" in self._variables:
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
            specialist=(
                role_from_name(self._variables["role"].get())
                if self._variables["role"].get().strip()
                else SpecialistRole.OTHER
            ),
            initial=parse_qualifier(self._variables["initial"].get()),
            final=(
                parse_qualifier(self._variables["final"].get())
                if "final" in self._variables
                else (previous.final if previous else None)
            ),
            note=self._variables["note"].get().strip(),
            initial_source=previous.initial_source if previous else None,
            final_source=previous.final_source if previous else None,
            initial_measured_at=previous.initial_measured_at if previous else None,
            final_measured_at=previous.final_measured_at if previous else None,
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
            count_needs_review=previous.count_needs_review if previous else False,
            performed_dates=previous.performed_dates if previous else (),
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
