"""Visible cell boundaries using native ttk elements, including Tk 8.6."""
from __future__ import annotations

from tkinter import ttk


def apply_icf_grid_style(tree: ttk.Treeview) -> None:
    style = ttk.Style(tree)
    element = "Icf.cellborder"
    if element not in style.element_names():
        # Tk 8.6 tiles image elements pixel by pixel. A tiny transparent image
        # across every cell can stall Windows painting; the native border is O(1).
        style.element_create(element, "from", "clam", "border")
        for suffix in ("Cell", "Item"):
            style.layout(f"Icf.Treeview.{suffix}", [(element, {
                "sticky": "nsew", "children": style.layout(f"Treeview.{suffix}"),
            })])
    style.configure(
        "Icf.Treeview", borderwidth=1, relief="solid",
        bordercolor="#aab6c4", lightcolor="#aab6c4", darkcolor="#aab6c4",
    )
    tree.configure(style="Icf.Treeview")
