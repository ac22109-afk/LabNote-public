"""Column mapping, full validation preview, then an explicit local save."""
import json
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from .bulk_import import FIELDS, preview_import, commit_import, export_template, suggest_mapping
from .tables import read_table, sheet_names
from .store import StoreError, registrant_id
from .session import DesktopSession


class BulkImportDialog(tk.Toplevel):
    MODES = {'測定（登録済み試料へ追加）': 'measurement', '試料を追加': 'sample'}

    def __init__(self, app):
        super().__init__(app.root)
        self.app, self.table, self.entries = app, None, []
        self.title('Excel／CSV一括登録')
        self.geometry('1100x760')
        self.minsize(900, 650)
        self.transient(app.root)
        self.protocol('WM_DELETE_WINDOW', self.close)
        self.columnconfigure(0, weight=1)
        self.rowconfigure(5, weight=1)
        ttk.Label(self, text='① ファイルを選択 → ② 列を対応づける → ③ 内容を確認 → ④ PCに保存。NASへの送信は主画面から行います。', padding=12).grid(row=0, column=0, sticky='ew')
        controls = ttk.Frame(self, padding=(12, 0))
        controls.grid(row=1, column=0, sticky='ew')
        self.mode = ttk.Combobox(controls, values=list(self.MODES), state='readonly', width=30)
        self.mode.set(next(iter(self.MODES)))
        self.mode.pack(side='left')
        self.mode.bind('<<ComboboxSelected>>', lambda event: self.reset_mapping())
        ttk.Button(controls, text='Excel／CSVを選ぶ…', command=self.choose).pack(side='left', padx=8)
        ttk.Button(controls, text='記入用ひな形を保存…', command=self.template).pack(side='left')
        ttk.Label(controls, text='Excelシート').pack(side='left', padx=(12, 4))
        self.sheet = ttk.Combobox(controls, state='readonly', width=18)
        self.sheet.pack(side='left')
        self.sheet.bind('<<ComboboxSelected>>', lambda event: self.read_path())
        self.filename = ttk.Label(self, text='ファイル未選択', padding=12, wraplength=1020)
        self.filename.grid(row=2, column=0, sticky='ew')
        self.mapping_frame = ttk.LabelFrame(self, text='列の対応（空欄：使わない／担当者は登録者IDを使用）', padding=10)
        self.mapping_frame.grid(row=3, column=0, sticky='ew', padx=12)
        self.mapping = {}
        self.preview_button = ttk.Button(self, text='プレビューして入力を検査', command=self.preview)
        self.preview_button.grid(row=4, column=0, sticky='w', padx=12, pady=8)
        self.tree = ttk.Treeview(self, columns=('row', 'status', 'summary', 'detail'), show='headings', height=10)
        for key, label, width in [('row', '行', 55), ('status', '判定', 95), ('summary', '内容', 310), ('detail', '確認事項', 520)]:
            self.tree.heading(key, text=label)
            self.tree.column(key, width=width, stretch=key in {'summary', 'detail'})
        self.tree.grid(row=5, column=0, sticky='nsew', padx=(12, 28))
        scrollbar = ttk.Scrollbar(self, orient='vertical', command=self.tree.yview)
        scrollbar.grid(row=5, column=0, sticky='nse', padx=(0, 12))
        self.tree.configure(yscrollcommand=scrollbar.set)
        self.tree.bind('<Double-1>', self.details)
        self.status = ttk.Label(self, text='測定セット名が同じ行は1記録にまとめ、数値を反復値として保持します。', padding=12, wraplength=1020)
        self.status.grid(row=6, column=0, sticky='ew')
        bottom = ttk.Frame(self, padding=12)
        bottom.grid(row=7, column=0, sticky='ew')
        self.save_button = ttk.Button(bottom, text='確認した内容をPCに保存（未送信）', command=self.save, state='disabled')
        self.save_button.pack(side='right')
        ttk.Button(bottom, text='閉じる', command=self.close).pack(side='left')
        self.reset_mapping()

    def kind(self):
        return self.MODES[self.mode.get()]

    def close(self):
        if not self.app.job:
            self.destroy()

    def details(self, event=None):
        selection = self.tree.selection()
        if selection:
            entry = self.entries[int(selection[0])]
            self.app.show_text('取り込む内容', entry.detail + '\n\n' + json.dumps(entry.data, ensure_ascii=False, indent=2))

    def invalidate(self, event=None):
        self.entries = []
        self.save_button.configure(state='disabled')
        self.tree.delete(*self.tree.get_children())

    def reset_mapping(self):
        self.invalidate()
        for child in self.mapping_frame.winfo_children():
            child.destroy()
        self.mapping = {}
        defaults = suggest_mapping(self.table.headers, self.kind()) if self.table else {}
        for i, (key, label) in enumerate(FIELDS[self.kind()]):
            row, column = i % 5, i // 5 * 2
            ttk.Label(self.mapping_frame, text=label).grid(row=row, column=column, sticky='w', padx=6, pady=3)
            box = ttk.Combobox(self.mapping_frame, state='readonly', values=[''] + list(self.table.headers if self.table else ()), width=31)
            box.grid(row=row, column=column+1, sticky='ew', padx=6, pady=3)
            box.set(defaults.get(key, ''))
            box.bind('<<ComboboxSelected>>', self.invalidate)
            self.mapping[key] = box

    def choose(self):
        path = filedialog.askopenfilename(parent=self, title='一括登録する表', filetypes=[('Excel／CSV', '*.xlsx *.csv *.tsv'), ('すべて', '*.*')])
        if not path or self.app.job:
            return
        self.path = Path(path)
        self.sheet.set('')
        self.read_path(load_sheets=True)

    def read_path(self, load_sheets=False):
        if not hasattr(self, 'path') or self.app.job:
            return
        self.invalidate()
        path, sheet = self.path, self.sheet.get()
        def work():
            names = sheet_names(path) if load_sheets else None
            try:
                return read_table(path, sheet=sheet or None), names, ''
            except (StoreError, ValueError) as exc:
                if names:
                    return None, names, str(exc)
                raise
        def done(result):
            self.table, names, error = result
            if names is not None:
                self.sheet.configure(values=names)
            if error:
                self.sheet.set(names[0])
                self.filename.configure(text=path.name + '：シートを選択し直してください。' + error)
                self.reset_mapping()
                return
            self.sheet.set(self.table.sheet)
            self.filename.configure(text=f'{path.name} ／ {len(self.table.rows)}行 ／ {self.table.sheet or self.table.encoding}')
            self.reset_mapping()
        self.app.run_job('表を読み込んでいます…', work, done)

    def preview(self):
        if not self.table or self.app.job:
            return
        try:
            actor = registrant_id(self.app.actor.get())
            self.app.actor.set(actor)
            self.entries = preview_import(self.table, self.kind(), {key: widget.get() for key, widget in self.mapping.items()}, self.app.snapshot, self.app.catalog, actor)
            self.preview_actor = actor
            self.tree.delete(*self.tree.get_children())
            for index, entry in enumerate(self.entries):
                data = entry.data
                summary = data.get('label', '')
                if entry.kind == 'measurement' and data:
                    sample = self.app.snapshot.current(data['sample_id'])['data']
                    summary = sample['label'] + ' / ' + sample['creator'] + ' — ' + data['method'] + ' / ' + data['metric']
                detail = entry.detail + (f' ／ 数値{len(data["values"])}点：' + ', '.join(data['values'][:6]) if 'values' in data else '')
                self.tree.insert('', 'end', iid=str(index), values=(entry.row, entry.status, summary, detail))
            ready = sum(entry.status == '登録可能' for entry in self.entries)
            errors = sum(entry.status == '要修正' for entry in self.entries)
            skipped = sum(entry.status == '取込済み' for entry in self.entries)
            self.status.configure(text=f'登録可能 {ready}件 ／ 要修正 {errors}件 ／ 取込済み {skipped}件。要修正がある間は保存しません。')
            self.save_button.configure(state='normal' if ready and not errors else 'disabled')
        except (StoreError, ValueError) as exc:
            messagebox.showerror('入力を確認してください', str(exc), parent=self)

    def save(self):
        if self.app.job or not self.entries:
            return
        if self.preview_actor != self.app.actor.get().strip().lower():
            self.invalidate()
            messagebox.showinfo('プレビューし直してください', '登録者IDが変わりました。もう一度内容を確認してください。', parent=self)
            return
        entries, store, actor = tuple(self.entries), self.app.store, self.preview_actor
        self.save_button.configure(state='disabled')
        def done(result):
            completed, error = result
            self.entries = []
            self.status.configure(text=error or f'{len(completed)}件をPCに保存しました。主画面の「まとめて送信」でNASへ送れます。')
            if error:
                messagebox.showwarning('途中で停止しました', error, parent=self)
            self.app.refresh(remote=False)
        self.app.run_job('確認した記録を順に保存しています…', lambda: commit_import(entries, store, actor), done)

    def template(self):
        path = filedialog.asksaveasfilename(parent=self, title='記入用ひな形を新しいファイルへ保存',
            initialfile='LabNote-測定登録.xlsx' if self.kind() == 'measurement' else 'LabNote-試料登録.xlsx',
            defaultextension='.xlsx', filetypes=[('Excel', '*.xlsx'), ('CSV', '*.csv')])
        if path:
            try:
                export_template(self.kind(), path)
                messagebox.showinfo('ひな形を保存しました', '記入例を実データへ置き換えてから取り込んでください。', parent=self)
            except (OSError, StoreError, ValueError) as exc:
                messagebox.showerror('保存できませんでした', str(exc), parent=self)
