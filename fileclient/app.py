import argparse
import gc
import json
import os
import re
import statistics
import sys
import tkinter as tk
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from decimal import Decimal
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk

from .store import (CommitUnverifiedError, FileStore, StoreError, export_csv, identifier,
                    normalized, registrant_id)
from .session import DesktopSession
from .catalog import Catalog
from .index import LocalSession
from .runtime import demo_directory, install_error_dialog, load_preferences, save_preferences, load_deployment, save_deployment, load_catalog, startup_target
from . import __version__
from .attachments import original_filename, original_extension, export_destination

SCOPE_MINE = '自分の試料'
SCOPE_ALL = '全員の試料'

PROJECT = Path(__file__).resolve().parent.parent
DEMO = demo_directory()


class RecordDialog(tk.Toplevel):
    def __init__(self, app, kind, existing=None, sample=None):
        super().__init__(app.root)
        self.app, self.kind, self.existing, self.sample = app, kind, existing, sample
        self.files = []
        title = {'sample': '試料', 'measurement': '測定', 'treatment': '処理'}[kind]
        self.title(f'{title}の' + ('訂正' if existing else '登録'))
        self.transient(app.root)
        self.resizable(True, True)
        self.columnconfigure(1, weight=1)
        self.fields = {}
        data = existing['data'] if existing else ({'material': 'アルミニウム合金'} if kind == 'sample' and 'アルミニウム合金' in app.catalog.data['alloys'] else {})
        row = 0
        if sample:
            ttk.Label(self, text=f'対象試料: {sample["data"]["label"]} / {sample["data"]["creator"]}').grid(row=row, column=0, columnspan=2, sticky='w', padx=14, pady=10)
            row += 1
        definitions = {
            'sample': [('label', '各自の試料ID *'), ('creator', '作製者 *'), ('material', '材料 *'),
                       ('alloy', '合金種 *'), ('polishing', '研磨条件')],
            'measurement': [('measurer', '測定者 *'), ('method', '測定方法 *'), ('metric', '測定項目'),
                            ('unit', '単位'), ('measured_on', '測定日 *（YYYY-MM-DD）')],
            'treatment': [('method', '処理方法 *'), ('temperature', '温度（℃）'), ('duration', '時間'), ('duration_unit', '時間単位（h / ks）')],
        }
        for key, label in definitions[kind]:
            ttk.Label(self, text=label).grid(row=row, column=0, sticky='w', padx=14, pady=5)
            default = app.actor.get().strip() if key in {'creator', 'measurer'} else date.today().isoformat() if key == 'measured_on' else 'h' if key == 'duration_unit' else ''
            if not existing and key in {'creator', 'measurer'} and default:
                try:
                    default = registrant_id(default)
                except StoreError:
                    pass
            options = app.catalog.options(kind, key, data)
            if options is not None:
                value = str(data.get(key, default))
                choices = [''] + list(options)
                if existing and value not in choices:
                    choices.append(value)
                entry = ttk.Combobox(self, width=46, values=choices, state='readonly')
                entry.set(value)
            else:
                entry = ttk.Entry(self, width=48)
                entry.insert(0, str(data.get(key, default)))
            if existing and key in {'creator', 'measurer'}:
                entry.configure(state='readonly')
            entry.grid(row=row, column=1, sticky='ew', padx=14, pady=5)
            self.fields[key] = entry
            if options is not None and key != 'duration_unit':
                ttk.Button(self, text='候補を追加…', command=lambda selected=key: self.add_candidate(selected)).grid(row=row, column=2, padx=(0, 14), pady=5)
            if key in {'material', 'method', 'metric'} and options is not None:
                entry.bind('<<ComboboxSelected>>', lambda event, changed=key: self.update_choices(changed))
            row += 1
            if key == 'label':
                ttk.Label(self, text='例：200d-24h-10k_01', foreground='#777777').grid(row=row, column=1, sticky='w', padx=14)
                row += 1
        self.solution = tk.BooleanVar(value=data.get('solution_treated', False))
        if kind == 'sample':
            ttk.Checkbutton(self, text='溶体化処理あり', variable=self.solution).grid(row=row, column=1, sticky='w', padx=14)
            row += 1
        self.values = None
        if kind == 'measurement':
            ttk.Label(self, text='数値（省略可）\n改行・カンマ・タブ区切り').grid(row=row, column=0, sticky='nw', padx=14, pady=8)
            self.values = tk.Text(self, width=48, height=5, wrap='word')
            self.values.insert('1.0', '\n'.join(data.get('values', [])))
            self.values.grid(row=row, column=1, sticky='ew', padx=14, pady=8)
            row += 1
        ttk.Label(self, text='備考').grid(row=row, column=0, sticky='nw', padx=14, pady=8)
        self.notes = tk.Text(self, width=48, height=4, wrap='word')
        self.notes.insert('1.0', data.get('notes', ''))
        self.notes.grid(row=row, column=1, sticky='ew', padx=14, pady=8)
        row += 1
        self.file_label = ttk.Label(self, text='追加する添付: なし（1件512MiBまで）')
        self.file_label.grid(row=row, column=0, columnspan=2, sticky='w', padx=14, pady=5)
        row += 1
        buttons = ttk.Frame(self)
        buttons.grid(row=row, column=0, columnspan=2, sticky='ew', padx=14, pady=14)
        ttk.Button(buttons, text='添付を選ぶ', command=self.pick_files).pack(side='left')
        ttk.Button(buttons, text='選択を解除', command=self.clear_files).pack(side='left', padx=6)
        self.save_button = ttk.Button(buttons, text=('PCに保存（未送信）' if isinstance(app.store, DesktopSession) else '登録') if not existing else '訂正を追加保存', command=self.save)
        self.save_button.pack(side='right')
        self.verify_button = ttk.Button(buttons, text='保存状況を確認', command=self.verify_save)
        self.unverified = None
        ttk.Label(self, text=('訂正は接続中のみ・共有先へ直接保存します。' if existing and isinstance(app.store, DesktopSession) else '') + '担当者・登録者IDは自己申告です。').grid(row=row + 1, column=0, columnspan=2, padx=14, pady=(0, 12))
        self.grab_set()

    def add_candidate(self, key):
        from .candidates_ui import CandidateDialog
        CandidateDialog(self, key)

    def update_choices(self, changed):
        dependencies = {'material': ['alloy'], 'method': ['metric', 'unit'], 'metric': ['unit']}
        for key in dependencies.get(changed, []):
            if key not in self.fields:
                continue
            widget = self.fields[key]
            widget.set('')
            context = {name: field.get() for name, field in self.fields.items()}
            widget.configure(values=[''] + self.app.catalog.options(self.kind, key, context))

    def pick_files(self):
        chosen = filedialog.askopenfilenames(parent=self, title='保存する添付を選択（ファイルは実行しません）')
        self.files.extend(path for path in chosen if path not in self.files)
        self.file_label.configure(text=f'追加する添付: {len(self.files)}件（保存済み添付は維持します）')

    def clear_files(self):
        self.files = []
        self.file_label.configure(text='追加する添付: なし（保存済み添付は維持します）')

    def save(self):
        try:
            actor = registrant_id(self.app.actor.get())
        except StoreError as exc:
            messagebox.showerror('登録者IDを確認してください', str(exc), parent=self)
            return
        self.app.actor.set(actor)
        data = {key: widget.get() for key, widget in self.fields.items()}
        data['notes'] = self.notes.get('1.0', 'end-1c')
        if self.kind == 'sample':
            data['solution_treated'] = self.solution.get()
        else:
            data['sample_id'] = self.existing['data']['sample_id'] if self.existing else self.sample['id']
        if self.values:
            raw = self.values.get('1.0', 'end-1c').strip()
            data['values'] = re.split(r'[\s,]+', raw) if raw else []
        try:
            data = self.app.catalog.validate(self.kind, data, self.existing['data'] if self.existing else None)
        except StoreError as exc:
            messagebox.showerror('入力を確認してください', str(exc), parent=self)
            return
        kwargs = {'files': tuple(self.files)}
        if self.existing:
            kwargs.update(record_id=self.existing['id'], base_revision=self.existing['revision_id'])
        store = self.app.store
        self.grab_release()
        self.withdraw()

        def done(item):
            self.app.remember_preferences()
            self.app.focus_sample_id = item['id'] if item['kind'] == 'sample' else item['data']['sample_id']
            self.app.focus_measurement_id = item['id'] if item['kind'] == 'measurement' else None
            if self.app.query.get().strip() and hasattr(store, 'search_samples'):
                visible = {record_id for record_id, revision_id in store.search_samples(self.app.query.get())}
                if self.app.focus_sample_id not in visible:
                    self.app.query.set('')
            self.destroy()
            if isinstance(store, DesktopSession) and store.notices:
                self.app.show_text('保存結果', '\n\n'.join(store.notices))
                store.notices.clear()
            self.app.refresh(remote=False)

        def error(exc):
            self.deiconify()
            self.grab_set()
            if isinstance(exc, CommitUnverifiedError):
                self.app.queue_status.set('保存確認が必要です。未送信数は再読込後に確認してください。')
                self.unverified = (exc.record_id, exc.revision_id)
                self.save_button.configure(state='disabled')
                self.verify_button.pack(side='right', padx=5)
            self.app.show_error(exc)

        self.app.run_job('保存しています。完了するまで閉じずにお待ちください。',
                         lambda: store.save(self.kind, data, actor, **kwargs), done, error)

    def verify_save(self):
        if not self.unverified:
            return
        record_id, revision_id = self.unverified
        store = self.app.store
        def done(item):
            self.grab_release()
            self.destroy()
            self.app.refresh()
        self.app.run_job('未確認の保存を読み直しています（再送はしません）…',
                         lambda: store.read_revision(record_id, revision_id), done)


