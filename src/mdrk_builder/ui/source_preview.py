"""Local source excerpt viewer, with original table/row coordinates and search."""
import tkinter as tk
from tkinter import ttk


def preview_source(parent, path, query, open_path):
    window=tk.Toplevel(parent);window.title('Источник: '+path.name);window.geometry('1000x700')
    bar=ttk.Frame(window);bar.pack(fill='x')
    search=tk.StringVar(value=query)
    ttk.Entry(bar,textvariable=search,width=60).pack(side='left',fill='x',expand=True)
    text=tk.Text(window,wrap='word');text.pack(fill='both',expand=True)
    session=getattr(parent.winfo_toplevel(),'_mdrk_scan_session',None)
    document=session.parsed(path) if session else None
    if document:
        lines=list(document.paragraphs)
        for ti,table in enumerate(document.tables,1):
            for ri,row in enumerate(table.rows,1):
                lines.append(f'Таблица {ti}, строка {ri}: '+ ' | '.join(row.as_list()))
        text.insert('1.0','\n'.join(lines))
    else:
        text.insert('1.0','Фрагмент пока недоступен. Повторно считайте документы для просмотра здесь или откройте оригинал.')
    text.configure(state='disabled');text.tag_configure('match',background='#fff1aa')
    def find():
        text.tag_remove('match','1.0','end');needle=search.get().strip()
        if not needle:return
        pos=text.search(needle,'1.0',stopindex='end',nocase=True)
        if pos:text.tag_add('match',pos,f'{pos}+{len(needle)}c');text.see(pos)
    ttk.Button(bar,text='Найти',command=find).pack(side='left')
    ttk.Button(bar,text='Открыть оригинал',command=lambda:open_path(path)).pack(side='right')
    window.bind('<Return>',lambda event:find());find()
