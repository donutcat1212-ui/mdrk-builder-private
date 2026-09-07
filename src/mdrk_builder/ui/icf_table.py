"""Visible cell boundaries using native ttk elements, including Tk 8.6."""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk


def apply_icf_grid_style(tree: ttk.Treeview) -> None:
    style = ttk.Style(tree)
    element = "Icf.cellborder"
    if element not in style.element_names():
        # Transparent centre keeps native selection and section backgrounds visible.
        border = tk.PhotoImage(master=tree, width=3, height=3)
        border.put("#aab6c4", to=(2, 0, 3, 3))
        border.put("#aab6c4", to=(0, 2, 3, 3))
        style.element_create(element, "image", border, border=(0, 0, 1, 1), sticky="nsew")
        for suffix in ("Cell", "Item"):
            style.layout(f"Icf.Treeview.{suffix}", [(element, {
                "sticky": "nsew", "children": style.layout(f"Treeview.{suffix}"),
            })])
        # The style belongs to the interpreter, not to a single table instance.
        tree._root()._icf_grid_image = border
    tree.configure(style="Icf.Treeview")