class App:
    def __init__(self, root, store, *, actor='', remember_settings=False, catalog=None, production=False):
        self.root, self.store = root, store
        self.production = production
        self.bound_store_id = store.store_id if production else None
        from tkinter import font
        from .font_requirements import inspect_fonts
        self.font_status = inspect_fonts(font.families(root))
        self.catalog = catalog or Catalog()
        if isinstance(store, DesktopSession):
            store.catalog = self.catalog
        self.remember_settings = remember_settings
        install_error_dialog(root, messagebox.showerror)
        self.snapshot = None
        self.job = None
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='labnote-files')
        root.title(f'LabNote {__version__} — 研究記録')
        root.geometry('1180x780')
        root.minsize(900, 620)
        root.protocol('WM_DELETE_WINDOW', self.close)
        menu = tk.Menu(root)
        help_menu = tk.Menu(menu, tearoff=False)
        help_menu.add_command(label='使い方', command=lambda: self.show_text('使い方', '通常は保存先の設定を変えずに利用します。\n新規記録はPC内へ保存し、NAS接続後に「まとめて送信」します。\n合金種・方法の候補は登録画面の「候補を追加…」からこのPCへ追加できます。\n多数の測定は「Excel／CSV一括登録」で列を指定し、確認後に保存できます。\n「分極曲線」は元の表から作図するツールで、読み込みだけでは測定を登録しません。\n共有先・配布設定の変更は「管理・引き継ぎ設定」にあります。'))
        help_menu.add_command(label='必要フォントの確認…', command=self.fonts)
        help_menu.add_separator()
        help_menu.add_command(label='管理・引き継ぎ設定…', command=self.maintenance)
        menu.add_cascade(label='ヘルプ', menu=help_menu)
        root.configure(menu=menu)
        style = ttk.Style(root)
        style.configure('Treeview', rowheight=28)
        style.configure('Title.TLabel', font=('Yu Gothic UI', 17, 'bold'))
        top = ttk.Frame(root, padding=14)
        top.pack(fill='x')
        ttk.Label(top, text='LabNote', style='Title.TLabel').pack(side='left')
        ttk.Label(top, text='  PCで登録 → 共有先へまとめて送信' + ('／本番共有データ' if production else '')).pack(side='left')
        self.location = ttk.Label(root, text=self.location_text(), padding=(14, 0, 14, 8), wraplength=1100)
        self.location.pack(fill='x')
        tools = ttk.Frame(root, padding=(14, 0, 14, 10))
        tools.pack(fill='x')
        ttk.Label(tools, text='登録者ID').pack(side='left')
        try:
            actor = registrant_id(actor) if actor else ''
        except StoreError:
            actor = ''  # Legacy display names remain in records, never become IDs.
        self.actor = tk.StringVar(value=actor)
        self.actor_entry = ttk.Entry(tools, textvariable=self.actor, width=18)
        self.actor_entry.pack(side='left', padx=6)
        self.actor_hint = ttk.Label(self.actor_entry, text='例：zz00001', foreground='#777777', cursor='xterm')
        def update_hint(*args):
            if not self.actor.get() and root.focus_get() != self.actor_entry:
                self.actor_hint.place(x=4, rely=0.5, anchor='w')
            else:
                self.actor_hint.place_forget()
        self.actor_hint.bind('<Button-1>', lambda event: self.actor_entry.focus_set())
        self.actor.trace_add('write', update_hint)
        self.actor_entry.bind('<FocusIn>', update_hint)
        self.actor_entry.bind('<FocusOut>', lambda event: root.after_idle(update_hint))
        update_hint()
        ttk.Label(tools, text='検索').pack(side='left', padx=(18, 4))
        self.query = tk.StringVar()
        search = ttk.Entry(tools, textvariable=self.query, width=24)
        search.pack(side='left', padx=4)
        search.bind('<Return>', lambda event: self.render_samples())
        self.search_timer = None
        self.query.trace_add('write', self.schedule_search)
        ttk.Button(tools, text='絞り込み', command=self.render_samples).pack(side='left')
        ttk.Button(tools, text='再読込', command=self.refresh).pack(side='left', padx=6)
        ttk.Button(tools, text='CSV出力', command=self.csv).pack(side='right')
        self.sync_button = ttk.Button(tools, text='まとめて送信', command=self.sync)
        self.sync_button.pack(side='right', padx=6)
        ttk.Button(tools, text='未送信一覧', command=self.pending).pack(side='right')
        self.queue_status = tk.StringVar(value='PC内の記録を準備しています…')
        ttk.Label(root, textvariable=self.queue_status, padding=(14, 0, 14, 8)).pack(fill='x')
        missing = [item['name'] for item in self.font_status if not item['installed']]
        if missing:
            font_banner = ttk.Frame(root, padding=(14, 0, 14, 8))
            font_banner.pack(fill='x')
            ttk.Label(font_banner, text='未導入の書体：' + '、'.join(missing) + '（代替表示になる場合があります）', wraplength=850).pack(side='left')
            ttk.Button(font_banner, text='フォントの確認', command=self.fonts).pack(side='right')
        feature_tools = ttk.Frame(root, padding=(14, 0, 14, 8))
        feature_tools.pack(fill='x')
        ttk.Button(feature_tools, text='ごみ箱・復元', command=self.trash).pack(side='right')
        self.feature_buttons = []
        for label, command in [('Excel／CSV一括登録', self.bulk_import), ('グラフ比較', self.compare),
                               ('分極曲線', self.polarization), ('図に乗らない記録', self.unplotted)]:
            button = ttk.Button(feature_tools, text=label, command=command)
            button.pack(side='left', padx=(0, 8))
            self.feature_buttons.append(button)
        pane = ttk.Panedwindow(root, orient='horizontal')
        pane.pack(fill='both', expand=True, padx=14)
        left, right = ttk.Frame(pane), ttk.Frame(pane)
        pane.add(left, weight=1)
        pane.add(right, weight=2)
        heading = ttk.Frame(left)
        heading.pack(fill='x', pady=5)
        ttk.Label(heading, text='試料一覧').pack(side='left')
        self.scope = tk.StringVar(value=SCOPE_MINE)
        scope = ttk.Combobox(heading, textvariable=self.scope, state='readonly',
                             width=10, values=[SCOPE_MINE, SCOPE_ALL])
        scope.pack(side='right')
        scope.bind('<<ComboboxSelected>>', lambda event: self.render_samples())
        # 登録者IDを直すと「自分」の範囲が変わるため、検索と同じ間引きで再表示する。
        # 保存のたびに同じIDが入れ直されるので、値が変わったときだけ予約する。
        # そうしないと遅れて走る再描画が、保存直後の選択を消してしまう。
        self.filtered_actor = self.actor.get().strip()
        self.actor.trace_add('write', self.actor_changed)
        self.samples = self.table(left, [('label', '試料ID', 120), ('creator', '作製者', 90), ('alloy', '合金種', 90), ('state', '保存状態', 130)])
        self.samples.bind('<<TreeviewSelect>>', self.select_sample)
        actions = ttk.Frame(left)
        actions.pack(fill='x', pady=8)
        ttk.Button(actions, text='試料登録', command=lambda: self.form('sample')).pack(side='left')
        ttk.Button(actions, text='試料訂正', command=self.edit_sample).pack(side='left', padx=5)
        ttk.Button(actions, text='試料を削除…', command=lambda: self.delete_record('sample')).pack(side='left')
        if not production:
            ttk.Button(actions, text='仮データ', command=self.demo).pack(side='right')
        self.summary = tk.Text(right, height=10, wrap='word', state='disabled', font=('Yu Gothic UI', 10))
        self.summary.pack(fill='x', pady=5)
        ttk.Label(right, text='この試料の測定').pack(anchor='w', pady=(8, 3))
        self.measurements = self.table(right, [('method', '方法・項目', 150), ('measurer', '測定者', 85), ('date', '日付', 100), ('count', '数値点数', 65), ('state', '保存状態', 120)], height=5)
        self.measurements.bind('<<TreeviewSelect>>', self.select_measurement)
        buttons = ttk.Frame(right)
        buttons.pack(fill='x', pady=7)
        delete_menu_button = ttk.Menubutton(buttons, text='削除…')
        delete_menu = tk.Menu(delete_menu_button, tearoff=False)
        delete_menu.add_command(label='選択した測定を削除…', command=lambda: self.delete_record('measurement'))
        delete_menu.add_command(label='処理履歴を選んで削除…', command=self.delete_treatment)
        delete_menu_button.configure(menu=delete_menu)
        delete_menu_button.pack(side='right')
        ttk.Button(buttons, text='測定登録', command=self.new_measurement).pack(side='left')
        ttk.Button(buttons, text='選択測定の訂正', command=self.edit_measurement).pack(side='left', padx=5)
        ttk.Button(buttons, text='処理追加', command=self.new_treatment).pack(side='left')
        ttk.Button(buttons, text='履歴', command=self.history).pack(side='right')
        self.measurement_info = ttk.Label(right, text='測定を選ぶと内容を表示します。', wraplength=680)
        self.measurement_info.pack(fill='x', pady=4)
        ttk.Label(right, text='試料／選択した測定の添付').pack(anchor='w', pady=(8, 3))
        self.attachments = self.table(right, [('name', '元のファイル名', 260), ('extension', '拡張子', 85), ('parent', '対象', 80), ('size', 'サイズ', 90)], height=4)
        ttk.Button(right, text='選択した添付を別名保存', command=self.copy_attachment).pack(anchor='e', pady=6)
        ttk.Button(right, text='選択した添付を削除…', command=lambda: self.delete_record('attachment')).pack(anchor='e')
        bottom = ttk.Frame(root, padding=14)
        bottom.pack(fill='x')
        self.status = tk.StringVar(value='読み込みます。')
        ttk.Label(bottom, textvariable=self.status, wraplength=920).pack(side='left')
        ttk.Button(bottom, text='保存状態・競合', command=self.warnings).pack(side='right')
        self.sample_items, self.measurement_items, self.attachment_items = {}, {}, {}
        self.refresh(remote=False)

    def location_text(self):
        return '研究室の共有データ／未送信記録はこのPCにも保持します' if isinstance(self.store, DesktopSession) else 'PC内デモ — 共有先未設定。架空データだけで試してください。実データの登録は担当者の設定後に行います。'

    def compare(self):
        if self.job or self.snapshot is None:
            messagebox.showinfo('処理中', '読込・保存が終わるまでお待ちください。', parent=self.root)
            return
        from .comparison_ui import ComparisonDialog
        ComparisonDialog(self)

    def fonts(self):
        from tkinter import font
        from .font_requirements import inspect_fonts, format_font_status
        self.font_status = inspect_fonts(font.families(self.root))
        self.show_text('必要フォントの確認', format_font_status(self.font_status))

    def maintenance(self):
        if self.job:
            messagebox.showinfo('処理中', '保存・読込が終わるまでお待ちください。', parent=self.root)
            return
        from .maintenance import MaintenanceDialog
        MaintenanceDialog(self)

    def table(self, parent, columns, height=12):
        frame = ttk.Frame(parent)
        frame.pack(fill='both', expand=True)
        widget = ttk.Treeview(frame, columns=[c[0] for c in columns], show='headings', selectmode='browse', height=height)
        for name, label, width in columns:
            widget.heading(name, text=label)
            widget.column(name, width=width, minwidth=45)
        scroll = ttk.Scrollbar(frame, orient='vertical', command=widget.yview)
        horizontal = ttk.Scrollbar(frame, orient='horizontal', command=widget.xview)
        widget.configure(yscrollcommand=scroll.set, xscrollcommand=horizontal.set)
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(0, weight=1)
        widget.grid(row=0, column=0, sticky='nsew')
        scroll.grid(row=0, column=1, sticky='ns')
        horizontal.grid(row=1, column=0, sticky='ew')
        return widget

    def show_error(self, exc):
        messagebox.showerror('操作を完了できませんでした', str(exc) + '\n\n保存中の通信エラーでは、一度再読込して保存済みか確認してから再操作してください。', parent=self.root)

    def bulk_import(self):
        if not self.job and self.snapshot is not None:
            from .bulk_import_ui import BulkImportDialog
            BulkImportDialog(self)

    def trash(self):
        if not self.job and self.snapshot is not None:
            from .trash_ui import TrashDialog
            TrashDialog(self)

    def delete_record(self, kind):
        if self.job or self.snapshot is None:
            return
        try:
            attachment = None
            if kind == 'attachment':
                item, attachment = self.selected(self.attachments, self.attachment_items)
            else:
                item = self.selected(self.samples if kind == 'sample' else self.measurements, self.sample_items if kind == 'sample' else self.measurement_items)
            self.snapshot.current(item['id'])
            from .trash_ui import VisibilityDialog
            VisibilityDialog(self, item, deleted=True, attachment=attachment)
        except StoreError as exc:
            self.show_error(exc)

    def delete_treatment(self):
        if self.job or self.snapshot is None:
            return
        try:
            sample = self.selected(self.samples, self.sample_items)
            choices = [item for item in self.snapshot.of_kind('treatment') if item['data']['sample_id'] == sample['id']]
            if not choices:
                raise StoreError('この試料に削除できる処理履歴はありません。')
            from .trash_ui import TreatmentPicker
            TreatmentPicker(self, choices)
        except StoreError as exc:
            self.show_error(exc)

    def unplotted(self):
        if self.job or self.snapshot is None:
            messagebox.showinfo('処理中', '読込・保存が終わるまでお待ちください。', parent=self.root)
            return
        from .unplotted_ui import UnplottedDialog
        UnplottedDialog(self)

    def polarization(self):
        if not self.job:
            from .polarization_ui import PolarizationDialog
            PolarizationDialog(self)

    def remember_preferences(self):
        if not self.remember_settings:
            return
        try:
            save_preferences(self.store, self.actor.get())
            if self.production:
                deployment = load_deployment()
                key = 'mac_path' if sys.platform == 'darwin' else 'windows_path'
                if deployment.get(key) != str(self.store.root):
                    save_deployment({**deployment, key: str(self.store.root)}, bound=deployment)
        except (OSError, StoreError, ValueError) as exc:
            messagebox.showwarning('起動設定の保存',
                '記録の保存先や表示名を、このPCに記憶できませんでした。次回は保存先を選び直してください。'
                f'\n\n詳細: {exc}', parent=self.root)

    def run_job(self, label, work, done, error=None):
        if self.job is not None:
            messagebox.showinfo('処理中', '現在の読込・保存が終わるまでお待ちください。', parent=self.root)
            return False
        self.status.set(label)
        # Tk objects can form cycles even after a dialog is destroyed. Automatic
        # collection in the file worker would finalize their Tcl variables on
        # the wrong thread. Collect cycles in the UI thread at job completion.
        resume_gc = gc.isenabled()
        gc.disable()
        try:
            self.job = self.executor.submit(work)
        except Exception:
            if resume_gc:
                gc.enable()
            raise

        def poll():
            if not self.job.done():
                self.root.after(100, poll)
                return
            future, self.job = self.job, None
            gc.collect()
            if resume_gc:
                gc.enable()
            try:
                result = future.result()
            except Exception as exc:
                self.status.set('操作未完了。保存状態を確認してください。')
                (error or self.show_error)(exc)
            else:
                self.status.set('完了しました。')
                done(result)
        self.root.after(100, poll)
        return True

    def refresh(self, remote=True):
        store = self.store
        def done(snapshot):
            self.snapshot = snapshot
            self.related = {'measurement': {}, 'treatment': {}}
            for kind, groups in self.related.items():
                for item in snapshot.of_kind(kind):
                    groups.setdefault(item['data']['sample_id'], []).append(item)
            self.parents_with_pending = {item['data']['sample_id'] for kind in self.related for values in self.related[kind].values()
                                         for item in values if item['id'] in self.pending_ids}
            self.render_samples()
            if isinstance(store, DesktopSession):
                self.queue_status.set(f'このPCの未送信：{self.pending_count}件 ｜ {store.connection_message}')
                self.sync_button.configure(state='normal' if self.pending_count else 'disabled')
            else:
                self.queue_status.set('PC内デモ：この記録は共有先へ送信されません。')
                self.sync_button.configure(state='disabled')
            self.status.set(f'記録 {len(snapshot.heads)}件／確認事項 {len(snapshot.warnings)}件。' +
                (f'{store.connection_message}／未送信 {self.pending_count}件。' if isinstance(store, DesktopSession) else 'PC内デモ。') + '登録者IDは自己申告です。')
        def work():
            snapshot = store.load() if remote or not hasattr(store, 'cached') else store.cached()
            try:
                pending = store.pending() if isinstance(store, DesktopSession) else []
                self.pending_count = len(pending)
                self.pending_ids = {i['id'] for i in pending}
            except StoreError as exc:
                self.pending_count, self.pending_ids = '確認必要', set()
                snapshot.warnings.append(str(exc))
            return snapshot
        self.run_job('共有先を確認しています…' if remote else 'PC内の記録を読み込んでいます。共有先は「再読込」で確認できます…', work, done)

    def pending(self):
        if not isinstance(self.store, DesktopSession):
            self.show_text('未送信一覧', 'PC内デモは共有先へ送信されません。保存先を選ぶと、その保存先専用の未送信記録を管理できます。')
            return
        store = self.store
        labels = {'sample': '試料', 'measurement': '測定', 'treatment': '処理'}
        self.run_job('未送信記録を確認しています…', store.pending,
            lambda items: self.show_text('未送信一覧（この保存先）', '\n'.join(
                f'{labels[i["kind"]]} / {i["data"].get("label", i["data"].get("method"))} / {i["registrant"]} / {i["id"]}'
                for i in items) or '未送信記録はありません。'))

    def sync(self):
        if not isinstance(self.store, DesktopSession):
            messagebox.showinfo('PC内デモ', 'デモの記録は送信されません。共有先を選んでから登録してください。', parent=self.root)
            return
        store = self.store
        def done(result):
            sent, errors, remaining = result
            self.show_text('まとめて送信の結果', f'送信確認済み {sent}件／未送信 {remaining}件\nPC内の記録と添付は保持しています。\n\n' + '\n'.join(errors))
            self.refresh()
        self.run_job('未送信記録と添付を送信・照合しています…', store.sync, done)

    def render_samples(self):
        if self.snapshot is None:
            return
        old_sample = self.samples.selection()
        old_item = self.sample_items.get(old_sample[0]) if old_sample else None
        target_sample = getattr(self, 'focus_sample_id', None) or (old_item['id'] if old_item else None)
        old_measurement = self.measurements.selection()
        old_measurement_item = self.measurement_items.get(old_measurement[0]) if old_measurement else None
        target_measurement = getattr(self, 'focus_measurement_id', None) or (old_measurement_item['id'] if old_measurement_item else None)
        self.focus_sample_id = self.focus_measurement_id = None
        self.samples.delete(*self.samples.get_children())
        self.sample_items = {}
        query = self.query.get().strip().casefold()
        actor = self.actor.get().strip()
        mine_only = self.scope.get() == SCOPE_MINE and bool(actor)
        hidden = 0
        indexed = self.store.search_samples(query) if hasattr(self.store, 'search_samples') else None
        for item in sorted(self.snapshot.of_kind('sample'), key=lambda row: row['data']['label']):
            data = item['data']
            if indexed is not None:
                if (item['id'], item['revision_id']) not in indexed:
                    continue
            elif query:
                related = [r for kind in self.related for r in self.related[kind].get(item['id'], [])]
                searchable = json.dumps([data, item['id'], item['registrant'], [r['data'] for r in related]], ensure_ascii=False).casefold()
                if query not in searchable:
                    continue
            if mine_only and not self.belongs_to(item, actor):
                hidden += 1
                continue
            iid = item['id'] + ':' + item['revision_id']
            self.sample_items[iid] = item
            state = self.record_state(item)
            self.samples.insert('', 'end', iid=iid, values=[data['label'], data['creator'], data['alloy'], state])
        self.measurements.delete(*self.measurements.get_children())
        self.attachments.delete(*self.attachments.get_children())
        self.summary.configure(state='normal')
        self.summary.delete('1.0', 'end')
        self.summary.insert('1.0', self.sample_list_hint(hidden, actor, bool(query)))
        self.summary.configure(state='disabled')
        self.measurement_info.configure(text='測定を選ぶと内容を表示します。')
        matches = [key for key, item in self.sample_items.items() if item['id'] == target_sample]
        if matches:
            self.samples.selection_set(matches[0])
            self.samples.see(matches[0])
            self.select_sample()
            measurement_matches = [key for key, item in self.measurement_items.items() if item['id'] == target_measurement]
            if measurement_matches:
                self.measurements.selection_set(measurement_matches[0])
                self.select_measurement()

    def schedule_search(self, *args):
        if self.search_timer is not None:
            self.root.after_cancel(self.search_timer)
        self.search_timer = self.root.after(220, self.run_search)

    def run_search(self):
        self.search_timer = None
        if self.job is not None:
            # Debounced actor/query changes may fire between a successful save
            # and its snapshot refresh. Do not consume focus IDs on old data.
            self.search_timer = self.root.after(100, self.run_search)
            return
        self.render_samples()

    def record_state(self, item):
        if item['id'] in self.snapshot.blocked:
            return '要確認'
        if len(self.snapshot.heads[item['id']]) > 1:
            return '競合'
        pending_ids = getattr(self, 'pending_ids', set())
        if item['id'] in pending_ids:
            return '未送信'
        if not isinstance(self.store, DesktopSession):
            return 'デモ'
        if item['kind'] == 'sample' and item['id'] in getattr(self, 'parents_with_pending', set()):
            return '子記録未送信あり'
        return '共有記録' if self.store.online else '共有記録（PC内）'

    def selected(self, tree, items):
        selection = tree.selection()
        if not selection or selection[0] not in items:
            raise StoreError('対象を一覧から選択してください。')
        return items[selection[0]]

    def select_sample(self, event=None):
        if not self.samples.selection():
            return
        item = self.selected(self.samples, self.sample_items)
        selection = self.measurements.selection()
        selected_measurement = self.measurement_items.get(selection[0]) if selection else None
        target_measurement = selected_measurement['id'] if selected_measurement else None
        data = item['data']
        lines = [f'{data["label"]} / 作製者: {data["creator"]}', f'材料: {data["material"]}  合金種: {data["alloy"]}',
                 f'研磨: {data["polishing"]}  溶体化処理: {"あり" if data["solution_treated"] else "なし"}',
                 f'登録者: {item["registrant"]}  最終訂正者: {item["actor"]}', f'内部ID: {item["id"]}', f'備考: {data["notes"]}']
        for treatment in self.related['treatment'].get(item['id'], []):
            if treatment['data']['sample_id'] == item['id']:
                row = treatment['data']
                lines.append(f'処理: {row["method"]} / {row["temperature"]}℃ / {row["duration"]}{row["duration_unit"]} / {row["notes"]} / {self.record_state(treatment)}')
        self.summary.configure(state='normal')
        self.summary.delete('1.0', 'end')
        self.summary.insert('1.0', '\n'.join(lines))
        self.summary.configure(state='disabled')
        self.measurements.delete(*self.measurements.get_children())
        self.measurement_items = {}
        for measurement in self.related['measurement'].get(item['id'], []):
            if measurement['data']['sample_id'] == item['id']:
                key = measurement['id'] + ':' + measurement['revision_id']
                self.measurement_items[key] = measurement
                row = measurement['data']
                self.measurements.insert('', 'end', iid=key, values=[row['method'] + ' / ' + row['metric'], row['measurer'], row['measured_on'], len(row['values']), self.record_state(measurement)])
        self.measurement_info.configure(text='測定を選ぶと内容を表示します。')
        self.render_attachments(item)
        matches = [key for key, measurement in self.measurement_items.items() if measurement['id'] == target_measurement]
        if matches:
            self.measurements.selection_set(matches[0])
            self.select_measurement()

    def select_measurement(self, event=None):
        if not self.measurements.selection():
            return
        item = self.selected(self.measurements, self.measurement_items)
        values = [Decimal(v) for v in item['data']['values']]
        summary = f'数値 {len(values)}点'
        if values:
            summary += f' / 平均 {statistics.mean(values):.6g}'
        if len(values) > 1:
            summary += f' / 標本標準偏差 {statistics.stdev(values):.6g}'
        if len(self.snapshot.heads[item['id']]) > 1:
            summary += ' / 訂正競合あり'
        if item['id'] in self.snapshot.blocked:
            summary += ' / 読めない改訂あり・訂正保留'
        self.measurement_info.configure(text=summary + f' / {item["data"]["unit"]}\n登録者: {item["registrant"]} / 備考: {item["data"]["notes"]}')
        self.render_attachments(self.selected(self.samples, self.sample_items), item)

    def actor_changed(self, *args):
        value = self.actor.get().strip()
        if value != self.filtered_actor:
            self.filtered_actor = value
            self.schedule_search()

    @staticmethod
    def belongs_to(item, actor):
        """List filter only. Editing rights, saving and sync are unchanged."""
        who = normalized(actor)
        if not who:
            return False
        return who in {normalized(item['registrant']),
                       normalized(item['data'].get('creator') or '')}

    def sample_list_hint(self, hidden, actor, searching=False):
        if self.scope.get() == SCOPE_MINE and not actor:
            return ('登録者IDが未入力のため、全員の試料を表示しています。'
                    '上の登録者IDを入れると自分の試料だけに絞り込みます。')
        if not self.sample_items and hidden:
            # hidden は検索で絞り込んだ後の件数なので、研究室全体の件数と混同させない。
            scope = '検索条件に一致する' if searching else ''
            return (f'{scope}自分の試料はありません。{scope}ほかの作製者・登録者の試料が{hidden}件あります。'
                    f'「{SCOPE_ALL}」に切り替えると表示できます。')
        return '試料を選択してください。記録は上書きせず改訂を追加保存します。'

    def render_attachments(self, sample, measurement=None):
        self.attachments.delete(*self.attachments.get_children())
        self.attachment_items = {}
        for item in [r for r in [sample, measurement] if r]:
            for attachment in item['attachments']:
                if self.snapshot.status(item, attachment['id'])['state'] == 'deleted':
                    continue
                key = item['id'] + ':' + attachment['id']
                self.attachment_items[key] = (item, attachment)
                self.attachments.insert('', 'end', iid=key, values=[attachment['name'], original_extension(attachment) or 'なし', '試料' if item['kind'] == 'sample' else '測定', f'{attachment["size"] / 1024 / 1024:.2f} MiB'])

    def form(self, kind, existing=None, sample=None):
        if self.job:
            messagebox.showinfo('処理中', '処理が終わるまでお待ちください。', parent=self.root)
            return
        try:
            if existing:
                self.snapshot.current(existing['id'])
                if not self.store.can_edit(existing, self.actor.get()):
                    raise StoreError('担当者または登録者のIDを入力してください。旧表示名のみの記録はID対応を別途確認してください。')
            RecordDialog(self, kind, existing, sample)
        except StoreError as exc:
            self.show_error(exc)

    def edit_sample(self):
        try:
            self.form('sample', self.selected(self.samples, self.sample_items))
        except StoreError as exc:
            self.show_error(exc)

    def new_measurement(self):
        try:
            self.form('measurement', sample=self.selected(self.samples, self.sample_items))
        except StoreError as exc:
            self.show_error(exc)

    def edit_measurement(self):
        try:
            self.form('measurement', self.selected(self.measurements, self.measurement_items), self.selected(self.samples, self.sample_items))
        except StoreError as exc:
            self.show_error(exc)

    def new_treatment(self):
        try:
            self.form('treatment', sample=self.selected(self.samples, self.sample_items))
        except StoreError as exc:
            self.show_error(exc)

    def show_text(self, title, value):
        window = tk.Toplevel(self.root)
        window.title(title)
        content = tk.Text(window, width=95, height=30, wrap='word')
        content.pack(fill='both', expand=True)
        content.insert('1.0', value)
        content.configure(state='disabled')

    def history(self):
        try:
            item = self.selected(self.measurements, self.measurement_items) if self.measurements.selection() else self.selected(self.samples, self.sample_items)
            history = list(self.snapshot.revisions[item['id']].values())
            self.show_text('改訂履歴（削除・自動マージは行いません）', json.dumps(history, ensure_ascii=False, indent=2))
        except StoreError as exc:
            self.show_error(exc)

    def warnings(self):
        if self.snapshot:
            self.show_text('保存状態・競合', '\n'.join(self.snapshot.warnings) or '現在の読込では問題は見つかりませんでした。\n改ざん防止・NAS障害への耐性を保証する検査ではありません。')

    def csv(self):
        if self.snapshot is None or self.job:
            return
        destination = filedialog.asksaveasfilename(parent=self.root, title='新しい名前でCSV保存（既存ファイルは上書きしません）', defaultextension='.csv', initialfile='labnote-samples.csv')
        if destination:
            snapshot = self.snapshot
            states = {i['id']: self.record_state(i) for i in snapshot.of_kind('sample')}
            self.run_job('CSVを書き出しています…', lambda: export_csv(snapshot, destination, states=states), lambda result: messagebox.showinfo('CSV出力', '保存しました。', parent=self.root))

    def copy_attachment(self):
        try:
            item, attachment = self.selected(self.attachments, self.attachment_items)
            destination = filedialog.asksaveasfilename(parent=self.root, title='添付を保存（拡張子を変えても形式変換はしません）', initialfile=original_filename(attachment))
            if destination:
                destination = export_destination(destination, attachment)
                store = self.store
                self.run_job('添付を複製し、ハッシュを照合しています…', lambda: store.export_attachment(item, attachment['id'], destination), lambda result: messagebox.showinfo('添付の保存', '複製と整合性の照合が完了しました。', parent=self.root))
        except StoreError as exc:
            self.show_error(exc)

    def pick_store(self):
        if self.job:
            return
        directory = filedialog.askdirectory(parent=self.root, title='labnote-store.jsonがある保存先を選択')
        if directory:
            self.change_store(lambda: FileStore(directory))

    def new_store(self):
        if self.production:
            messagebox.showinfo('本番の保存先', '本番配布版は共有データのIDを固定しています。別の保存先を新規作成する場合は、担当者が配布設定を更新してください。', parent=self.root)
            return
        if self.job:
            return
        parent = filedialog.askdirectory(parent=self.root, title='新しいLabNote専用フォルダを作る親フォルダを選択')
        if not parent:
            return
        name = simpledialog.askstring('新しい保存先', '新規フォルダ名（既存フォルダは使用しません）', initialvalue='LabNote-Data', parent=self.root)
        if not name:
            return
        if not re.fullmatch(r'[\w-]{1,80}', name) or name.upper() in {'CON', 'PRN', 'AUX', 'NUL', *(f'COM{i}' for i in range(10)), *(f'LPT{i}' for i in range(10))}:
            messagebox.showerror('フォルダ名', '文字・数字・_・-で、別の名前を指定してください。', parent=self.root)
            return
        target = Path(parent) / name
        if messagebox.askyesno('保存先の新規作成', f'{target}\n\nこの新規フォルダに記録を保存します。既存ファイルは変更しません。作成しますか？', parent=self.root):
            self.change_store(lambda: FileStore.create(target))

    def change_store(self, factory):
        def done(store):
            self.store = store
            if isinstance(store, DesktopSession):
                store.catalog = self.catalog
            self.snapshot = None
            self.location.configure(text=self.location_text())
            self.remember_preferences()
            self.refresh()
        def work():
            store = factory()
            if self.bound_store_id and store.store_id != self.bound_store_id:
                raise StoreError('本番設定とは異なるデータです。接続先を変更しません。担当者に配布設定を確認してください。')
            return LocalSession(store) if store.root == DEMO.resolve() else DesktopSession.connect(store)
        self.run_job('保存先を確認しています…', work, done)

    def demo(self):
        if self.production or self.store.root != DEMO.resolve():
            messagebox.showinfo('仮データ', '仮データの追加はPC内のデモ保存先だけで行えます。', parent=self.root)
            return
        store = self.store
        def work():
            for alloy, values in [('A1050', ['1', '2', '3']), ('A5052', ['3', '5', '7']), ('A6061', ['5', '8', '11'])]:
                sample = store.save('sample', {'label': 'DEMO-' + identifier()[:8], 'creator': 'zz00000', 'material': 'アルミニウム合金', 'alloy': alloy, 'polishing': '未研磨', 'notes': '動作確認用の架空データです。実際の材料の測定値ではありません。'}, 'zz00000')
                store.save('measurement', {'sample_id': sample['id'], 'measurer': 'zz00000', 'method': '画像観察', 'measured_on': date.today().isoformat(), 'values': [], 'notes': '数値なしでも登録できます。'}, 'zz00000')
                store.save('measurement', {'sample_id': sample['id'], 'measurer': 'zz00000', 'method': 'SEM', 'metric': '結晶粒径', 'unit': 'μm', 'measured_on': date.today().isoformat(), 'values': values, 'notes': 'グラフ表示確認専用の架空の反復値です。'}, 'zz00000')
                store.save('treatment', {'sample_id': sample['id'], 'method': '焼鈍', 'temperature': '100', 'duration': '1', 'duration_unit': 'h'}, 'zz00000')
        self.actor.set('zz00000')
        self.run_job('PC内に仮データを追加しています…', work, lambda result: self.refresh())

    def close(self):
        if self.job:
            messagebox.showinfo('処理中', '保存／読込が終わるまでお待ちください。共有接続の停止時はOSのタイムアウトまで待つ場合があります。', parent=self.root)
            return
        self.remember_preferences()
        if self.search_timer is not None:
            self.root.after_cancel(self.search_timer)
        self.executor.shutdown(wait=False)
        self.root.destroy()


