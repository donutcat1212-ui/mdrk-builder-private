"""Native Tk bold tags with a portable draft representation; fonts stay fixed."""
import tkinter as tk
from tkinter import font, scrolledtext, ttk

from mdrk_builder.domain.formatted_text import emphasis_runs, emphasize


class FormattedText(scrolledtext.ScrolledText):
    def __init__(self, parent, **kwargs):
        super().__init__(parent, **kwargs, exportselection=False)
        self._bold_font = font.Font(self, font=self.cget("font"))
        self._bold_font.configure(weight="bold")
        self.tag_configure("bold", font=self._bold_font)
        bar = ttk.Frame(self.frame)
        bar.pack(side="top", fill="x", before=self.vbar)
        ttk.Button(bar, text="Ж", width=3, takefocus=False, command=self.toggle_bold).pack(side="left")
        self.bind("<Control-b>", self.toggle_bold)
        self.bind("<Control-B>", self.toggle_bold)
        if self.tk.call("tk", "windowingsystem") == "aqua":
            self.bind("<Command-b>", self.toggle_bold)
            self.bind("<Command-B>", self.toggle_bold)

    def toggle_bold(self, _event=None):
        if str(self.cget("state")) == "disabled":
            return "break"
        ranges = self.tag_ranges("sel")
        if not ranges:
            return "break"
        first, last = ranges
        fully_bold = self.tag_nextrange("bold", first, last)
        remove = bool(fully_bold and self.compare(fully_bold[0], "==", first)
                      and self.compare(fully_bold[1], ">=", last))
        (self.tag_remove if remove else self.tag_add)("bold", first, last)
        self.edit_modified(True)
        return "break"

    def insert(self, index, chars, *tags):
        if tags or "**" not in chars:
            return super().insert(index, chars, *tags)
        # A right-gravity mark advances correctly on Tk 8.6 and Tk 9, including Unicode.
        self.mark_set("format_insert", index)
        self.mark_gravity("format_insert", "right")
        for text, bold in emphasis_runs(chars):
            super().insert("format_insert", text, ("bold",) if bold else ())
        self.mark_unset("format_insert")

    def get(self, index1, index2=None):
        plain = super().get(index1, index2)
        if str(index1) != "1.0" or str(index2) not in {"end-1c", "end"}:
            return plain
        ranges = self.tag_ranges("bold")
        output, offset = [], 0
        for first, last in zip(ranges[::2], ranges[1::2]):
            start = len(super().get("1.0", first))
            end = min(len(plain), len(super().get("1.0", last)))
            output.extend((plain[offset:start], emphasize(plain[start:end])))
            offset = end
        output.append(plain[offset:])
        return "".join(output)
