"""Document-like specialist editing using the discharge panel's existing edits."""
import tkinter as tk
from tkinter import ttk

from mdrk_builder.ui.inline_tree import InlineTreeEditor
from mdrk_builder.ui.episode_adapter import role_names
from mdrk_builder.ui.formatted_text import FormattedText
from mdrk_builder.ui.source_access import TableSourceAccess, row_source_links


class DischargeSpecialists(ttk.Frame):
    def __init__(self, panel):
        super().__init__(panel.notebook, padding=7)
        self.panel = panel
        self.index = None
        self.owner = None
        self._refreshing = False
        pane = ttk.Panedwindow(self, orient="horizontal")
        pane.pack(fill="both", expand=True)
        left, right = ttk.Frame(pane), ttk.Frame(pane)
        pane.add(left, weight=1)
        pane.add(right, weight=4)
        self.list = ttk.Treeview(left, show="tree", selectmode="browse")
        self.list.pack(fill="both", expand=True)
        self.list.bind("<<TreeviewSelect>>", self._select)
        bar = ttk.Frame(left)
        bar.pack(fill="x")
        ttk.Button(bar, text="Добавить", command=self._add).pack(side="left")
        ttk.Button(bar, text="Удалить", command=self._delete).pack(side="left")
        self.role = ttk.Combobox(right, values=role_names(), state="readonly")
        self.role.pack(fill="x")
        self.role.bind("<<ComboboxSelected>>", lambda _: self._field("role", self.role.get()))
        ttk.Label(right, text="ФИО специалиста").pack(anchor="w")
        self.name = ttk.Entry(right)
        self.name.pack(fill="x", pady=5)
        self.name.bind("<FocusOut>", lambda _: self._field("specialist_name", self.name.get()))
        ttk.Label(right, text="Должность в документе").pack(anchor="w")
        self.title = ttk.Entry(right)
        self.title.pack(fill="x", pady=(0, 5))
        self.title.bind("<FocusOut>", lambda _: self._field("specialist_title", self.title.get()))
        scale_bar = ttk.Frame(right)
        scale_bar.pack(fill="x")
        ttk.Label(scale_bar, text="Шкалы специалиста").pack(side="left")
        ttk.Button(scale_bar, text="Добавить шкалу", command=self._add_scale).pack(side="right")
        self.scales = ttk.Treeview(right, columns=("name", "initial_value", "value"), show="headings", height=6)
        for column, title, width in (("name", "Шкала", 340), ("initial_value", "Первичное", 100), ("value", "Повторное", 100)):
            self.scales.heading(column, text=title)
            self.scales.column(column, width=width)
        self.scales.pack(fill="x", pady=5)
        self._sources = TableSourceAccess(self.scales, scale_bar,
            links=self._scale_links, open_path=self.panel._open_path)
        self._editor = InlineTreeEditor(self.scales, editable_columns={"name", "initial_value", "value"},
            commit=self._scale, is_data_row=str.isdigit)
        self.scales.bind("<Delete>", self._delete_scale)
        conclusion_bar = ttk.Frame(right)
        conclusion_bar.pack(fill="x")
        ttk.Label(conclusion_bar, text="Заключение и рекомендации").pack(side="left")
        ttk.Button(conclusion_bar, text="Источник", command=lambda: self.panel._open_path(
            self.owner.source if self.owner else None)).pack(side="right")
        self.text = FormattedText(right, wrap="word", undo=True, height=14)
        self.text.pack(fill="both", expand=True)
        from mdrk_builder.ui.quick_phrases import add_quick_phrases
        add_quick_phrases(conclusion_bar, self.text, "conclusion")

    def commit(self):
        if self._refreshing or self.index is None or not self.panel.draft:
            return
        rows = self.panel.draft.team_findings
        if self.index >= len(rows) or rows[self.index] is not self.owner:
            return
        values = {"conclusion": self.text.get("1.0", "end-1c"),
                  "specialist_name": self.name.get(), "specialist_title": self.title.get()}
        changed = {key: value for key, value in values.items() if value != getattr(self.owner, key)}
        if changed:
            self._refreshing = True
            try:
                for key, value in changed.items():
                    self.panel._edit_clinical_row("cell", row_id=f"team:{self.index}", field=key, value=value)
                self.owner = self.panel.draft.team_findings[self.index]
            finally:
                self._refreshing = False

    def refresh(self):
        if self._refreshing:
            return
        self._refreshing = True
        try:
            self.list.delete(*self.list.get_children())
            rows = self.panel.draft.team_findings if self.panel.draft else ()
            for index, finding in enumerate(rows):
                self.list.insert("", "end", iid=str(index), text=f"{finding.role.display_name} {finding.specialist_name}".strip())
            self.index = min(self.index or 0, len(rows) - 1) if rows else None
            self.owner = rows[self.index] if self.index is not None else None
            self.text.delete("1.0", "end")
            self.text.insert("1.0", self.owner.conclusion if self.owner else "")
            self.text.edit_reset()
            self.scales.delete(*self.scales.get_children())
            self.role.set(self.owner.role.display_name if self.owner else "")
            self.name.delete(0, "end")
            self.name.insert(0, self.owner.specialist_name if self.owner else "")
            self.title.delete(0, "end")
            self.title.insert(0, self.owner.specialist_title if self.owner else "")
            if self.owner:
                self.list.selection_set(str(self.index))
                for index, scale in enumerate(self.owner.scales):
                    self.scales.insert("", "end", iid=str(index), values=(scale.name, scale.initial_value, scale.value))
        finally:
            self._refreshing = False

    def _select(self, _event=None):
        selected = self.list.selection()
        if self._refreshing or not selected or int(selected[0]) == self.index:
            return
        target = int(selected[0])
        self.commit()
        self.index = target
        self.refresh()

    def _field(self, name, value):
        if self._refreshing or self.owner is None or str(getattr(self.owner, name)) == value:
            return
        self.commit()
        self.panel._edit_clinical_row("cell", row_id=f"team:{self.index}", field=name, value=value)

    def _scale(self, row, column, value):
        self.commit()
        self.panel._edit_clinical_row("cell", row_id=f"team:{self.index}:scale:{row}", field=column, value=value)

    def _scale_links(self, row):
        if self.owner is None or not row.isdigit() or int(row) >= len(self.owner.scales):
            return ()
        scale = self.owner.scales[int(row)]
        initial = scale.initial_at.strftime("%d.%m.%Y") if scale.initial_at else "дата не указана"
        current = scale.current_at.strftime("%d.%m.%Y") if scale.current_at else "дата не указана"
        return [(f"Первичное измерение ({initial})", scale.initial_source),
                *row_source_links(scale, f"Последнее измерение ({current})")]

    def _add(self):
        self.commit()
        self.panel._edit_clinical_row("add", row_id="team")
        self.index = len(self.panel.draft.team_findings) - 1 if self.panel.draft else None
        self.refresh()

    def _delete(self):
        if self.index is not None:
            self.panel._edit_clinical_row("delete", row_id=f"team:{self.index}")

    def _add_scale(self):
        if self.index is not None:
            self.commit()
            self.panel._edit_clinical_row("add_scale", row_id=f"team:{self.index}")
            row = str(len(self.owner.scales) - 1)
            self.scales.selection_set(row)
            self.after_idle(lambda: self._editor.edit(row, "name"))

    def _delete_scale(self, _event=None):
        if self.index is not None and self.scales.selection():
            self.commit()
            self.panel._edit_clinical_row("delete", row_id=f"team:{self.index}:scale:{self.scales.selection()[0]}")