def main():
    parser = argparse.ArgumentParser(description='LabNote desktop client')
    source = parser.add_mutually_exclusive_group()
    source.add_argument('--store', help='Existing LabNote data folder; never initialized automatically')
    source.add_argument('--demo', action='store_true', help='Preview only local demo; do not change remembered shared destination')
    args = parser.parse_args()
    if not args.store:
        args.demo = True  # Public edition defaults to an isolated local demo.
    root = tk.Tk()
    root.withdraw()
    install_error_dialog(root, messagebox.showerror)
    try:
        try:
            preferences = load_preferences()
        except (OSError, ValueError) as exc:
            messagebox.showwarning('起動設定を読み取れません',
                f'起動設定を読み取れません。配布設定または保存先を確認してください。\n\n詳細: {exc}', parent=root)
            preferences = {}
        deployment = {} if args.demo else load_deployment()
        catalog = load_catalog(deployment)
        target = startup_target(deployment, preferences, demo=args.demo, explicit_store=args.store, demo_path=DEMO)
        chosen, expected_id = target['path'], target['store_id']
        if target['select_required']:
            messagebox.showinfo('本番データへの接続', 'このPCでNASへ接続した後、本番の「.Labnote_DB/data」フォルダを選んでください。\n正しい共有データのIDを確認して記憶します。キャンセルすると終了します。', parent=root)
            chosen = filedialog.askdirectory(parent=root, title='本番のLabNoteデータフォルダを選択')
            if not chosen:
                root.destroy()
                return
        if target['production'] and (target['select_required'] or args.store):
            while chosen:
                try:
                    candidate = FileStore(chosen)
                    if candidate.store_id != expected_id:
                        raise StoreError('選んだフォルダは本番設定のデータIDと一致しません。')
                    break
                except (OSError, StoreError, ValueError) as exc:
                    messagebox.showerror('本番の保存先を確認してください', str(exc), parent=root)
                    chosen = filedialog.askdirectory(parent=root, title='本番のLabNoteデータフォルダを選択（キャンセルで終了）')
                    if not chosen:
                        root.destroy()
                        return
        if chosen and expected_id and Path(chosen) != DEMO:
            store = DesktopSession(chosen, expected_id)
            chosen = None
            cached_start = True
        else:
            cached_start = False
        while chosen:
            try:
                store = FileStore(chosen)
                if expected_id and store.store_id != expected_id:
                    raise StoreError('前回とは異なるデータの保存先です。接続先を確認してください。')
                if store.root != DEMO.resolve():
                    store = DesktopSession.connect(store)
                else:
                    store = LocalSession(store)
                break
            except (OSError, ValueError, StoreError) as exc:
                messagebox.showwarning('保存先を開けません',
                    'NASへの接続やフォルダを確認してください。別の保存先を選べます。'
                    f'\n\n{chosen}\n\n詳細: {exc}', parent=root)
                chosen = filedialog.askdirectory(parent=root, title='LabNoteの保存先を選択（キャンセルでデモ起動を確認）')
                if target['production']:
                    if not chosen:
                        root.destroy()
                        return
                    continue
                expected_id = None
                if not chosen and not messagebox.askyesno('PC内のデモで起動しますか？',
                    'デモの記録はNASへ保存されません。デモで起動する場合は「はい」、終了する場合は「いいえ」を選んでください。', parent=root):
                    root.destroy()
                    return
        else:
            if not cached_start:
                DEMO.parent.mkdir(parents=True, exist_ok=True)
                store = LocalSession(FileStore(DEMO) if DEMO.exists() else FileStore.create(DEMO))
        App(root, store, actor=preferences.get('actor', ''), remember_settings=not args.demo, catalog=catalog, production=target['production'])
    except (OSError, StoreError) as exc:
        messagebox.showerror('起動できませんでした', str(exc), parent=root)
        root.destroy()
        return
    root.deiconify()
    root.mainloop()
