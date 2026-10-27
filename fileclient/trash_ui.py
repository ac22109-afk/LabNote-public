"""Explicit, reversible archival. No file removal or automatic expiry."""
import tkinter as tk
from tkinter import messagebox, ttk
from .store import UNKNOWN, StoreError, registrant_id


def record_name(item):
    return item['data'].get('label') or item['data'].get('method', '') + ' / ' + item['data'].get('metric', '')


class VisibilityDialog(tk.Toplevel):
    def __init__(self, app, item, *, deleted, attachment=None, callback=None):
        super().__init__(app.root)
        self.app, self.item, self.deleted, self.attachment, self.callback = app, item, deleted, attachment, callback
        self.transient(app.root)
        self.title('ごみ箱へ移す前の確認' if deleted else 'ごみ箱から復元')
        self.resizable(False, False)
        self.confirmed = tk.BooleanVar(value=False)
        self.attempted = False
        self.keyword = tk.StringVar()
        name = attachment['name'] if attachment else record_name(item)
        ttk.Label(self, text=('ごみ箱へ移す対象：' if deleted else '復元する対象：')+'\n'+name, padding=16, wraplength=620).pack(fill='x')
        counts = ''
        if item['kind'] == 'sample' and not attachment:
            related = [entry for kind in ('measurement', 'treatment') for entry in app.snapshot.of_kind(kind, include_deleted=True) if entry['data']['sample_id'] == item['id']]
            counts = f'関連する測定 {sum(entry["kind"] == "measurement" for entry in related)}件・処理 {sum(entry["kind"] == "treatment" for entry in related)}件・添付 {len(item["attachments"])+sum(len(entry["attachments"]) for entry in related)}件も、試料と一緒に表示を切り替えます。\n'
        ttk.Label(self, text=counts+'元の記録・添付ファイルは消しません。ごみ箱から復元できます。\n共有済みの記録への操作は、接続中に共有先へ保存します。', padding=(16, 0, 16, 12), wraplength=620).pack(fill='x')
        ttk.Label(self, text='理由（必須・500文字以内）', padding=(16, 0)).pack(anchor='w')
        self.reason = ttk.Entry(self, width=68)
        self.reason.pack(padx=16, pady=8, fill='x')
        if not deleted:
            self.reason.insert(0, '誤削除の復元')
        ttk.Checkbutton(self, text='対象と関連データへの影響を確認しました\n共有済み記録は、利用者が1.2.0以降へ更新済みであることも確認しました', variable=self.confirmed, command=self.update_button).pack(anchor='w', padx=16, pady=8)
        if deleted:
            ttk.Label(self, text='誤操作防止のため「削除」と入力してください', padding=(16, 0)).pack(anchor='w')
            ttk.Entry(self, textvariable=self.keyword, width=22).pack(anchor='w', padx=16, pady=8)
        self.execute = ttk.Button(self, text='ごみ箱へ移す' if deleted else '復元する', state='disabled', command=self.apply)
        self.execute.pack(side='right', padx=16, pady=16)
        ttk.Button(self, text='キャンセル', command=self.destroy).pack(side='left', padx=16, pady=16)
        self.keyword.trace_add('write', lambda *args: self.update_button())
        self.reason.bind('<KeyRelease>', lambda event: self.update_button())
        self.grab_set()

    def update_button(self):
        allowed = not self.attempted and self.confirmed.get() and 0 < len(self.reason.get().strip()) <= 500 and (not self.deleted or self.keyword.get().strip() == '削除')
        self.execute.configure(state='normal' if allowed else 'disabled')

    def apply(self):
        if self.app.job or str(self.execute.cget('state')) == 'disabled':
            return
        try:
            actor = registrant_id(self.app.actor.get())
            if not self.app.store.can_edit(self.item, actor):
                raise StoreError('担当者または登録者のIDで操作してください。')
        except StoreError as exc:
            messagebox.showerror('操作できませんでした', str(exc), parent=self)
            return
        store, item, deleted, reason = self.app.store, self.item, self.deleted, self.reason.get().strip()
        attachment_id = self.attachment['id'] if self.attachment else None
        self.attempted = True
        self.execute.configure(state='disabled')
        self.grab_release()
        self.withdraw()
        def done(result):
            self.destroy()
            if self.callback:
                self.callback()
            self.app.refresh(remote=False)
        def error(exc):
            self.deiconify()
            self.grab_set()
            # A lost acknowledgement must be reloaded before any repeat action.
            self.app.show_error(StoreError(str(exc)+'\n\nこの確認画面を閉じ、主画面で再読込して保存状態を確認してください。'))
        self.app.run_job('ごみ箱の状態を保存しています…', lambda: store.set_visibility(item, actor, deleted, reason, attachment_id=attachment_id), done, error)


