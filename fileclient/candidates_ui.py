"""Add a canonical candidate from a form without exposing storage controls."""
import tkinter as tk
from tkinter import messagebox, ttk
from .runtime import save_catalog
from .store import StoreError


class CandidateDialog(tk.Toplevel):
    def __init__(self, form, key):
        super().__init__(form)
        self.form, self.key = form, key
        self.transient(form)
        self.title('選択肢を追加')
        self.resizable(False, False)
        names = {'material': '材料', 'alloy': '合金種', 'polishing': '研磨条件', 'method': '方法', 'metric': '測定項目', 'unit': '単位'}
        self.category = {'material': 'material', 'alloy': 'alloys', 'polishing': 'polishing',
                         'method': 'treatments' if form.kind == 'treatment' else 'method', 'metric': 'methods', 'unit': 'units'}[key]
        dependency = {'alloy': 'material', 'metric': 'method', 'unit': 'metric'}.get(key)
        self.parent_value = form.fields[dependency].get() if dependency else ''
        ttk.Label(self, text=names[key] + 'の正式な表記を追加します。' + (f'\n分類：{self.parent_value or "先に分類を選んでください"}' if dependency else ''), padding=14).pack(fill='x')
        self.value = ttk.Entry(self, width=42)
        self.value.pack(padx=14, pady=6)
        ttk.Label(self, text='このPCの候補に追加します。既存の記録は変更しません。', padding=14).pack()
        ttk.Button(self, text='追加して選択', command=self.save).pack(pady=(0, 14))
        self.protocol('WM_DELETE_WINDOW', self.close)
        self.grab_set()
        self.value.focus_set()

    def close(self):
        self.destroy()
        self.form.grab_set()

    def save(self):
        if self.form.app.job:
            return
        try:
            catalog = self.form.app.catalog.add(self.category, self.value.get(), self.parent_value)
            save_catalog(catalog)
            self.form.app.catalog = catalog
            if hasattr(self.form.app.store, 'catalog'):
                self.form.app.store.catalog = catalog
            context = {key: widget.get() for key, widget in self.form.fields.items()}
            options = catalog.options(self.form.kind, self.key, context)
            self.form.fields[self.key].configure(values=[''] + list(options))
            # Catalog.add validates and normalizes the newly appended candidate.
            self.form.fields[self.key].set(options[-1])
            self.form.update_choices(self.key)
            self.close()
        except (StoreError, OSError, ValueError) as exc:
            messagebox.showerror('追加できませんでした', str(exc), parent=self)
