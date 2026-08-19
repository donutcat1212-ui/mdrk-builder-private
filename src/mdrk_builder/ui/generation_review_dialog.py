from __future__ import annotations

import tkinter as tk
from collections.abc import Sequence
from tkinter import ttk

from mdrk_builder.domain import ReviewIssue, ReviewSeverity


def confirm_generation_with_issues(
    parent: tk.Misc,
    issues: Sequence[ReviewIssue],
    *,
    document_name: str,
) -> bool:
    review_issues = tuple(
        issue
        for issue in issues
        if issue.severity in {ReviewSeverity.BLOCKING, ReviewSeverity.WARNING}
    )
    if not review_issues:
        return True
    dialog = _GenerationReviewDialog(parent, review_issues, document_name)
    dialog.grab_set()
    dialog.wait_window()
    return dialog.confirmed


class _GenerationReviewDialog(tk.Toplevel):
    def __init__(
        self,
        parent: tk.Misc,
        issues: tuple[ReviewIssue, ...],
        document_name: str,
    ) -> None:
        super().__init__(parent)
        self.confirmed = False
        self.title("Проверка перед созданием")
        self.geometry("760x430")
        self.minsize(620, 350)
        self.transient(parent)
        self.protocol("WM_DELETE_WINDOW", self._cancel)
        self.bind("<Escape>", lambda _event: self._cancel())

        shell = ttk.Frame(self, padding=14)
        shell.pack(fill="both", expand=True)
        ttk.Label(
            shell,
            text=f"Перед созданием документа «{document_name}» найдены замечания",
            font=("TkDefaultFont", 13, "bold"),
        ).pack(anchor="w")
        ttk.Label(
            shell,
            text=(
                "Раскройте нужный раздел для просмотра. Документ можно создать "
                "с текущими данными, но замечания необходимо проверить вручную."
            ),
            foreground="#555555",
            wraplength=710,
            justify="left",
        ).pack(fill="x", pady=(4, 12))

        groups = (
            (
                "Блокирующие ошибки",
                ReviewSeverity.BLOCKING,
                "#A40000",
            ),
            (
                "Предупреждения",
                ReviewSeverity.WARNING,
                "#9A5B00",
            ),
        )
        for title, severity, color in groups:
            group_issues = tuple(issue for issue in issues if issue.severity is severity)
            self._add_group(shell, title, group_issues, color)

        ttk.Separator(shell).pack(fill="x", pady=(12, 10))
        controls = ttk.Frame(shell)
        controls.pack(fill="x", side="bottom")
        ttk.Button(controls, text="Отмена", command=self._cancel).pack(side="right")
        ttk.Button(
            controls,
            text="Игнорировать все и создать",
            command=self._confirm,
        ).pack(side="right", padx=(0, 8))

    def _add_group(
        self,
        parent: ttk.Frame,
        title: str,
        issues: tuple[ReviewIssue, ...],
        color: str,
    ) -> None:
        section = ttk.Frame(parent)
        section.pack(fill="x", pady=3)
        body = ttk.Frame(section, padding=(18, 5, 0, 5))

        header_text = tk.StringVar(value=f"▸ {title} ({len(issues)})")

        def toggle() -> None:
            if not issues:
                return
            if body.winfo_manager():
                body.pack_forget()
                header_text.set(f"▸ {title} ({len(issues)})")
            else:
                body.pack(fill="x")
                header_text.set(f"▾ {title} ({len(issues)})")

        ttk.Button(section, textvariable=header_text, command=toggle).pack(fill="x")
        if not issues:
            return

        text = tk.Text(
            body,
            height=min(7, max(3, len(issues) * 2)),
            wrap="word",
            relief="flat",
            background="#F5F5F5",
            foreground=color,
            padx=8,
            pady=6,
        )
        scrollbar = ttk.Scrollbar(body, orient="vertical", command=text.yview)
        text.configure(yscrollcommand=scrollbar.set)
        for index, issue in enumerate(issues, start=1):
            source = f"\n   Источник: {issue.source}" if issue.source else ""
            field = f"\n   Поле: {issue.field}" if issue.field else ""
            text.insert("end", f"{index}. {issue.message}{field}{source}\n\n")
        text.configure(state="disabled")
        text.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

    def _confirm(self) -> None:
        self.confirmed = True
        self.destroy()

    def _cancel(self) -> None:
        self.destroy()
