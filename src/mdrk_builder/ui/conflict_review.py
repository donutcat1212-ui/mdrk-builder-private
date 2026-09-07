"""Compare full source alternatives before explicitly selecting a clinical value."""
from tkinter import ttk, simpledialog
import tkinter as tk
from dataclasses import replace
from mdrk_builder.application.conflicts import scale_conflicts
from mdrk_builder.application.diagnosis import diagnosis_parts, LABELS


class ChoiceDialog(simpledialog.Dialog):
    def __init__(self,parent,title,choices):
        self.choices=choices;self.result=None
        super().__init__(parent,title)

    def body(self,master):
        self.selection=tk.IntVar(value=0)
        for i,(value,source) in enumerate(self.choices):
            ttk.Radiobutton(master,text=f'Вариант {i+1}: {source.name if source else "ручной"}',variable=self.selection,value=i).pack(anchor='w')
            w=tk.Text(master,width=85,height=5,wrap='word');w.insert('1.0',value);w.configure(state='disabled');w.pack(fill='x')
        return master

    def apply(self): self.result=self.selection.get()


def resolve_conflicts(app):
    if app.document_var.get()=='discharge' and app.discharge_workspace.draft:
        panel=app.discharge_workspace;panel.apply();draft=panel.draft
        for key,choices in list(draft.conflict_choices.items()):
            dialog=ChoiceDialog(app.root, 'Согласовать: '+key, choices)
            if dialog.result is None:break
            value,source=choices[dialog.result]
            if key.startswith('clinical_diagnosis.'):
                parts=diagnosis_parts(draft.clinical_diagnosis);parts[key.split('.')[-1]]=value
                draft.clinical_diagnosis='\n\n'.join(LABELS[k]+':\n'+v for k,v in parts.items())
                draft.manual_fields.add('clinical_diagnosis');panel._dirty_fields.add('clinical_diagnosis')
            elif key.startswith('icf:'):
                from mdrk_builder.application.icf_conflicts import resolve_icf_choice
                resolve_icf_choice(draft.icf_domains, key, value, source)
            elif key.startswith('scale:'):
                from mdrk_builder.application.conflicts import resolve_discharge_scale
                resolve_discharge_scale(draft, key, value, source)
            elif hasattr(draft, key):
                setattr(draft, key, value)
                panel._dirty_fields.add(key)
            draft.manual_fields.add(key)
            if source is not None:
                draft.field_sources[key]=source
            draft.conflict_choices.pop(key)
            draft.issues=[i for i in draft.issues if i.field!=key]
        panel._populate()
    elif app.episode:
        from mdrk_builder.application.icf_conflicts import icf_choices, resolve_icf_choice
        for key, choices in icf_choices(app.episode.icf_domains).items():
            dialog = ChoiceDialog(app.root, 'Согласовать МКФ', choices)
            if dialog.result is None:
                return
            value, source = choices[dialog.result]
            resolve_icf_choice(app.episode.icf_domains, key, value, source)
            app._mark_collection_dirty('icf')
        for rows in scale_conflicts(app.episode):
            dialog=ChoiceDialog(app.root, 'Согласовать: '+rows[0].name, [(r.value,r.source) for r in rows])
            if dialog.result is None:break
            chosen=rows[dialog.result];chosen.manual_fields.add('value')
            app._mark_collection_dirty('findings')
        app._refresh_all_trees()
