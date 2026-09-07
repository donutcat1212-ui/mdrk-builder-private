"""Source access for native tables, resolved against the current row on demand."""
from __future__ import annotations

import tkinter as tk
from collections.abc import Callable, Sequence
from pathlib import Path
from tkinter import ttk

from mdrk_builder.domain import IcfDomain

SourceLinks = Sequence[tuple[str, Path | None]]


def path_column_links(tree: ttk.Treeview, item: str, column: str) -> SourceLinks:
    value = tree.set(item, column) if tree.exists(item) else ""
    return [("Исходный документ", Path(value) if value else None)]


def icf_source_links(domain: IcfDomain, *, include_final: bool = True) -> SourceLinks:
    links = [("Исходный документ", domain.initial_source)]
    if include_final:
        links.append(("Повторный документ", domain.final_source))
    if domain.source is not None and domain.source not in {path for _, path in links}:
        links.append(("Код, описание и ответственный", domain.source))
    if domain.origin_note:
        links.append((domain.origin_note, None))
    if domain.manual_fields:
        links = [(label + " до ручной правки", path) for label, path in links]
        links.append(("Ручная правка: " + ", ".join(manual_field_label(name) for name in sorted(domain.manual_fields)), None))
    return links


class TableSourceAccess:
    def __init__(
        self, tree: ttk.Treeview, parent: tk.Misc, *,
        links: Callable[[str], SourceLinks],
        open_path: Callable[[Path | None], None],
        show_button: bool = True,
    ) -> None:
        self.tree = tree
        self.links = links
        self.open_path = open_path
        self.menu = tk.Menu(tree, tearoff=False)
        self.button = ttk.Button(parent, text="Источник", command=self.open_selected)
        if show_button:
            self.button.pack(side="right")
        tree.bind("<<TreeviewSelect>>", self.refresh, add="+")
        tree.bind("<Map>", self.refresh, add="+")
        tree.bind("<Button-3>", self._pointer_menu, add="+")
        if tree.tk.call("tk", "windowingsystem") == "aqua":
            tree.bind("<Button-2>", self._pointer_menu, add="+")
            tree.bind("<Control-Button-1>", self._pointer_menu, add="+")
        tree.bind("<Shift-F10>", self._keyboard_menu, add="+")
        self.refresh()

    def _selected_links(self) -> SourceLinks:
        selected = self.tree.selection()
        return self.links(selected[0]) if selected else ()

    def refresh(self, _event: tk.Event | None = None) -> None:
        selected = self.tree.selection()
        available = any(path is not None for _, path in self._selected_links())
        self.button.configure(
            text="Источник" if available or not selected else "Источник не указан",
            state="normal" if available else "disabled",
        )

    def _fill_menu(self) -> None:
        self.menu.delete(0, "end")
        links = self._selected_links()
        if not links:
            self.menu.add_command(label="Источник не указан", state="disabled")
        for label, path in links:
            if path is None:
                self.menu.add_command(label=label if label.startswith(("Ручная", "Расчёт", "Шаблон")) else f"{label}: не указан", state="disabled")
            else:
                self.menu.add_command(
                    label=f"{label}: {path.name}",
                    command=lambda source=path: self.open_path(source),
                )
        for label, path in links:
            if path is not None:
                self.menu.add_command(label="Фрагмент: " + path.name,
                    command=lambda source=path: self._preview(source))

    def _preview(self, path):
        from mdrk_builder.ui.source_preview import preview_source
        selected = self.tree.selection()
        query = ""
        if selected:
            item = selected[0]
            for column in ("code", "name", "description"):
                if column in self.tree["columns"]:
                    query = self.tree.set(item, column)
                    if query:
                        break
            if not query:
                query = self.tree.item(item, "text").split(": ")[-1]
        preview_source(self.tree, path, query, self.open_path)

    def _show_menu(self, x: int, y: int) -> None:
        self._fill_menu()
        try:
            self.menu.tk_popup(x, y)
        finally:
            self.menu.grab_release()

    def _pointer_menu(self, event: tk.Event) -> str:
        row = self.tree.identify_row(event.y)
        if not row:
            return "break"
        self.tree.selection_set(row)
        self.tree.focus(row)
        self.tree.focus_set()
        self.refresh()
        self._show_menu(event.x_root, event.y_root)
        return "break"

    def _keyboard_menu(self, _event: tk.Event | None = None) -> str:
        selected = self.tree.selection()
        if selected:
            self.tree.see(selected[0])
            box = self.tree.bbox(selected[0])
            if box:
                x, y, _, height = box
                self._show_menu(self.tree.winfo_rootx() + x, self.tree.winfo_rooty() + y + height)
        return "break"

    def open_selected(self) -> None:
        links = self._selected_links()
        paths = {path for _, path in links if path is not None}
        has_origin_note = any(path is None and label.startswith(("Ручная правка", "Расчёт", "Шаблон"))
                              for label, path in links)
        if len(paths) == 1 and not has_origin_note:
            self.open_path(next(iter(paths)))
        elif paths:
            self._show_menu(self.button.winfo_rootx(), self.button.winfo_rooty() + self.button.winfo_height())