class TrashDialog(tk.Toplevel):
    def __init__(self, app):
        super().__init__(app.root)
        self.app = app
        self.title('ごみ箱 — 記録・添付の復元')
        self.geometry('1080x620')
        self.transient(app.root)
        ttk.Label(self, text='元ファイルは保持しています。親試料の削除に連動している記録は、先に親試料を復元してください。\n他のPCの変更を取り込む場合は、閉じて主画面の「再読込」を押してください。', padding=14).pack(fill='x')
        frame = ttk.Frame(self)
        frame.pack(fill='both', expand=True, padx=14)
        self.tree = ttk.Treeview(frame, columns=('kind', 'name', 'actor', 'date', 'reason'), show='headings')
        for key, label, width in [('kind', '対象', 85), ('name', '名前', 230), ('actor', '操作した人', 95), ('date', '日時', 180), ('reason', '理由・状態', 350)]:
            self.tree.heading(key, text=label)
            self.tree.column(key, width=width)
        self.tree.pack(side='left', fill='both', expand=True)
        scroll = ttk.Scrollbar(frame, command=self.tree.yview)
        scroll.pack(side='right', fill='y')
        self.tree.configure(yscrollcommand=scroll.set)
        ttk.Button(self, text='選択した記録・添付を復元…', command=self.restore).pack(anchor='e', padx=14, pady=14)
        self.entries = {}
        snapshot = app.snapshot
        for heads in snapshot.heads.values():
            for item in heads:
                if snapshot.hidden(item):
                    status = snapshot.status(item)
                    self.add(item, None, status,
                             '改訂のつながりをたどれません（要確認）' if status['state'] == UNKNOWN
                             else '親試料または競合改訂の削除に連動' if status['state'] != 'deleted' else '')
                for attachment in item['attachments']:
                    status = snapshot.status(item, attachment['id'])
                    if status['state'] == 'deleted':
                        self.add(item, attachment, status, '')

    def add(self, item, attachment, status, note):
        key = str(len(self.entries))
        self.entries[key] = item, attachment, note
        self.tree.insert('', 'end', iid=key, values=('添付' if attachment else {'sample': '試料', 'measurement': '測定', 'treatment': '処理'}[item['kind']],
            attachment['name'] if attachment else record_name(item), status.get('actor', ''), status.get('at', ''), note or status.get('reason', '')))

    def restore(self):
        selected = self.tree.selection()
        if not selected or self.app.job:
            return
        item, attachment, note = self.entries[selected[0]]
        if note or (attachment and self.app.snapshot.hidden(item)):
            messagebox.showinfo('先に親を復元してください', '記録または親試料をごみ箱から復元してから操作してください。', parent=self)
            return
        try:
            self.app.snapshot.current(item['id'], include_deleted=True)
            VisibilityDialog(self.app, item, deleted=False, attachment=attachment, callback=self.destroy)
        except StoreError as exc:
            self.app.show_error(exc)


class TreatmentPicker(tk.Toplevel):
    def __init__(self, app, choices):
        super().__init__(app.root)
        self.transient(app.root)
        self.title('削除する処理履歴を選択')
        ttk.Label(self, text='この試料の処理履歴から対象を選びます。次の画面で確認します。', padding=14).pack()
        self.choice = ttk.Combobox(self, state='readonly', width=80, values=[f'{index+1}: {item["data"]["method"]} / {item["data"]["temperature"]}℃ / {item["data"]["duration"]}{item["data"]["duration_unit"]} / {item["data"]["notes"][:40]}' for index, item in enumerate(choices)])
        self.choice.pack(padx=14, pady=8)
        def selected():
            if self.choice.current() >= 0 and not app.job:
                item = choices[self.choice.current()]
                self.destroy()
                VisibilityDialog(app, item, deleted=True)
        ttk.Button(self, text='この処理の削除確認へ', command=selected).pack(pady=14)
        self.grab_set()
