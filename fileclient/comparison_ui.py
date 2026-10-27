"""Native, read-only comparison screen. No browser, service, or extra runtime."""
import tkinter as tk
import math
from dataclasses import replace
from tkinter import filedialog, font, messagebox, ttk

from .comparison import (AXES, GROUPS, KINDS, chart_scene, comparison_rows, series_of,
                         export_comparison, export_svg, select_rows)
from .store import StoreError
from .figure_preview import FigureControls, preview_canvas, render_scene, scene_point
from .graph_style import (ERROR_MODES, SCRIPT_DROP, SCRIPT_RISE, SCRIPT_SIZE, SYMBOL_FONT,
                          GraphStyle, font_family, font_runs, load_style, missing_fonts,
                          symbol_text)


class ComparisonDialog(tk.Toplevel):
    def __init__(self, app, *, style_file=None, preset_file=None):
        super().__init__(app.root)
        self.app = app
        self.style_file = style_file
        try:
            self.graph_style = load_style(style_file)
            style_error = None
        except (OSError, StoreError, ValueError, TypeError) as exc:
            self.graph_style = GraphStyle()
            style_error = str(exc)
        self.font_cache = {}
        self.legend_drag = None
        self.current_signature = None
        self.title('グラフ比較 — LabNote')
        self.geometry('1150x860')
        self.minsize(880, 740)
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)
        self.transient(app.root)
        self.rows, self.excluded = comparison_rows(app.snapshot, app.record_state)
        self.selected_rows, self.scene = [], None
        self.signatures = sorted({row.signature for row in self.rows})
        self.signature_labels = [f'{i+1}. {method} / {metric} [{unit}]'
                                 for i, (method, metric, unit) in enumerate(self.signatures)]
        top = ttk.Frame(self, padding=12)
        top.grid(row=0, column=0, sticky='ew')
        header = ttk.Frame(top)
        header.pack(fill='x')
        ttk.Label(header, text='数値データを比較', font=('Yu Gothic UI', 15, 'bold')).pack(side='left')
        ttk.Button(header, text='軸名・凡例・書式…', command=self.format_graph).pack(side='right')
        ttk.Label(top, text='開いた時点のPC内データ（未送信を含む）を使用。共有先の更新は主画面で再読込後、この画面を開き直してください。', wraplength=1080).pack(anchor='w', pady=(3, 8))
        filters = ttk.Frame(top)
        filters.pack(fill='x')
        self.signature = self.combo(filters, '方法・項目・単位', self.signature_labels, 0, width=36)
        self.alloy = self.combo(filters, '合金種', ['すべて'] + sorted({row.alloy for row in self.rows}), 1)
        self.creator = self.combo(filters, '作製者', ['すべて'] + sorted({row.creator for row in self.rows}), 2)
        self.measurer = self.combo(filters, '測定者', ['すべて'] + sorted({row.measurer for row in self.rows}), 3)
        self.since = self.date_entry(filters, '測定日 開始', 4)
        self.until = self.date_entry(filters, '測定日 終了', 5)
        self.range_error = ''
        options = ttk.Frame(top)
        options.pack(fill='x', pady=(8, 0))
        self.kind = self.combo(options, 'グラフ', list(KINDS), 0)
        self.axis = self.combo(options, '横軸', list(AXES), 1)
        self.axis.bind('<<ComboboxSelected>>', self.change_axis)
        self.group = self.combo(options, '色分け', list(GROUPS), 2)
        self.error_mode = self.combo(options, 'エラーバー', list(ERROR_MODES), 3)
        self.error_mode.set(self.graph_style.error_mode)
        FigureControls(self, top, preset_file).pack(fill='x', pady=(8, 0))
        self.status = ttk.Label(top, text='', wraplength=1080)
        self.status.pack(fill='x', pady=(8, 0))
        self.canvas = preview_canvas(self, 1)
        self.canvas.bind('<Configure>', lambda event: render_scene(self))
        self.canvas.bind('<Button-1>', self.press_chart)
        self.canvas.bind('<B1-Motion>', self.drag_legend)
        self.canvas.bind('<ButtonRelease-1>', lambda event: setattr(self, 'legend_drag', None))
        frame = ttk.Frame(self, padding=(12, 8, 12, 0))
        frame.grid(row=2, column=0, sticky='ew')
        ttk.Label(frame, text='使用する測定セット（行を選ぶと出典と反復値を表示）').pack(anchor='w')
        table = ttk.Frame(frame)
        table.pack(fill='x')
        columns = ('sample', 'creator', 'date', 'measurer', 'n', 'mean', 'sd', 'state')
        self.table = ttk.Treeview(table, columns=columns, show='headings', height=4, selectmode='browse')
        for key, title, width in zip(columns, ['試料ID', '作製者', '測定日', '測定者', '点数', '平均', '標本SD', '保存状態'], [235, 100, 105, 100, 55, 100, 100, 120]):
            self.table.heading(key, text=title)
            self.table.column(key, width=width, minwidth=40)
        scroll = ttk.Scrollbar(table, orient='vertical', command=self.table.yview)
        horizontal = ttk.Scrollbar(table, orient='horizontal', command=self.table.xview)
        self.table.configure(yscrollcommand=scroll.set, xscrollcommand=horizontal.set)
        self.table.grid(row=0, column=0, sticky='nsew')
        scroll.grid(row=0, column=1, sticky='ns')
        horizontal.grid(row=1, column=0, sticky='ew')
        table.columnconfigure(0, weight=1)
        self.table.bind('<<TreeviewSelect>>', self.show_provenance)
        self.detail = ttk.Label(frame, text='箱ひげ図は各測定内の反復値。ひげは最小–最大、箱は25–75%、中央線は中央値です。', wraplength=1080)
        self.detail.pack(fill='x', pady=(5, 0))
        bottom = ttk.Frame(self, padding=12)
        bottom.grid(row=3, column=0, sticky='ew')
        self.csv_button = ttk.Button(bottom, text='使用データをCSV保存', command=lambda: self.export('summary'))
        self.csv_button.pack(side='left')
        self.raw_button = ttk.Button(bottom, text='反復値もCSV保存', command=lambda: self.export('raw'))
        self.raw_button.pack(side='left', padx=6)
        self.svg_button = ttk.Button(bottom, text='グラフをSVG保存', command=lambda: self.export('svg'))
        self.svg_button.pack(side='left')
        ttk.Button(bottom, text='閉じる', command=self.destroy).pack(side='right')
        ttk.Label(bottom, text='枠・文字：純黒／目盛り：内向き').pack(side='right', padx=10)
        self.refresh()
        if style_error:
            messagebox.showwarning('グラフの書式', '標準書式を読み取れないため、今回は初期書式を使用します。\n' + style_error, parent=self)

    def change_axis(self, event=None):
        self.graph_style = replace(self.graph_style, x_label=None, x_italics=(),
                            x_supers=(), x_subs=(), x_symbols=())
        self.refresh()

    def format_graph(self):
        from .graph_style_ui import GraphStyleDialog
        self.graph_style = replace(self.graph_style, error_mode=self.error_mode.get())
        GraphStyleDialog(self)

    def series_names(self):
        return sorted({series_of(row, self.group.get()) for row in self.selected_rows})

    def combo(self, parent, title, choices, column, width=18):
        ttk.Label(parent, text=title).grid(row=0, column=column, sticky='w', padx=(0, 8))
        combo = ttk.Combobox(parent, values=choices, state='readonly', width=width)
        combo.grid(row=1, column=column, sticky='ew', padx=(0, 8))
        if choices:
            combo.current(0)
        combo.bind('<<ComboboxSelected>>', lambda event: self.refresh())
        parent.columnconfigure(column, weight=1)
        return combo

    def date_entry(self, parent, title, column, width=12):
        ttk.Label(parent, text=title).grid(row=0, column=column, sticky='w', padx=(0, 8))
        widget = ttk.Entry(parent, width=width)
        widget.grid(row=1, column=column, sticky='ew', padx=(0, 8))
        for sequence in ('<Return>', '<FocusOut>'):
            widget.bind(sequence, lambda event: self.refresh())
        parent.columnconfigure(column, weight=1)
        return widget

    def refresh(self):
        index = self.signature.current()
        signature = self.signatures[index] if index >= 0 else None
        if self.current_signature is not None and signature != self.current_signature:
            self.graph_style = replace(self.graph_style, y_label=None, y_italics=(),
                            y_supers=(), y_subs=(), y_symbols=())
        self.current_signature = signature
        self.range_error = ''
        try:
            self.selected_rows = select_rows(self.rows, self.signatures[index],
                alloy=self.alloy.get() if self.alloy.current() > 0 else '',
                creator=self.creator.get() if self.creator.current() > 0 else '',
                measurer=self.measurer.get() if self.measurer.current() > 0 else '',
                since=self.since.get().strip(), until=self.until.get().strip()) if index >= 0 else []
        except StoreError as exc:
            # 範囲の書式・前後関係の誤りは表示を止めず、理由を出して空にする。
            self.selected_rows = []
            self.range_error = str(exc)
        selected = self.table.selection()
        self.table.delete(*self.table.get_children())
        for row in self.selected_rows:
            self.table.insert('', 'end', iid=row.measurement_id, values=(row.label, row.creator,
                row.measured_on, row.measurer, len(row.values), f'{row.mean:.6g}',
                f'{row.sd:.6g}' if row.sd is not None else '—', row.state))
        if selected and self.table.exists(selected[0]):
            self.table.selection_set(selected[0])
        count = sum(len(row.values) for row in self.selected_rows)
        exclusions = '、'.join(f'{key} {value}件' for key, value in self.excluded.items() if value)
        unavailable = missing_fonts(self.graph_style, font.families(self))
        self.status.configure(text=(self.range_error + ' ' if self.range_error else '') +
            f'{len(self.selected_rows)}測定セット／反復値 {count}点。' +
            ('比較対象外：' + exclusions + '。' if exclusions else '') +
            '処理温度・時間は測定との対応が未確定のため横軸に使用しません。' +
            (' 指定フォントなし（代替表示）：' + '、'.join(unavailable) if unavailable else ''))
        self.detail.configure(text='点・行を選ぶと出典を表示。凡例はドラッグで移動できます。箱のひげは最小–最大。横軸の「測定日」は等間隔、「測定日（実日付）」は実際の間隔で並べます。')
        for button in (self.csv_button, self.raw_button):
            button.configure(state='normal' if self.selected_rows else 'disabled')
        self.draw()

    def draw(self):
        self.canvas.delete('all')
        self.scene = None
        try:
            scene = chart_scene(self.selected_rows, kind=self.kind.get(), axis=self.axis.get(),
                group=self.group.get(), style=replace(self.graph_style, error_mode=self.error_mode.get()))
        except StoreError as exc:
            self.scene_error = str(exc)
            render_scene(self)
            self.svg_button.configure(state='disabled')
            return
        self.scene = scene
        self.svg_button.configure(state='normal')
        render_scene(self)

    def draw_text(self, scene, coords, text, anchor, color, fmt, tags):
        runs = []
        for value, language, italic, script, symbol in font_runs(
                text, fmt['italics'], fmt['supers'], fmt['subs'], fmt['symbols']):
            family = SYMBOL_FONT if symbol else (
                scene.style.japanese_font if language == 'jp' else scene.style.english_font)
            size = max(1, round(fmt['size'] * self.preview_scale * (SCRIPT_SIZE if script else 1)))
            key = family, size, fmt['bold'], italic
            if key not in self.font_cache:
                self.font_cache[key] = font.Font(self, family=font_family(family), size=-size,
                    weight='bold' if fmt['bold'] else 'normal', slant='italic' if italic else 'roman')
            face = self.font_cache[key]
            body = symbol_text(value) if symbol else value
            rise = (-fmt['size'] * SCRIPT_RISE if script == 'super'
                    else fmt['size'] * SCRIPT_DROP if script == 'sub' else 0) * self.preview_scale
            runs.append((body, face, face.measure(body), rise))
        total = sum(width for value, face, width, rise in runs)
        offset = {'start': 0, 'end': -total, 'middle': -total/2}[anchor]
        radians = math.radians(fmt['angle'])
        for value, face, width, rise in runs:
            center = offset + width/2
            # 上付き・下付きは文字列の向きに対して垂直へずらす。
            self.canvas.create_text(
                coords[0] + center*math.cos(radians) + rise*math.sin(radians),
                coords[1] - center*math.sin(radians) + rise*math.cos(radians),
                text=value, font=face, fill=color, angle=fmt['angle'], tags=tags)
            offset += width

    def press_chart(self, event):
        bounds = self.scene.legend_bounds if self.scene else None
        x, y = scene_point(self, event) if self.scene else (0, 0)
        if bounds and bounds[0] <= x <= bounds[2] and bounds[1] <= y <= bounds[3]:
            self.legend_drag = (x, y, *bounds)
            return
        self.click_chart(event)

    def drag_legend(self, event):
        if not self.legend_drag or self.scene is None:
            return
        x, y, left, top, right, bottom = self.legend_drag
        width, height = right-left, bottom-top
        current_x, current_y = scene_point(self, event)
        horizontal = (left+current_x-x-5) / max(1, self.scene.width-width-10) * 100
        vertical = (top+current_y-y-5) / max(1, self.scene.height-height-10) * 100
        self.graph_style = replace(self.graph_style, legend_position='自由配置',
            legend_x=max(0, min(100, horizontal)), legend_y=max(0, min(100, vertical)))
        self.draw()

    def click_chart(self, event):
        x, y = self.canvas.canvasx(event.x), self.canvas.canvasy(event.y)
        matches = self.canvas.find_overlapping(x-5, y-5, x+5, y+5)
        for item in reversed(matches):
            for tag in self.canvas.gettags(item):
                if tag.startswith('record:'):
                    record = tag[7:]
                    self.table.selection_set(record)
                    self.table.see(record)
                    self.show_provenance()
                    return

    def show_provenance(self, event=None):
        selected = self.table.selection()
        if not selected:
            return
        row = next(row for row in self.selected_rows if row.measurement_id == selected[0])
        preview = ', '.join(f'{value:.6g}' for value in row.values[:15])
        if len(row.values) > 15:
            preview += ' …（全値は反復値CSVへ）'
        self.detail.configure(text=f'{row.label}／{row.state}\n測定ID: {row.measurement_id}　改訂ID: {row.measurement_revision}\n反復値 [{row.unit}]: {preview}')

    def export(self, mode):
        extension = '.svg' if mode == 'svg' else '.csv'
        destination = filedialog.asksaveasfilename(parent=self, title='新しい名前で保存',
            initialfile={'svg': '比較グラフ.svg', 'raw': '比較データ_反復値.csv', 'summary': '比較データ.csv'}[mode],
            defaultextension=extension, filetypes=[('SVG' if mode == 'svg' else 'CSV', '*' + extension)])
        if not destination:
            return
        try:
            if mode == 'svg':
                if self.scene is None:
                    raise StoreError('先にグラフを表示してください。')
                export_svg(self.scene, destination)
            else:
                export_comparison(self.selected_rows, destination, raw=mode == 'raw')
        except (OSError, StoreError) as exc:
            messagebox.showerror('保存できませんでした', str(exc) + '\n既存ファイルには上書きしません。別の名前を指定してください。', parent=self)
            return
        messagebox.showinfo('保存しました', destination, parent=self)
