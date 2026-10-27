"""Infrequent handover tasks, kept away from everyday registration controls."""
import json
import os
import sys
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from .runtime import load_deployment, save_catalog, save_deployment, validate_deployment
from .session import DesktopSession
from .store import StoreError


class MaintenanceDialog(tk.Toplevel):
    CATEGORIES = {'材料区分': 'material', '合金種': 'alloys', '研磨条件': 'polishing',
                  '処理方法': 'treatments', '測定方法': 'method', '測定項目': 'methods', '単位': 'units'}

    def __init__(self, app):
        super().__init__(app.root)
        self.app = app
        self.title('管理・引き継ぎ設定')
        self.transient(app.root)
        self.geometry('820x640')
        self.minsize(740, 580)
        ttk.Label(self, text='担当者向け設定です。通常の登録・閲覧では変更する必要はありません。',
                  wraplength=770, padding=16).pack(fill='x')
        self.enabled = tk.BooleanVar(value=False)
        ttk.Checkbutton(self, text='設定を変更する（保存先と候補が利用者の表示・登録内容に影響することを確認した）',
                        variable=self.enabled, command=self.toggle).pack(anchor='w', padx=16, pady=(0, 10))
        notebook = ttk.Notebook(self)
        notebook.pack(fill='both', expand=True, padx=16, pady=8)
        choices, storage = ttk.Frame(notebook, padding=14), ttk.Frame(notebook, padding=14)
        notebook.add(choices, text='選択肢の管理')
        notebook.add(storage, text='共有先・配布')
        ttk.Label(choices, text='候補のない値を登録画面で自由入力させず、ここで正式な表記を追加します。\n追加はこのPCに反映します。全員への反映には配布設定を更新して配ってください。',
                  wraplength=720).pack(fill='x', pady=(0, 12))
        self.category = ttk.Combobox(choices, state='readonly', values=list(self.CATEGORIES), width=24)
        self.category.set('合金種')
        self.category.pack(anchor='w', pady=5)
        self.category.bind('<<ComboboxSelected>>', self.update_parent)
        ttk.Label(choices, text='分類（合金種は材料、測定項目は方法、単位は測定項目を選択）').pack(anchor='w', pady=5)
        self.parent_choice = ttk.Combobox(choices, state='readonly', width=42)
        self.parent_choice.pack(anchor='w')
        self.parent_choice.bind('<<ComboboxSelected>>', self.show_candidates)
        self.candidates = ttk.Label(choices, wraplength=720)
        self.candidates.pack(fill='x', pady=12)
        ttk.Label(choices, text='追加する正式表記').pack(anchor='w')
        self.value = ttk.Entry(choices, width=50)
        self.value.pack(anchor='w', pady=5)
        self.add_button = ttk.Button(choices, text='このPCに候補を追加', command=self.add)
        self.add_button.pack(anchor='w', pady=8)
        ttk.Label(choices, text='大文字・全角などだけが異なる候補の重複は拒否します。既存の記録は書き換えません。',
                  wraplength=720).pack(fill='x', pady=8)
        self.path_label = ttk.Label(storage, text=f'現在の保存先：\n{app.store.root}', wraplength=720)
        self.path_label.pack(fill='x', pady=(0, 12))
        self.bind('<FocusIn>', lambda event: self.path_label.configure(text=f'現在の保存先：\n{app.store.root}'))
        ttk.Label(storage, text='変更すると表示する共有データが変わります。以前の未送信分は元の共有先用としてPC内に残ります。\n通常利用者は変更せず、配布時に設定された共有先を使います。',
                  wraplength=720).pack(fill='x', pady=8)
        self.open_button = ttk.Button(storage, text='既存の共有先を選び直す…', command=app.pick_store)
        self.open_button.pack(anchor='w', pady=5)
        self.new_button = ttk.Button(storage, text='担当者が専用保存先を新規作成…', command=app.new_store)
        self.new_button.pack(anchor='w', pady=5)
        self.rebuild_button = ttk.Button(storage, text='検索データを作り直す（記録は変更しません）', command=self.rebuild)
        self.rebuild_button.pack(anchor='w', pady=5)
        ttk.Separator(storage).pack(fill='x', pady=14)
        other_label = 'Windows側の共有先パス' if sys.platform == 'darwin' else 'Mac側の共有先パス（Macで確認後に設定。推測で入力しない）'
        ttk.Label(storage, text=other_label, wraplength=720).pack(anchor='w')
        self.other_path = ttk.Entry(storage, width=75)
        try:
            deployment = load_deployment()
        except (OSError, StoreError, ValueError):
            deployment = {}
        self.other_path.insert(0, deployment.get('windows_path' if sys.platform == 'darwin' else 'mac_path', ''))
        self.other_path.pack(fill='x', pady=5)
        self.save_button = ttk.Button(storage, text='このPCに設定を保存', command=self.save_local)
        self.save_button.pack(anchor='w', pady=5)
        self.export_button = ttk.Button(storage, text='メンバー用の配布設定を書き出す…', command=self.export)
        self.export_button.pack(anchor='w', pady=8)
        ttk.Label(storage, text='初回は同梱設定をPC内へ取り込みます。その後はPC内の設定を優先します。\nこのPCの設定変更は上の保存ボタンから行います。配布設定は新規利用者向けです。資格情報は含めません。',
                  wraplength=720).pack(fill='x', pady=5)
        ttk.Button(self, text='閉じる', command=self.destroy).pack(anchor='e', padx=16, pady=10)
        self.update_parent()
        self.toggle()
        self.grab_set()

    def toggle(self):
        state = 'normal' if self.enabled.get() else 'disabled'
        for widget in [self.add_button, self.open_button, self.new_button, self.export_button, self.save_button, self.value, self.other_path, self.rebuild_button]:
            widget.configure(state=state)
        if self.app.production:
            self.new_button.configure(state='disabled')

    def rebuild(self):
        if not self.enabled.get() or self.app.job:
            return
        if not hasattr(self.app.store, 'rebuild_index'):
            return
        def done(result):
            messagebox.showinfo('検索データ', '原本から作り直しました。記録や添付は変更していません。', parent=self)
            self.app.refresh(remote=False)
        self.app.run_job('PC内の検索データを原本から作り直しています…', self.app.store.rebuild_index, done)

    def update_parent(self, event=None):
        category = self.CATEGORIES[self.category.get()]
        data = self.app.catalog.data
        if category in {'alloys', 'methods'}:
            parents = list(data[category])
        elif category == 'units':
            parents = list(dict.fromkeys(item for values in data['methods'].values() for item in values))
        else:
            parents = []
        self.parent_choice.configure(values=parents)
        self.parent_choice.set(parents[0] if parents else '')
        self.show_candidates()

    def show_candidates(self, event=None):
        category = self.CATEGORIES[self.category.get()]
        data = self.app.catalog.data
        if category in {'alloys', 'methods', 'units'}:
            values = data[category].get(self.parent_choice.get(), [])
        elif category in {'material', 'method'}:
            values = list(data['alloys' if category == 'material' else 'methods'])
        else:
            values = data[category]
        self.candidates.configure(text='現在の候補：' + ('、'.join(values) or 'なし'))

    def add(self):
        if not self.enabled.get() or self.app.job:
            return
        try:
            catalog = self.app.catalog.add(self.CATEGORIES[self.category.get()], self.value.get(), self.parent_choice.get())
            save_catalog(catalog)
            self.app.catalog = catalog
            if isinstance(self.app.store, DesktopSession):
                self.app.store.catalog = catalog
            self.value.delete(0, 'end')
            self.show_candidates()
            messagebox.showinfo('候補を追加しました', 'このPCに反映しました。全員へ反映するには「共有先・配布」で配布設定を書き出し、更新した設定を配ってください。', parent=self)
        except (OSError, StoreError, ValueError) as exc:
            messagebox.showerror('候補を追加できませんでした', str(exc), parent=self)

    def configuration(self):
        current, other = str(self.app.store.root), self.other_path.get().strip()
        data = {'version': 1, 'mode': 'production' if self.app.production else 'development', 'store_id': self.app.store.store_id,
                'windows_path': other if sys.platform == 'darwin' else current,
                'mac_path': current if sys.platform == 'darwin' else other,
                'catalog': self.app.catalog.data}
        return validate_deployment(data)

    def save_local(self):
        if not self.enabled.get() or self.app.job:
            return
        if not isinstance(self.app.store, DesktopSession):
            messagebox.showinfo('共有先の準備が必要です', '先に専用保存先を設定してください。', parent=self)
            return
        try:
            bound = load_deployment() if self.app.production else None
            save_deployment(self.configuration(), bound=bound)
            self.app.remember_preferences()
            messagebox.showinfo('設定を保存しました', 'このPCに保存しました。exeや.appの場所を変えても設定を引き続き使えます。', parent=self)
        except (OSError, StoreError, ValueError) as exc:
            messagebox.showerror('設定を保存できませんでした', str(exc), parent=self)

    def export(self):
        if not self.enabled.get() or self.app.job:
            return
        if not isinstance(self.app.store, DesktopSession):
            messagebox.showinfo('共有先の準備が必要です', '先に担当者が専用保存先を設定してください。デモはメンバー用の共有先として配布できません。', parent=self)
            return
        destination = filedialog.asksaveasfilename(parent=self, title='配布設定を新しいファイルに保存',
                    initialfile='labnote-deployment.json', defaultextension='.json')
        if not destination:
            return
        try:
            data = self.configuration()
            with Path(destination).open('x', encoding='utf-8') as stream:
                json.dump(data, stream, ensure_ascii=False, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            messagebox.showinfo('配布設定', '保存しました。アプリと同じフォルダへ同梱して配布してください。\n既に別の共有先を使っているPCは、担当者が設定を確認してください。', parent=self)
        except (OSError, StoreError, ValueError) as exc:
            messagebox.showerror('配布設定を書き出せませんでした', f'既存ファイルは上書きしません。別のフォルダか新しい名前で保存してください。\n\n{exc}', parent=self)
