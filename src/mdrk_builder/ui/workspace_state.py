"""Local persistence and leave prompts, independent of document panel internals."""
from copy import deepcopy
from tkinter import messagebox

from mdrk_builder.application.workspace import restore_workspace_state
from mdrk_builder.infrastructure.draft_store import save_draft, load_draft


class WorkspacePersistence:
    def __init__(self, root, *, capture, restore, folder, busy, status):
        self.root = root
        self.capture, self.restore = capture, restore
        self.folder, self.busy, self.status = folder, busy, status
        self.saved = None

    def save(self, explicit=False):
        folder = self.folder()
        if folder is None:
            return False
        try:
            state = self.capture()
            state.validate(folder)
            save_draft(folder / '.mdrk draft.json', state)
            self.saved = deepcopy(state)
        except (OSError, ValueError, TypeError) as exc:
            if explicit:
                messagebox.showerror('Черновик не сохранён', str(exc), parent=self.root)
            else:
                self.status('Не удалось сохранить локальный черновик. Используйте «Сохранить черновик».')
            return False
        if explicit:
            self.status('Рабочий черновик сохранён в папке эпизода')
        return True

    def autosave(self):
        if not self.busy() and self.folder():
            self.save()
        self.root.after(30000, self.autosave)

    def refresh_indicator(self):
        if not self.busy():
            state = self.capture() if self.folder() else None
            unsaved = state is not None and state != self.saved
            title = self.root.title().removesuffix(' • Черновик не сохранён')
            self.root.title(title + (' • Черновик не сохранён' if unsaved else ''))
        self.root.after(1500, self.refresh_indicator)

    def confirm_leave(self):
        if not self.folder():
            return True
        answer = messagebox.askyesnocancel('Рабочий черновик',
            'Сохранить текущие правки в локальный черновик перед закрытием или сменой пациента?', parent=self.root)
        if answer is None:
            return False
        return self.save(explicit=True) if answer else True

    def open(self, folder):
        path = folder / '.mdrk draft.json'
        if not path.is_file():
            return False
        if not messagebox.askyesno('Найден черновик', 'Восстановить сохранённые правки этого эпизода?', parent=self.root):
            return False
        try:
            state = restore_workspace_state(load_draft(path), folder)
            self.restore(state)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            messagebox.showerror('Не удалось открыть черновик', str(exc), parent=self.root)
            return False
        self.saved = deepcopy(self.capture())
        self.status('Черновик восстановлен. Повторное считывание обновит исходные данные, сохраняя правки.')
        return True
