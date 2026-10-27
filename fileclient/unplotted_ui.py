"""Read-only list of measurements no chart can show, with the reason for each."""
import tkinter as tk
from tkinter import ttk

from .comparison import unplotted_measurements

GUIDANCE = {
    '競合・要確認': '同じ記録に競合する訂正があります。訂正を止めて「保存状態・競合」と「履歴」を確認し、'
                '担当者へ連絡してください。解決のための画面はまだありません。',
    '関連試料不明': '関連付けた試料が見つかりません。試料が競合しているか、ごみ箱にある可能性があります。'
                '試料の側を確認してください。',
    '数値なし': '数値が入っていません。訂正で数値を追加すると図に載ります。数値なしの記録として残すこともできます。',
    '項目・単位なし': '測定項目または単位が入っていません。訂正で両方を入れると図に載ります。',
    '表示できない状態': 'ごみ箱ではない理由で表示できない状態です。改訂のつながりをたどれない可能性があります。'
                '「保存状態・競合」と「履歴」を確認し、担当者へ連絡してください。',
}


class UnplottedDialog(tk.Toplevel):
    def __init__(self, app):
        super().__init__(app.root)
        self.app = app
        self.transient(app.root)
        self.title('図に乗らない記録')
        self.geometry('900x480')
        actor = app.actor.get().strip()
        self.records = unplotted_measurements(app.snapshot, actor)

        ttk.Label(self, text='図に乗らない記録', font=('Yu Gothic UI', 15, 'bold'),
                  padding=(16, 14, 16, 0)).pack(anchor='w')
        ttk.Label(self, padding=(16, 4, 16, 8), wraplength=860,
                  text=(f'登録者ID「{actor}」の記録を表示しています。' if actor else
                        '登録者IDが未入力のため、全員の記録を表示しています。') +
                       '開いた時点のPC内データ（未送信を含む）が対象です。').pack(anchor='w')

        columns = [('measured_on', '測定日', 100), ('method', '方法・項目', 220),
                   ('measurer', '測定者', 100), ('state', '保存状態', 120), ('reason', '理由', 130)]
        frame = ttk.Frame(self)
        frame.pack(fill='both', expand=True, padx=16)
        self.table = ttk.Treeview(frame, columns=[name for name, _title, _width in columns],
                                  show='headings', selectmode='browse', height=12)
        for name, title, width in columns:
            self.table.heading(name, text=title)
            self.table.column(name, width=width, anchor='w')
        scroll = ttk.Scrollbar(frame, orient='vertical', command=self.table.yview)
        self.table.configure(yscrollcommand=scroll.set)
        self.table.pack(side='left', fill='both', expand=True)
        scroll.pack(side='right', fill='y')
        self.table.bind('<<TreeviewSelect>>', self.show_guidance)

        self.rows = {}
        for record in self.records:
            item, data = record['item'], record['item']['data']
            self.rows[record['id']] = record
            self.table.insert('', 'end', iid=record['id'], values=(
                data.get('measured_on', ''),
                (data.get('method', '') + ' / ' + data.get('metric', '')).strip(' /'),
                item['registrant'] if not data.get('measurer') else data['measurer'],
                app.record_state(item), record['reason']))

        self.detail = ttk.Label(self, padding=(16, 10), wraplength=860,
                                text=self.summary())
        self.detail.pack(fill='x')
        buttons = ttk.Frame(self)
        buttons.pack(fill='x', padx=16, pady=(0, 14))
        self.focus_button = ttk.Button(buttons, text='主画面でこの記録を表示',
                                       command=self.focus_record, state='disabled')
        self.focus_button.pack(side='left')
        ttk.Button(buttons, text='閉じる', command=self.destroy).pack(side='right')
        self.grab_set()

    def summary(self):
        if not self.records:
            return 'すべての記録が図に載ります。'
        counts = {}
        for record in self.records:
            counts[record['reason']] = counts.get(record['reason'], 0) + 1
        return ('図に乗らない記録が' + str(len(self.records)) + '件あります（' +
                '、'.join(f'{reason} {count}件' for reason, count in counts.items()) +
                '）。行を選ぶと対処方法を表示します。')

    def show_guidance(self, event=None):
        selection = self.table.selection()
        if not selection:
            self.detail.configure(text=self.summary())
            self.focus_button.configure(state='disabled')
            return
        record = self.rows[selection[0]]
        self.detail.configure(text=record['reason'] + '：' + GUIDANCE[record['reason']])
        self.focus_button.configure(state='normal')

    def focus_record(self):
        selection = self.table.selection()
        if not selection:
            return
        record = self.rows[selection[0]]
        self.app.focus_sample_id = record['item']['data'].get('sample_id')
        self.app.focus_measurement_id = record['id']
        self.destroy()
        # 試料が競合・ごみ箱にあると一覧へ出ないため、主画面の案内に任せる。
        self.app.refresh(remote=False)
