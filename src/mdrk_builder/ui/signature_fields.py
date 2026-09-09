"""Two signatory controls backed by the existing editable signature text."""
import re
import tkinter as tk
from tkinter import scrolledtext, ttk


class SignatureFields(ttk.Frame):
    def __init__(self, parent, text: tk.Text):
        super().__init__(parent)
        self.text = text
        self._last_text = None
        self._loading = False
        self._prefix = ""
        self._fields = {}
        for name, label in (("doctor", "Лечащий врач"), ("head", "Заведующий отделением")):
            ttk.Label(self, text=label).pack(anchor="w")
            field = scrolledtext.ScrolledText(self, height=3, wrap="word", undo=True)
            field.pack(fill="both", expand=True)
            field.bind("<<Modified>>", self._changed)
            self._fields[name] = field
        text.bind("<<Modified>>", self._refresh, add="+")

    def _refresh(self, _event=None):
        value = self.text.get("1.0", "end-1c")
        if value == self._last_text:
            return
        self._last_text = value
        labels = list(re.finditer(r"(?im)^(лечащ\w*\s+врач|заведующ\w*\s+отделени\w*)\s*:?\s*", value))
        if value and not labels:
            self.pack_forget()
            self.text.pack(fill="both", expand=True)
            return
        self.text.pack_forget()
        self.pack(fill="both", expand=True)
        self._prefix = value[:labels[0].start()].strip() if labels else ""
        values = {"doctor": "", "head": ""}
        for index, match in enumerate(labels):
            end = labels[index + 1].start() if index + 1 < len(labels) else len(value)
            name = "doctor" if match.group(1).lower().startswith("лечащ") else "head"
            values[name] = value[match.end():end].strip()
        self._loading = True
        try:
            for name, field in self._fields.items():
                field.delete("1.0", "end")
                field.insert("1.0", values[name])
                field.edit_reset()
                field.edit_modified(False)
        finally:
            self._loading = False

    def _changed(self, event):
        field = event.widget
        if not field.edit_modified():
            return
        field.edit_modified(False)
        if self._loading:
            return
        lines = [self._prefix] if self._prefix else []
        for name, label in (("doctor", "Лечащий врач"), ("head", "Заведующий отделением")):
            lines.append(label + ": " + self._fields[name].get("1.0", "end-1c"))
        self._last_text = "\n".join(lines)
        self.text.delete("1.0", "end")
        self.text.insert("1.0", self._last_text)
