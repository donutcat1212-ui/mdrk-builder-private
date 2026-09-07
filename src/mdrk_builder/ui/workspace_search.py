"""Search current editable fields and tables, with direct navigation to each result."""
import tkinter as tk
from tkinter import ttk


def reveal(widget, item=None):
    chain=[];child=widget
    while child.master is not None:
        parent=child.master
        if isinstance(parent,ttk.Notebook):chain.append((parent,child))
        child=parent
    for notebook,tab in reversed(chain):notebook.select(tab)
    if item is not None and isinstance(widget,ttk.Treeview) and widget.exists(item):
        ancestor=widget.parent(item)
        while ancestor:
            widget.item(ancestor,open=True);ancestor=widget.parent(ancestor)
        widget.selection_set(item);widget.see(item)
    widget.focus_set()


def search_workspace(app):
    window=tk.Toplevel(app.root);window.title('Найти и проверить');window.geometry('1000x600')
    bar=ttk.Frame(window);bar.pack(fill='x');query=tk.StringVar();mode=tk.StringVar(value='Все')
    entry=ttk.Entry(bar,textvariable=query);entry.pack(side='left',fill='x',expand=True)
    ttk.Combobox(bar,textvariable=mode,values=('Все','Незаполненное','Ручные правки','Конфликты'),state='readonly').pack(side='left')
    results=ttk.Treeview(window,columns=('location','value'),show='headings');results.heading('location',text='Поле / строка');results.heading('value',text='Значение');results.column('location',width=300);results.column('value',width=650);results.pack(fill='both',expand=True)
    from mdrk_builder.ui.discharge_summary_dialog import DISCHARGE_TEXT_FIELDS
    labels = {field.name: field.label for field in DISCHARGE_TEXT_FIELDS}
    refs={}
    def gather():
        document=app.document_var.get();records=[]
        if document in {'mdrk1','mdrk2'}:
            for key,w in app._text_fields.items():records.append((labels.get(key,key),w.get('1.0','end-1c'),w,None,key in app._dirty_section_fields[app._current_kind]))
            trees=[app.icf_tree,app.procedure_tree,app.finding_tree,app.issue_tree]
        else:
            panel=app.discharge_workspace if document=='discharge' else app.reverse_workspace
            for key,w in getattr(panel,'_widgets',{}).items():records.append((labels.get(key,key),w.get('1.0','end-1c'),w,None,key in panel._dirty_fields))
            trees=[getattr(panel,name,None) for name in ('icf_tree','clinical_tree','row_tree','warning_tree')]
        for tree in trees:
            if tree is None:continue
            def visit(parent=''):
                for item in tree.get_children(parent):
                    data=tree.item(item);value=' | '.join(str(v) for v in data['values'])
                    links=[]
                    if document=='discharge':
                        links=panel._clinical_links.get(item,()) if tree is getattr(panel,'clinical_tree',None) else panel._icf_source_links(item) if tree is getattr(panel,'icf_tree',None) else ()
                    manual=any('ручн' in label.casefold() for label,_ in links)
                    if document in {'mdrk1','mdrk2'} and app.episode and item.isdigit():
                        rows=app.episode.icf_domains if tree is app.icf_tree else app.episode.procedures if tree is app.procedure_tree else app.episode.findings if tree is app.finding_tree else []
                        if int(item)<len(rows):manual=bool(getattr(rows[int(item)],'manual_fields',()))
                    records.append((data['text'] or (str(data['values'][0]) if data['values'] else item),value,tree,item,manual))
                    visit(item)
            visit()
        return records
    def run(*args):
        results.delete(*results.get_children());refs.clear()
        for label,value,widget,item,manual in gather():
            text=(label+' '+value).casefold()
            if query.get().casefold() not in text:continue
            selected=mode.get()
            if selected=='Ручные правки' and not manual:continue
            if selected=='Конфликты' and not any(x in text for x in ('конфликт','расхожд','разные знач','другой вариант','другого пациента')):continue
            if selected=='Незаполненное' and value.strip() and not any(x in text for x in ('не заполн','не найден','отсутств','не указан','—',' |  | ')):continue
            key=str(len(refs));refs[key]=(widget,item);results.insert('', 'end',iid=key,values=(label,value))
    def jump(event=None):
        if results.selection():
            widget,item=refs[results.selection()[0]];window.destroy();reveal(widget,item)
    query.trace_add('write',run);mode.trace_add('write',run);results.bind('<Double-1>',jump);results.bind('<Return>',jump)
    ttk.Button(bar,text='Перейти',command=jump).pack(side='right');run();entry.focus_set()
