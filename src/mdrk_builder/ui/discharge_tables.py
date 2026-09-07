"""Structured editing for discharge tables using the existing domain dialogs."""
from dataclasses import replace
from copy import deepcopy
import tkinter as tk
from tkinter import ttk, simpledialog, messagebox
from mdrk_builder.domain import DischargeScaleRow, DischargeTeamFinding, SpecialistRole
from mdrk_builder.ui.dialogs import IcfDomainDialog, ProcedureDialog
from mdrk_builder.ui.episode_adapter import parse_optional_datetime, format_datetime, role_from_name, role_names
from mdrk_builder.ui.source_access import mark_manual_changes
from mdrk_builder.application.editing import row_key


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


class DischargeTableEditing:
    def _edit_discharge_icf(self, action='edit'):
        if self.draft is None: return
        selected=self.icf_tree.selection()
        index=int(selected[0].split(':')[1]) if selected and selected[0].startswith('domain:') else None
        rows=list(self.draft.icf_domains)
        if action != 'add' and index is None: return
        old=rows[index] if index is not None else None
        if action == 'delete':
            if not messagebox.askyesno('Удалить строку', 'Удалить выбранный домен? Отменить можно через меню «Правка».', parent=self): return
            rows.pop(index)
        elif action == 'source':
            original=next((r for r in self._baseline.icf_domains if row_key(r)==row_key(old)),None)
            if original is None: return
            rows[index]=deepcopy(original)
        else:
            dialog=IcfDomainDialog(self, old if action=='edit' else None)
            if not dialog.result:return
            if action=='add':
                dialog.result.manual_fields.update({'code', 'initial', 'final'})
                rows.append(dialog.result)
            else: rows[index]=dialog.result
        self.draft.icf_domains=tuple(rows);self._refresh_icf()

    def _edit_clinical_row(self, action='edit'):
        if self.draft is None:return
        selected=self.clinical_tree.selection()
        if not selected:return
        parts=selected[0].split(':');group=parts[0]
        mapping={'team':'team_findings','program':'completed_procedures','admission':'admission_scale_rows','discharge':'discharge_scale_rows'}
        if group not in mapping:return
        attr=mapping[group];rows=list(getattr(self.draft,attr));index=int(parts[1]) if len(parts)>1 else None
        if action!='add' and index is None:return
        child=len(parts)==4 and parts[2]=='scale'
        if action == "add_scale":
            if group != "team" or index is None: return
            child = True
            action = "add"
        parent=rows[index] if index is not None else None
        target=list(parent.scales) if child else rows
        target_index=(int(parts[3]) if len(parts)==4 else None) if child else index
        old=target[target_index] if target_index is not None else None
        if action=='delete':
            if not messagebox.askyesno('Удалить строку','Удалить выбранную строку? Отмена доступна в меню «Правка».',parent=self):return
            target.pop(target_index)
        elif action=='source':
            originals=getattr(self._baseline,attr)
            if child:
                original_parent=next((r for r in originals if row_key(r)==row_key(parent)),None)
                originals=original_parent.scales if original_parent else ()
            original=next((r for r in originals if row_key(r)==row_key(old)),None)
            if original is None:return
            target[target_index]=deepcopy(original)
        else:
            if action=='add':old=None
            if group=='program':
                dialog=ProcedureDialog(self,old);new=dialog.result
            elif group=='team' and not child:
                values={'role':('Специалист',old.role.display_name if old else SpecialistRole.OTHER.display_name),
                    'specialist_name':('ФИО',old.specialist_name if old else ''),
                    'occurred_at':('Дата и время',format_datetime(old.occurred_at) if old else ''),
                    'conclusion':('Заключение',old.conclusion if old else '')}
                dialog=FieldsDialog(self,'Заключение специалиста',values)
                if not dialog.result:return
                data=dialog.result;new=replace(old,role=role_from_name(data['role']),specialist_name=data['specialist_name'],occurred_at=parse_optional_datetime(data['occurred_at']),conclusion=data['conclusion']) if old else DischargeTeamFinding(role_from_name(data['role']),data['conclusion'],specialist_name=data['specialist_name'],occurred_at=parse_optional_datetime(data['occurred_at']))
            else:
                values={'role':('Специалист',old.role.display_name if old else SpecialistRole.OTHER.display_name),
                    'name':('Шкала',old.name if old else ''),'value':('Текущее значение',old.value if old else ''),
                    'initial_value':('Первичное значение',old.initial_value if old else ''),
                    'initial_at':('Дата первичной оценки',format_datetime(old.initial_at) if old else ''),
                    'current_at':('Дата текущей оценки',format_datetime(old.current_at) if old else '')}
                if child:
                    values.pop('role')
                dialog=FieldsDialog(self,'Шкала',values)
                if not dialog.result:return
                data=dialog.result;data['role']=parent.role if child else role_from_name(data['role'])
                for k in ('initial_at','current_at'):data[k]=parse_optional_datetime(data[k])
                new=replace(old,**data) if old else DischargeScaleRow(**data)
            if new is None:return
            if old is not None:mark_manual_changes(old,new)
            elif hasattr(new,'manual_fields'):new.manual_fields.update({'value'} if hasattr(new,'value') else {'name'})
            if action=='add':target.append(new)
            else:target[target_index]=new
        from mdrk_builder.application.shared_edits import synchronize_scale_rows, synchronize_discharge_point, remove_scale_rows
        if child:
            rows[index] = replace(parent, scales=tuple(target))
            if old is not None:
                remove_scale_rows(self.draft, old)
            if action != 'delete':
                synchronize_scale_rows(self.draft, target[-1] if action == 'add' else target[target_index])
        setattr(self.draft, attr, tuple(rows))
        if group in {'admission', 'discharge'}:
            synchronize_discharge_point(self.draft, attr, old, None if action == 'delete' else target[-1] if action == 'add' else target[target_index])
        elif group == 'team' and not child and action == 'delete':
            for row in old.scales:
                remove_scale_rows(self.draft, row)
        self._refresh_clinical_data()
