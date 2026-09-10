"""Explicit physician actions; missing source data never inserts a negative claim."""
import tkinter as tk
from tkinter import ttk


PHRASES = {
    "laboratory_results": ("Не проводились.",),
    "instrumental_results": ("Не проводились.",),
    "transfusions": ("Не проводились.",),
    "operations": ("Не проводились.",),
    "work_capacity": ("В выдаче листка нетрудоспособности не нуждается.",),
    "neurological_status": ("Неврологический статус без изменений.",),
    "local_status": ("Локальный статус без изменений.",),
    "discharge_neurological_status": ("Неврологический статус без изменений.",),
    "discharge_condition": ("Состояние без изменений.",),
    "conclusion": ("Статус без изменений.",),
}


def insert_phrase(widget, text):
    if str(widget.cget("state")) == "disabled":
        return
    if widget.tag_ranges("sel"):
        widget.delete("sel.first", "sel.last")
    widget.insert("insert", text)
    widget.focus_set()


def add_quick_phrases(parent, widget, field):
    if field not in PHRASES:
        return
    button = ttk.Menubutton(parent, text="Фразы")
    menu = tk.Menu(button, tearoff=False)
    for text in PHRASES[field]:
        menu.add_command(label=text, command=lambda value=text: insert_phrase(widget, value))
    button.configure(menu=menu)
    button.pack(side="right", padx=4)
