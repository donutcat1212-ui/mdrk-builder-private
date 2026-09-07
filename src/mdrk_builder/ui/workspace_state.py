"""Local recoverable workspace state and explicit leave/save actions."""
from copy import deepcopy
from pathlib import Path
from tkinter import messagebox
from mdrk_builder.infrastructure.draft_store import save_draft, load_draft


class WorkspaceState:
    def _capture_workspace(self):
        manual = self._capture_manual_state()
        state = {'episode': manual['episode'] if manual else self.episode,
                 'manual': manual, 'kind': self._current_kind, 'document': self.document_var.get(),
                 'baseline': getattr(self, '_scan_baseline', None),
                 'reverse': deepcopy(self.reverse_workspace.draft),
                 'discharge': deepcopy(self.discharge_workspace.draft),
                 'reverse_baseline': getattr(self.reverse_workspace, '_baseline', None),
                 'discharge_baseline': getattr(self.discharge_workspace, '_baseline', None),
                 'reverse_dirty': self.reverse_workspace._rows_dirty,
                 'reverse_header_dirty': self.reverse_workspace._header_dirty,
                 'discharge_dirty': self.discharge_workspace._dirty_fields,
                 'discharge_identity_dirty': self.discharge_workspace._dirty_identity,
                 'entries': {k: v.get() for k,v in self._entry_variables.items()},
                 'sections': {k: w.get('1.0','end-1c') for k,w in self._text_fields.items()},
                 'discharge_entries': {k: v.get() for k,v in self.discharge_workspace._identity_vars.items()},
                 'discharge_text': {k: w.get('1.0','end-1c') for k,w in self.discharge_workspace._widgets.items()},
                 'reverse_entries': {k: v.get() for k,v in self.reverse_workspace._header_vars.items()}}
        return state

    def _state_folder(self):
        for value in (self.episode, self.discharge_workspace.draft, self.reverse_workspace.draft):
            if value is not None:
                return value.folder
        return None

    def _save_workspace(self, explicit=False):
        folder = self._state_folder()
        if folder is None:
            return False
        try:
            state = self._capture_workspace()
            save_draft(folder / '.mdrk draft.json', state)
            self._saved_workspace = deepcopy(state)
        except (OSError, ValueError, TypeError) as exc:
            if explicit:
                messagebox.showerror('Черновик не сохранён', str(exc), parent=self.root)
            else:
                self.status_var.set('Не удалось сохранить локальный черновик. Используйте «Сохранить черновик».')
            return False
        if explicit:
            self.status_var.set('Рабочий черновик сохранён в папке эпизода')
        return True

    def _autosave_workspace(self):
        if not self._scanning and self._state_folder():
            self._save_workspace()
        self.root.after(30000, self._autosave_workspace)

    def _refresh_draft_indicator(self):
        if not self._scanning:
            state = self._capture_workspace() if self._state_folder() else None
            unsaved = state is not None and state != getattr(self, '_saved_workspace', None)
            title = self.root.title().removesuffix(' • Черновик не сохранён')
            self.root.title(title + (' • Черновик не сохранён' if unsaved else ''))
        self.root.after(1500, self._refresh_draft_indicator)

    def _confirm_leave(self):
        if not self._state_folder():
            return True
        answer = messagebox.askyesnocancel('Рабочий черновик',
            'Сохранить текущие правки в локальный черновик перед закрытием или сменой пациента?', parent=self.root)
        if answer is None:
            return False
        if answer:
            return self._save_workspace(explicit=True)
        return True

    def _restore_workspace(self, folder):
        path = folder / '.mdrk draft.json'
        if not path.is_file():
            return False
        if not messagebox.askyesno('Найден черновик', 'Восстановить сохранённые правки этого эпизода?', parent=self.root):
            return False
        try:
            state = load_draft(path)
            for name in ('episode', 'reverse', 'discharge'):
                draft = state.get(name)
                if draft is not None and draft.folder.resolve() != folder.resolve():
                    raise ValueError('Черновик относится к другой папке эпизода')
            self._table_history.clear()
            self.reverse_workspace._table_history.clear()
            self.discharge_workspace._table_history.clear()
            self.reverse_workspace.draft = None
            self.discharge_workspace.draft = None
            self.episode = state['episode']
            self._current_kind = state['kind']
            self._scan_baseline = state.get('baseline')
            manual = state.get('manual') or {}
            self._dirty_entry_fields = manual.get('entry_fields', set())
            self._dirty_section_fields = manual.get('section_fields', self._dirty_section_fields)
            self._manual_collections = manual.get('collections', set())
            if self.episode:
                self._populate_from_episode()
            self._populating = True
            self.discharge_workspace._populating = True
            self.reverse_workspace._populating = True
            for key, value in state['entries'].items(): self._entry_variables[key].set(value)
            for key, value in state['sections'].items():
                w=self._text_fields[key];w.delete('1.0','end');w.insert('1.0',value);w.edit_reset()
            for name, panel in (('reverse',self.reverse_workspace),('discharge',self.discharge_workspace)):
                if state[name] is not None:
                    panel.load(state[name]);panel._baseline=state.get(name+'_baseline') or deepcopy(state[name])
            self.reverse_draft=state['reverse'];self.discharge_draft=state['discharge']
            self.discharge_workspace._populating = True
            self.reverse_workspace._populating = True
            for key,value in state['discharge_entries'].items(): self.discharge_workspace._identity_vars[key].set(value)
            for key,value in state['discharge_text'].items():
                w=self.discharge_workspace._widgets[key];w.delete('1.0','end');w.insert('1.0',value);w.edit_reset()
            for key,value in state['reverse_entries'].items(): self.reverse_workspace._header_vars[key].set(value)
            self.reverse_workspace._rows_dirty=state.get('reverse_dirty',False)
            self.reverse_workspace._header_dirty=state.get('reverse_header_dirty',set())
            self.discharge_workspace._dirty_fields=state.get('discharge_dirty',set())
            self.discharge_workspace._dirty_identity=state.get('discharge_identity_dirty',set())
            self.document_var.set(state['document'])
            self._previous_document = state['document']
            for key, panel in (('reverse', self.reverse_workspace), ('discharge', self.discharge_workspace), ('mdrk', self.mdrk_workspace)):
                panel.pack_forget()
            panel = self.reverse_workspace if state['document'] == 'reverse' else self.discharge_workspace if state['document'] == 'discharge' else self.mdrk_workspace
            panel.pack(fill='both', expand=True)
            self.kind_var.set(state['kind'].value)
            self._populating = False
            self.discharge_workspace._populating = False
            self.reverse_workspace._populating = False
            self._update_action_states()
            self._saved_workspace = deepcopy(self._capture_workspace())
            self.status_var.set('Черновик восстановлен. Повторное считывание обновит исходные данные, сохраняя правки.')
            return True
        except (OSError, ValueError, KeyError, TypeError) as exc:
            messagebox.showerror('Не удалось открыть черновик',str(exc),parent=self.root)
            return False