def field_source_links(sources: dict[str, Path], key: str, *, manual: bool = False) -> SourceLinks:
    paths = list(dict.fromkeys(path for name, path in sources.items()
                             if name == key or name.startswith(key + '.')))
    prefix = 'Документ до ручной правки' if manual else 'Исходный документ'
    links = [(prefix, path) for path in paths]
    if manual:
        links.append(('Ручная правка: текущее значение введено пользователем', None))
    return links


def row_source_links(row, label: str = 'Исходный документ') -> SourceLinks:
    manual = getattr(row, 'manual_fields', set())
    if manual:
        label += ' до ручной правки'
    links = [(label, path) for path in getattr(row, "source_paths", ())] or [(label, row.source)]
    if manual:
        links.append(('Ручная правка: ' + ', '.join(manual_field_label(name) for name in sorted(manual)), None))
    return links


def mark_manual_changes(previous, current) -> None:
    """Keep the origin document while marking only changed clinical fields."""
    from dataclasses import fields
    ignored = {'manual_fields', 'source', 'initial_source', 'final_source', 'field_sources', 'scales', 'origin_note'}
    object.__setattr__(current, 'manual_fields', set(getattr(previous, 'manual_fields', ())))
    for item in fields(current):
        if item.name not in ignored and getattr(previous, item.name) != getattr(current, item.name):
            current.manual_fields.add(item.name)


def open_source_links(parent: tk.Misc, links: SourceLinks, open_path: Callable[[Path | None], None]) -> None:
    menu = tk.Menu(parent, tearoff=False)
    parent._source_detail_menu = menu
    for label, path in links or [('Источник не указан', None)]:
        menu.add_command(label=f'{label}: {path.name}' if path else label,
                         state='normal' if path else 'disabled',
                         command=lambda p=path: open_path(p))
    from mdrk_builder.ui.source_preview import preview_source
    for path in dict.fromkeys(p for _, p in links if p is not None):
        menu.add_command(label='Показать фрагмент: ' + path.name,
                         command=lambda p=path: preview_source(parent, p, '', open_path))
    try:
        menu.tk_popup(parent.winfo_rootx(), parent.winfo_rooty() + parent.winfo_height())
    finally:
        menu.grab_release()


def open_source_path(path: Path | None) -> None:
    import os
    import subprocess
    import sys
    from tkinter import messagebox
    if path is None:
        return
    if not path.is_file():
        messagebox.showerror('Источник недоступен', f'Файл не найден:\n{path}\n\nПроверьте расположение исходных документов и повторите сканирование.')
        return
    try:
        if sys.platform == 'win32':
            os.startfile(path)
        else:
            subprocess.Popen(['open' if sys.platform == 'darwin' else 'xdg-open', str(path)])
    except OSError as exc:
        messagebox.showerror('Источник недоступен', f'Не удалось открыть {path}:\n{exc}')


def manual_field_label(name: str) -> str:
    return {"name": "название", "code": "код", "description": "описание", "value": "значение",
            "initial": "исходная оценка", "final": "повторная оценка", "specialist": "специалист",
            "note": "уточнение", "conclusion": "заключение", "source_datetime": "дата документа",
            "measured_at": "дата измерения", "actual_count": "количество", "duration_minutes": "длительность",
            "frequency": "кратность", "performer": "исполнитель", "appointment_date": "дата назначения",
            "performed_at": "дата исполнения", "intervention": "вмешательство", "role": "специалист",
            "section_override": "раздел МКФ"}.get(name, name)
