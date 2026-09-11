"""Physician-owned phrases, inserted only by an explicit menu action."""
import tkinter as tk
from tkinter import messagebox, scrolledtext, simpledialog, ttk

from mdrk_builder.infrastructure.phrase_store import PhraseStore, PhraseStorageError


def insert_phrase(widget, text):
    if str(widget.cget("state")) == "disabled":
        return
    widget.edit_separator()
    if widget.tag_ranges("sel"):
        widget.delete("sel.first", "sel.last")
    widget.insert("insert", text)
    widget.edit_separator()
    widget.event_generate("<<UserTextEdit>>")
    widget.focus_set()


def _phrase_label(text):
    label = " ".join(text.split())
    return label if len(label) <= 90 else label[:87] + "…"


class PhraseDialog(simpledialog.Dialog):
    def __init__(self, parent, store, field):
        self.store, self.field = store, field
        super().__init__(parent, title="Добавить фразу")

    def body(self, parent):
        ttk.Label(parent, text="Текст фразы").pack(anchor="w")
        self.text = scrolledtext.ScrolledText(parent, width=65, height=9, wrap="word", undo=True)
        self.text.pack(fill="both", expand=True, pady=5)
        return self.text

    def buttonbox(self):
        bar = ttk.Frame(self)
        ttk.Button(bar, text="Сохранить", command=self.ok).pack(side="left", padx=5)
        ttk.Button(bar, text="Отмена", command=self.cancel).pack(side="left", padx=5)
        bar.pack(pady=5)
        self.bind("<Control-Return>", self.ok)
        self.bind("<Escape>", self.cancel)

    def validate(self):
        try:
            self.store.add(self.field, self.text.get("1.0", "end-1c"))
        except (PhraseStorageError, ValueError) as exc:
            messagebox.showerror("Фраза не сохранена", str(exc), parent=self)
            return False
        return True


class QuickPhrases(ttk.Menubutton):
    def __init__(self, parent, widget, field, *, store=None):
        super().__init__(parent, text="Фразы")
        self.widget, self.field = widget, field
        self.store = store if store is not None else PhraseStore()
        self.menu = tk.Menu(self, tearoff=False, postcommand=self.refresh)
        self.delete_menu = tk.Menu(self.menu, tearoff=False)
        self.configure(menu=self.menu)
        self.pack(side="right", padx=4)

    def refresh(self):
        self.menu.delete(0, "end")
        self.delete_menu.delete(0, "end")
        try:
            phrases = self.store.phrases(self.field)
        except PhraseStorageError as exc:
            message = str(exc)
            self.menu.add_command(label="Не удалось загрузить фразы…", command=lambda: messagebox.showerror(
                "Фразы недоступны", message, parent=self))
            return
        for text in phrases:
            self.menu.add_command(label=_phrase_label(text), command=lambda value=text: insert_phrase(self.widget, value))
            self.delete_menu.add_command(label=_phrase_label(text), command=lambda value=text: self.delete_phrase(value))
        if phrases:
            self.menu.add_separator()
        self.menu.add_command(label="+ Добавить", command=self.add_phrase)
        if phrases:
            self.menu.add_cascade(label="Удалить", menu=self.delete_menu)

    def add_phrase(self):
        PhraseDialog(self, self.store, self.field)
        self.refresh()

    def delete_phrase(self, text):
        try:
            self.store.delete(self.field, text)
        except PhraseStorageError as exc:
            messagebox.showerror("Фраза не удалена", str(exc), parent=self)
        self.refresh()


def add_quick_phrases(parent, widget, field):
    return QuickPhrases(parent, widget, field)
