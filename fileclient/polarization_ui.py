"""Local read-only curve workbench; adding a curve never registers a measurement."""
import tkinter as tk
from dataclasses import replace
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from .comparison import export_svg
from .figure_preview import FigureControls, preview_canvas, render_scene
from .comparison_ui import ComparisonDialog
from .graph_style import load_style
from .polarization import POTENTIAL_UNITS, CURRENT_UNITS, LAYOUTS, make_curve, curve_scene, export_curve_csv
from .runtime import profile_directory
from .store import StoreError, identifier
from .tables import read_table


class PolarizationDialog(tk.Toplevel):
    is_curve_tool = True
    draw_text = ComparisonDialog.draw_text
    drag_legend = ComparisonDialog.drag_legend
    press_chart = ComparisonDialog.press_chart
    click_chart = lambda self, event: None

    def __init__(self, app, *, style_file=None, preset_file=None):
        super().__init__(app.root)
        self.app, self.curves, self.table = app, [], None
        self.scene, self.legend_drag, self.font_cache = None, None, {}
        self.style_file = style_file or profile_directory() / 'polarization-style.json'
        self.graph_style = load_style(self.style_file)
        self.selected_rows = []
        self.error_mode = tk.StringVar(value='なし')
        self.title('分極曲線 — Excel／CSVから作図')
        self.geometry('1180x850')
        self.minsize(1000, 730)
        self.transient(app.root)
        self.protocol('WM_DELETE_WINDOW', self.close)
        self.columnconfigure(0, weight=1)
        self.rowconfigure(6, weight=1)
        ttk.Label(self, text='元データの列・単位・参照電極を確認して追加します。読み込みだけでは研究記録を登録・変更しません。', padding=12).grid(row=0, column=0, sticky='ew')
        files = ttk.Frame(self, padding=(12, 0))
        files.grid(row=1, column=0, sticky='ew')
        ttk.Button(files, text='Excel／CSVを読む…', command=self.choose).pack(side='left')
        ttk.Button(files, text='主画面で選択した添付を読む', command=self.attachment).pack(side='left', padx=8)
        self.filename = ttk.Label(files, text='未選択')
        self.filename.pack(side='left', padx=8)
        inputs = ttk.Frame(self, padding=12)
        inputs.grid(row=2, column=0, sticky='ew')
        self.fields = {}
        specifications = [('e_col', '電位の列', ()), ('i_col', '電流／電流密度の列', ()),
            ('e_unit', '元の電位単位', tuple(POTENTIAL_UNITS)), ('i_unit', '元の電流単位', tuple(CURRENT_UNITS)),
            ('area', '露出面積 cm²（電流の場合）', None), ('reference', '参照電極（例：Ag/AgCl）', None),
            ('name', '凡例名', None), ('sheet', 'Excelシート名（空欄：先頭）', None)]
        for index, (key, label, choices) in enumerate(specifications):
            row, col = index % 4, index//4*2
            ttk.Label(inputs, text=label).grid(row=row, column=col, sticky='w', padx=5, pady=3)
            widget = ttk.Combobox(inputs, state='readonly', values=choices, width=25) if choices is not None else ttk.Entry(inputs, width=28)
            widget.grid(row=row, column=col+1, sticky='ew', padx=5, pady=3)
            self.fields[key] = widget
        self.fields['sheet'].bind('<Return>', lambda event: self.reload_sheet())
        ttk.Button(inputs, text='この列・単位で曲線を追加', command=self.add_curve).grid(row=4, column=0, columnspan=2, sticky='w', padx=5, pady=8)
        ttk.Button(inputs, text='指定したシートを読み直す', command=self.reload_sheet).grid(row=4, column=2, columnspan=2, sticky='w', padx=5)
        options = ttk.Frame(self, padding=(12, 0))
        options.grid(row=3, column=0, sticky='ew')
        self.layout = ttk.Combobox(options, state='readonly', values=LAYOUTS, width=34)
        self.layout.set(LAYOUTS[0])
        self.layout.pack(side='left')
        self.layout.bind('<<ComboboxSelected>>', self.change_axes)
        self.logarithmic = tk.BooleanVar(value=True)
        ttk.Checkbutton(options, text='電流密度を対数表示（絶対値）', variable=self.logarithmic, command=self.change_axes).pack(side='left', padx=10)
        ttk.Button(options, text='軸名・凡例・書式…', command=self.format_graph).pack(side='left')
        ttk.Button(options, text='曲線をすべて外す', command=self.clear).pack(side='right')
        FigureControls(self, self, preset_file).grid(row=4, column=0, sticky='ew', padx=12, pady=6)
        self.curve_list = ttk.Label(self, text='曲線なし。単位と参照電極を確認してから追加してください。', padding=12, wraplength=1100)
        self.curve_list.grid(row=5, column=0, sticky='ew')
        self.canvas = preview_canvas(self, 6)
        self.canvas.bind('<Configure>', lambda event: render_scene(self))
        self.canvas.bind('<ButtonPress-1>', self.press_chart)
        self.canvas.bind('<B1-Motion>', self.drag_legend)
        self.canvas.bind('<ButtonRelease-1>', lambda event: setattr(self, 'legend_drag', None))
        bottom = ttk.Frame(self, padding=12)
        bottom.grid(row=7, column=0, sticky='ew')
        self.status = ttk.Label(bottom, text='符号付き元データと取得順を保持します。参照電極の換算・腐食電流の自動推定は行いません。', wraplength=760)
        self.status.pack(side='left')
        self.svg_button = ttk.Button(bottom, text='SVG保存', command=lambda: self.export('svg'), state='disabled')
        self.svg_button.pack(side='right')
        ttk.Button(bottom, text='元データ・換算値CSV', command=lambda: self.export('csv')).pack(side='right', padx=8)

    def choose(self):
        path = filedialog.askopenfilename(parent=self, title='分極曲線の元データ', filetypes=[('Excel／CSV', '*.xlsx *.csv *.tsv *.txt'), ('すべて', '*.*')])
        if path:
            self.fields['sheet'].delete(0, 'end')
            self.load_path(Path(path))

    def close(self):
        if not self.app.job:
            self.destroy()

    def load_path(self, path):
        if self.app.job:
            return
        self.table = None
        sheet = self.fields['sheet'].get().strip()
        def done(table):
            self.path, self.table = path, table
            self.filename.configure(text=f'{path.name} ／ {len(table.rows)}行')
            for key in ('e_col', 'i_col'):
                self.fields[key].configure(values=table.headers)
                self.fields[key].set('')
            for key, words in [('e_col', ('potential', '電位', 'e/v', 'e (v)')), ('i_col', ('current', '電流', 'j/', 'i/a'))]:
                candidates = [column for column in table.headers if any(word in column.lower() for word in words)]
                if len(candidates) == 1:
                    self.fields[key].set(candidates[0])
            self.fields['name'].delete(0, 'end')
            self.fields['name'].insert(0, path.stem[:60])
            self.fields['sheet'].delete(0, 'end')
            self.fields['sheet'].insert(0, table.sheet)
        self.app.run_job('曲線の表を読み込んでいます…', lambda: read_table(path, sheet=sheet or None, max_rows=100000), done)

    def reload_sheet(self):
        if hasattr(self, 'path'):
            self.load_path(self.path)

    def attachment(self):
        if self.app.job:
            return
        try:
            item, attachment = self.app.selected(self.app.attachments, self.app.attachment_items)
            from .attachments import original_filename
            name = original_filename(attachment)
            if Path(name).suffix.lower() not in {'.csv', '.tsv', '.txt', '.xlsx'}:
                raise StoreError('主画面でExcel／CSVの添付を一つ選んでください。')
            directory = profile_directory() / 'curve-inputs' / identifier()
            directory.mkdir(parents=True)
            destination = directory / name
            store = self.app.store
            self.fields['sheet'].delete(0, 'end')
            self.app.run_job('添付を照合してPC内へ読み込んでいます…', lambda: store.export_attachment(item, attachment['id'], destination), lambda result: self.load_path(destination))
        except (OSError, StoreError) as exc:
            messagebox.showerror('添付を選択してください', str(exc), parent=self)

    def add_curve(self):
        if self.table is None or self.app.job:
            return
        try:
            if len(self.curves) >= 8:
                raise StoreError('同時表示は8曲線までです。')
            fields = {key: widget.get() for key, widget in self.fields.items()}
            curve = make_curve(self.table, fields['name'], fields['e_col'], fields['i_col'], fields['e_unit'], fields['i_unit'], area=fields['area'], reference=fields['reference'])
            if any(existing.name == curve.name for existing in self.curves):
                raise StoreError('凡例名が重複しています。別の名前にしてください。')
            if self.curves and self.curves[0].reference != curve.reference:
                raise StoreError('参照電極が異なります。別の図として作成してください。')
            # Validate the chosen transformation before accepting this curve.
            curve_scene(self.curves+[curve], layout=self.layout.get(), logarithmic=self.logarithmic.get(), style=self.graph_style)
            self.curves.append(curve)
            self.curve_list.configure(text=' ／ '.join(f'{curve.name}（{len(curve.points)}点）' for curve in self.curves))
            self.draw()
        except (StoreError, ValueError) as exc:
            messagebox.showerror('曲線を追加できませんでした', str(exc), parent=self)

    def axis_defaults(self):
        reference = self.curves[0].reference if self.curves else '参照電極'
        current = ('|j|' if self.logarithmic.get() else 'j') + ' / (μA/cm²)'
        potential = 'E / V vs. ' + reference
        x, y = (current, potential) if self.layout.get() == LAYOUTS[0] else (potential, current)
        return {'x': x, 'y': y}

    def change_axes(self, event=None):
        self.graph_style = replace(self.graph_style, x_label=None, y_label=None, x_italics=(), y_italics=())
        self.draw()

    def series_names(self):
        return [curve.name for curve in self.curves]

    def format_graph(self):
        from .graph_style_ui import GraphStyleDialog
        GraphStyleDialog(self)

    def clear(self):
        self.curves.clear()
        self.curve_list.configure(text='曲線なし。元ファイル・研究記録は変更していません。')
        self.draw()

    def refresh(self):
        self.draw()

    def draw(self):
        self.canvas.delete('all')
        self.scene = None
        try:
            scene = curve_scene(self.curves, layout=self.layout.get(), logarithmic=self.logarithmic.get(), style=self.graph_style)
        except StoreError as exc:
            self.scene_error = str(exc)
            render_scene(self)
            self.svg_button.configure(state='disabled')
            return
        self.scene = scene
        self.svg_button.configure(state='normal')
        render_scene(self)
        zero = sum(point[4] == 0 for curve in self.curves for point in curve.points)
        self.status.configure(text=f'全点を取得順で表示。' + (f'対数表示は絶対値を使用し、0の{zero}点は描画せず区切ります。符号の切替も線を接続しません。' if self.logarithmic.get() else '線形表示は符号を保持します。') + ' 元データはCSVへ保存できます。')

    def export(self, mode):
        if not self.curves or (mode == 'svg' and not self.scene):
            return
        destination = filedialog.asksaveasfilename(parent=self, title='分極曲線を新しいファイルに保存', initialfile='分極曲線.'+mode, defaultextension='.'+mode)
        if destination:
            try:
                export_svg(self.scene, destination) if mode == 'svg' else export_curve_csv(self.curves, destination)
                messagebox.showinfo('保存しました', '元の研究記録・ファイルは変更していません。', parent=self)
            except (OSError, StoreError, ValueError) as exc:
                messagebox.showerror('保存できませんでした', str(exc), parent=self)
