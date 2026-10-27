"""Graph-only formatting editor; canonical sample names remain untouched."""
import tkinter as tk
from dataclasses import replace
from tkinter import colorchooser, font, messagebox, simpledialog, ttk

from .graph_style import (BLACK, ENGLISH_FONTS, ERROR_DIRECTIONS, ERROR_MODES, SYMBOL_FONT,
                          JAPANESE_FONTS, LEGEND_POSITIONS, MARKERS, missing_fonts, save_style)
from .store import StoreError

TAGS = (('italic', 'italics'), ('super', 'supers'), ('sub', 'subs'), ('symbol', 'symbols'))


def text_index(value, offset):
    return f'1.0 + {len(value[:offset].encode("utf-16-le")) // 2} chars'


class GraphStyleDialog(tk.Toplevel):
    def __init__(self, comparison):
        super().__init__(comparison)
        self.comparison = comparison
        self.original = comparison.graph_style
        self.title('グラフの書式 — 軸名・凡例・フォント・エラーバー')
        self.geometry('810x650')
        self.minsize(720, 600)
        self.transient(comparison)
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)
        notebook = ttk.Notebook(self)
        notebook.grid(row=0, column=0, sticky='nsew', padx=12, pady=12)
        figures, axes, errors, legend = (ttk.Frame(notebook, padding=14) for _ in range(4))
        for frame, title in ((figures, '寸法と用途'), (axes, '軸名とフォント'), (errors, 'エラーバー'), (legend, '凡例とデータ系列')):
            notebook.add(frame, text=title)
            frame.columnconfigure(1, weight=1)
        self.labels = {}
        self.defaults = comparison.axis_defaults() if hasattr(comparison, 'axis_defaults') else {'x': comparison.axis.get(), 'y': f'{comparison.selected_rows[0].metric} / {comparison.selected_rows[0].unit}' if comparison.selected_rows else ''}
        for row, key, title in ((0, 'x', '横軸の名前'), (3, 'y', '縦軸の名前')):
            ttk.Label(axes, text=title).grid(row=row, column=0, sticky='w', padx=(0, 10))
            widget = tk.Text(axes, height=2, width=44, wrap='none', undo=True, exportselection=False)
            value = getattr(self.original, key + '_label')
            value = self.defaults[key] if value is None else value
            widget.insert('1.0', value)
            widget.tag_configure('italic', font=(self.original.english_font, 12, 'italic'))
            widget.tag_configure('super', offset=5, font=(self.original.english_font, 9))
            widget.tag_configure('sub', offset=-3, font=(self.original.english_font, 9))
            widget.tag_configure('symbol', font=(SYMBOL_FONT, 12))
            for tag, suffix in TAGS:
                for start, end in getattr(self.original, key + '_' + suffix):
                    widget.tag_add(tag, text_index(value, start), text_index(value, end))
            widget.grid(row=row, column=1, sticky='ew', pady=4)
            self.labels[key] = widget
            buttons = ttk.Frame(axes)
            buttons.grid(row=row+1, column=1, sticky='w', pady=(0, 8))
            for title, tag in (('斜体', 'italic'), ('上付き', 'super'), ('下付き', 'sub'),
                               ('Symbol指定', 'symbol')):
                ttk.Button(buttons, text=title,
                           command=lambda w=widget, t=tag: self.mark(w, t, True)).pack(side='left', padx=(0, 4))
            ttk.Button(buttons, text='書式を解除',
                       command=lambda w=widget: self.clear_marks(w)).pack(side='left', padx=(6, 4))
            ttk.Button(buttons, text='自動の軸名に戻す', command=lambda k=key: self.reset_label(k)).pack(side='left')
        ttk.Label(axes, text='変数の文字だけ選択して斜体にします。単位・括弧などは正体のままにできます。\n'
                              '「cm-1」の「-1」を上付き、「HIT」の「IT」を下付きにできます。上付き文字をそのまま打つ必要はありません。\n'
                              'ギリシャ文字は入力すると自動でSymbolで表示します。「Symbol指定」では選択範囲にも指定できます。保存される文字は元のままです。\n'
                              '軸名は表示だけを変更します。単位の換算や原本の変更は行いません。', wraplength=680).grid(row=6, column=0, columnspan=2, sticky='w', pady=(0, 10))
        self.variables = {}
        self.preset = tk.StringVar(value=self.original.preset_name)
        ttk.Label(figures, text='図版の用途').grid(row=0, column=0, sticky='w')
        self.preset_box = ttk.Combobox(figures, textvariable=self.preset, state='readonly',
            values=comparison.figure_controls.preset.cget('values'))
        self.preset_box.grid(row=0, column=1, sticky='ew')
        self.preset_box.bind('<<ComboboxSelected>>', self.select_preset)
        self.field(figures, 1, 'width_mm', '書き出す図の幅 mm')
        self.field(figures, 2, 'height_mm', '書き出す図の高さ mm')
        ttk.Label(figures, text='幅と高さは別々に変更できます。窓を広げても書き出す寸法は変わりません。\n'
            '論文の1段・2段は暫定値です。投稿先の規定を確認してください。\n'
            'SVGをPowerPoint側で拡大縮小すると文字も変わります。最終寸法で書き出してください。',
            wraplength=650).grid(row=3, column=0, columnspan=2, sticky='w', pady=15)
        self.as_default = tk.BooleanVar(value=False)
        ttk.Checkbutton(figures, text='保存した個人プリセットを次回も使う', variable=self.as_default).grid(row=4, column=1, sticky='w')
        ttk.Button(figures, text='名前を付けて保存…', command=self.save_preset).grid(row=5, column=0, sticky='ew', pady=8)
        ttk.Button(figures, text='この個人プリセットを更新', command=lambda: self.save_preset(update=True)).grid(row=5, column=1, sticky='ew', pady=8)

        for row, key, title, choices in ((7, 'english_font', '英数字のフォント', ENGLISH_FONTS),
                (8, 'japanese_font', '日本語のフォント', JAPANESE_FONTS)):
            self.field(axes, row, key, title, choices)
        for row, key, title in ((9, 'label_size', '軸名のサイズ pt'), (10, 'tick_size', '目盛りの文字サイズ pt'), (11, 'legend_size', '凡例の文字サイズ pt')):
            self.field(axes, row, key, title)
        self.bold = tk.BooleanVar(value=self.original.labels_bold)
        ttk.Checkbutton(axes, text='軸名を太字にする', variable=self.bold).grid(row=12, column=1, sticky='w', pady=6)
        self.field(axes, 13, 'line_width', '線の太さ pt（0.3〜4）')
        self.field(axes, 14, 'tick_length', '内向き目盛りの長さ pt（1〜15）')
        ttk.Label(axes, text='文字・枠・目盛りは純黒。軸の交点は左下の最小値。補助線なし。\nPCの標準保存は体裁のみ。軸名・凡例文字・系列色は、この比較画面内の設定です。', wraplength=670).grid(row=15, column=0, columnspan=2, sticky='w', pady=8)
        self.field(errors, 0, 'error_mode', 'エラーバーの種類', ERROR_MODES)
        self.field(errors, 1, 'error_direction', '向き', ERROR_DIRECTIONS)
        self.field(errors, 2, 'error_multiplier', '倍率（0.01〜10）')
        self.field(errors, 3, 'error_cap', '端の横棒の半幅 pt（0〜15）')
        ttk.Label(errors, text='散布図・折れ線・棒グラフに適用します。箱ひげ図は反復値の分布を表示します。\n\n標本SDと標準誤差はn≥2の場合に算出します。標準誤差は登録された反復値からの計算で、独立した試料間の誤差とは限りません。\n\n「最小–最大」は倍率1で、平均から最小値・最大値までの範囲を表します。倍率を変えた場合はその距離を拡大・縮小します。\n\nエラーバーの数値を任意入力する機能、信頼区間の自動計算は未対応です。', wraplength=670).grid(row=4, column=0, columnspan=2, sticky='nw', pady=18)
        self.field(legend, 0, 'legend_position', '凡例の位置', LEGEND_POSITIONS)
        self.field(legend, 1, 'legend_x', '自由配置：横位置（0〜100%）')
        self.field(legend, 2, 'legend_y', '自由配置：縦位置（0〜100%）')
        ttk.Label(legend, text='自由配置ではグラフ上の凡例をドラッグして動かすこともできます。\n下の名前は表示用です。保存済みの合金種・作製者名は変わりません。', wraplength=660).grid(row=3, column=0, columnspan=2, sticky='w', pady=10)
        series_frame = ttk.Frame(legend)
        series_frame.grid(row=4, column=0, columnspan=2, sticky='nsew')
        legend.rowconfigure(4, weight=1)
        canvas = tk.Canvas(series_frame, height=225, highlightthickness=0)
        scroll = ttk.Scrollbar(series_frame, orient='vertical', command=canvas.yview)
        canvas.configure(yscrollcommand=scroll.set)
        canvas.pack(side='left', fill='both', expand=True)
        scroll.pack(side='right', fill='y')
        rows = ttk.Frame(canvas)
        canvas.create_window((0, 0), window=rows, anchor='nw')
        rows.bind('<Configure>', lambda event: canvas.configure(scrollregion=canvas.bbox('all')))
        self.series_fields = {}
        for i, title in enumerate(('元の分類', '凡例に表示する文字', '色（#RRGGBB）', '点の形')):
            if i == 3 and getattr(comparison, 'is_curve_tool', False):
                continue
            ttk.Label(rows, text=title).grid(row=0, column=i, sticky='w', padx=4, pady=4)
        for i, name in enumerate(comparison.series_names()):
            current = self.original.series.get(name, {})
            label = tk.StringVar(value=current.get('label', name))
            color = tk.StringVar(value=current.get('color', BLACK))
            shape = tk.StringVar(value=current.get('marker', MARKERS[i % len(MARKERS)]))
            ttk.Label(rows, text=name, width=16, wraplength=120).grid(row=i+1, column=0, sticky='w', padx=4, pady=5)
            ttk.Entry(rows, textvariable=label, width=25).grid(row=i+1, column=1, sticky='ew', padx=4)
            color_box = ttk.Frame(rows)
            color_box.grid(row=i+1, column=2, padx=4)
            ttk.Entry(color_box, textvariable=color, width=9).pack(side='left')
            ttk.Button(color_box, text='選ぶ', width=5, command=lambda v=color: self.pick_color(v)).pack(side='left', padx=2)
            if not getattr(comparison, 'is_curve_tool', False):
                ttk.Combobox(rows, textvariable=shape, values=MARKERS, state='readonly', width=8).grid(row=i+1, column=3, padx=4)
            self.series_fields[name] = label, color, shape
        if getattr(comparison, 'is_curve_tool', False):
            notebook.hide(errors)
        buttons = ttk.Frame(self, padding=(12, 0, 12, 12))
        buttons.grid(row=1, column=0, sticky='ew')
        ttk.Button(buttons, text='体裁をこのPCの標準に保存', command=lambda: self.apply(save=True)).pack(side='left')
        ttk.Button(buttons, text='キャンセル', command=self.destroy).pack(side='right')
        ttk.Button(buttons, text='適用', command=self.apply).pack(side='right', padx=8)
        self.grab_set()

    def field(self, parent, row, key, title, choices=None):
        ttk.Label(parent, text=title).grid(row=row, column=0, sticky='w', padx=(0, 10), pady=3)
        value = tk.StringVar(value=str(getattr(self.original, key)))
        self.variables[key] = value
        widget = ttk.Combobox(parent, textvariable=value, values=choices, state='readonly') if choices else ttk.Entry(parent, textvariable=value)
        widget.grid(row=row, column=1, sticky='ew', pady=3)

    def apply_to_widgets(self, style):
        for key, variable in self.variables.items():
            variable.set(str(getattr(style, key)))
        self.bold.set(style.labels_bold)
        self.preset.set(style.preset_name)
        for key, widget in self.labels.items():
            value = getattr(style, key + '_label')
            value = self.defaults[key] if value is None else value
            widget.delete('1.0', 'end')
            widget.insert('1.0', value)
            for tag, suffix in TAGS:
                for start, end in getattr(style, key + '_' + suffix):
                    widget.tag_add(tag, text_index(value, start), text_index(value, end))
        for name, variables in self.series_fields.items():
            settings = style.series.get(name, {})
            for variable, key in zip(variables, ('label', 'color', 'marker')):
                if key in settings:
                    variable.set(settings[key])

    def select_preset(self, event=None):
        try:
            style = self.comparison.figure_presets.apply(self.preset.get(), self.get_style())
            self.apply_to_widgets(style)
        except (OSError, StoreError, ValueError, TypeError) as exc:
            messagebox.showerror('図のプリセット', str(exc), parent=self)

    def save_preset(self, update=False):
        try:
            style = self.get_style()
            name = self.preset.get() if update else simpledialog.askstring('図の個人プリセット', '保存する名前', parent=self)
            if name is None:
                return
            item = self.comparison.figure_presets.save(name, style, update=update, make_default=self.as_default.get())
            self.preset.set(item['name'])
            self.comparison.figure_controls.reload()
            self.preset_box.configure(values=self.comparison.figure_presets.choices())
            messagebox.showinfo('図のプリセットを保存しました', 'このPCに寸法と体裁を保存しました。軸名・系列名・研究データは含みません。', parent=self)
        except (OSError, StoreError, ValueError, TypeError) as exc:
            messagebox.showerror('図のプリセットを保存できません', str(exc), parent=self)

    def selection(self, widget):
        try:
            return widget.index('sel.first'), widget.index('sel.last')
        except tk.TclError:
            messagebox.showinfo('文字を選択', '軸名の中で、変数などの文字を先に選択してください。', parent=self)
            return None

    def mark(self, widget, tag, enabled=True):
        span = self.selection(widget)
        if span is None:
            return
        if tag in ('super', 'sub'):
            # 同じ文字を上付きと下付きの両方にはできない。
            widget.tag_remove('super' if tag == 'sub' else 'sub', *span)
        widget.tag_add(tag, *span) if enabled else widget.tag_remove(tag, *span)

    def clear_marks(self, widget):
        span = self.selection(widget)
        if span is None:
            return
        for tag, _suffix in TAGS:
            widget.tag_remove(tag, *span)

    def reset_label(self, key):
        self.labels[key].delete('1.0', 'end')
        self.labels[key].insert('1.0', self.defaults[key])

    def pick_color(self, variable):
        try:
            color = colorchooser.askcolor(color=variable.get(), parent=self, title='データ系列の色')[1]
        except tk.TclError:
            messagebox.showerror('色の指定', '#000000 のような形式で色を入力してください。', parent=self)
            return
        if color:
            variable.set(color)

    def get_style(self):
        values = {key: variable.get() for key, variable in self.variables.items()}
        try:
            for key in ('label_size', 'tick_size', 'legend_size', 'width_mm', 'height_mm',
                        'line_width', 'tick_length', 'error_multiplier', 'error_cap', 'legend_x', 'legend_y'):
                values[key] = float(values[key])
        except ValueError as exc:
            raise StoreError('文字サイズ・線幅・位置などを数値で入力してください。') from exc
        values['labels_bold'] = self.bold.get()
        values['preset_name'] = self.preset.get()
        for key, widget in self.labels.items():
            text = widget.get('1.0', 'end-1c')
            marked = False
            for tag, suffix in TAGS:
                ranges = widget.tag_ranges(tag)
                spans = tuple((len(widget.get('1.0', ranges[i])), len(widget.get('1.0', ranges[i+1])))
                              for i in range(0, len(ranges), 2))
                values[key + '_' + suffix] = spans
                marked = marked or bool(spans)
            values[key + '_label'] = None if text == self.defaults[key] and not marked else text
        values['series'] = {**self.original.series, **{name: dict(label=items[0].get(), color=items[1].get(), marker=items[2].get()) for name, items in self.series_fields.items()}}
        return replace(self.original, **values).validate()

    def apply(self, save=False, *, notify=True):
        try:
            style = self.get_style()
            if save:
                save_style(style, self.comparison.style_file)
                if self.comparison.figure_presets.path.exists():
                    self.comparison.figure_presets.set_default(None)
        except (OSError, StoreError) as exc:
            messagebox.showerror('書式を適用できませんでした', str(exc), parent=self)
            return
        missing = missing_fonts(style, font.families(self))
        self.comparison.graph_style = style
        self.comparison.error_mode.set(style.error_mode)
        self.comparison.refresh()
        if missing and notify:
            messagebox.showwarning('指定フォントがありません', 'このPCにないフォント：' + '、'.join(missing) + '\n画面では代替フォントになります。SVGには指定名を保存しますが、閲覧先にもそのフォントが必要です。', parent=self)
        self.destroy()
