"""Keep each specialist's editable text and undo buffer bound to that finding."""
from tkinter import scrolledtext, ttk
from mdrk_builder.ui.formatted_text import FormattedText


class SpecialistConclusions(ttk.Frame):
    def __init__(self, parent, commit):
        super().__init__(parent)
        self._commit = commit
        self._buffers = {}
        self.finding = None
        self.text = self._new_text()
        self._empty = self.text
        self._empty.configure(state="disabled")
        self._empty.pack(fill="both", expand=True)

    def _new_text(self):
        text = FormattedText(self, height=12, wrap="word", undo=True)
        from mdrk_builder.ui.quick_phrases import add_quick_phrases
        bar = ttk.Frame(text.frame)
        bar.pack(side="top", fill="x", before=text.vbar)
        add_quick_phrases(bar, text, "conclusion")
        return text

    def show(self, finding, active_findings):
        self.text.pack_forget()
        active_ids = {id(row) for row in active_findings}
        for key in list(self._buffers):
            if key not in active_ids:
                _owner, widget = self._buffers.pop(key)
                widget.destroy()
        self.finding = finding
        if finding is None:
            self.text = self._empty
        else:
            key = id(finding)
            if key not in self._buffers:
                widget = self._new_text()
                widget.insert("1.0", finding.conclusion)
                widget.edit_reset()
                # A queued focus event retains the original owner even after
                # another specialist has been selected in the list.
                widget.bind("<FocusOut>", lambda event, owner=finding: self._commit(
                    event, finding=owner, widget=event.widget))
                self._buffers[key] = (finding, widget)
            self.text = self._buffers[key][1]
        self.text.pack(fill="both", expand=True)
        return self.text

    def clear(self):
        self.show(None, ())
