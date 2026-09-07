"""Structured editing for discharge tables using the existing domain dialogs."""
import tkinter as tk
from tkinter import ttk, simpledialog, messagebox
from mdrk_builder.ui.episode_adapter import parse_optional_datetime, role_from_name, role_names


class FieldsDialog(simpledialog.Dialog):
    def __init__(self, parent, title, values):
        self.values, self.widgets, self.result = values, {}, None
        super().__init__(parent, title)

    def body(self, master):
        for i, (key, (label, value)) in enumerate(self.values.items()):
            ttk.Label(master, text=label).grid(row=i, column=0, sticky='nw')
            if key == 'role':
                w = ttk.Combobox(master, values=role_names(), state='readonly', width=55); w.set(value)
            elif key == 'conclusion':
                w = tk.Text(master, height=8, width=70, wrap='word', undo=True);w.insert('1.0',value)
            else:
                w=ttk.Entry(master,width=70);w.insert(0,value)
            w.grid(row=i,column=1,sticky='ew',padx=5,pady=4);self.widgets[key]=w
        return next(iter(self.widgets.values()))

    def validate(self):
        self.result={k: w.get('1.0','end-1c') if isinstance(w,tk.Text) else w.get() for k,w in self.widgets.items()}
        try:
            for key in ('initial_at','current_at','occurred_at'):
                if key in self.result: parse_optional_datetime(self.result[key])
            if 'role' in self.result: role_from_name(self.result['role'])
        except ValueError as exc:
            messagebox.showerror('Проверьте поля',str(exc),parent=self);return False
        return True
