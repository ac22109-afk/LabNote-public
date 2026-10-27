"""One preview transform; scene coordinates and exports never use window size."""
import tkinter as tk
from tkinter import messagebox, ttk

from .figure_presets import FigurePresets, FIGURE_PRESETS
from .store import StoreError


class FigureControls(ttk.Frame):
    def __init__(self, owner, parent, preset_file=None):
        super().__init__(parent)
        self.owner = owner
        owner.figure_controls = self
        owner.figure_presets = FigurePresets(preset_file)
        try:
            owner.graph_style = owner.figure_presets.default(owner.graph_style)
        except (OSError, StoreError, ValueError, TypeError):
            # reload below displays the error; preserve the readable style.
            pass
        ttk.Label(self, text='図版の用途').pack(side='left')
        self.preset = ttk.Combobox(self, state='readonly', width=23)
        self.preset.pack(side='left', padx=6)
        self.preset.bind('<<ComboboxSelected>>', self.choose)
        self.mode = ttk.Combobox(self, state='readonly', width=20,
                                values=('全体を表示', '100%（OSのDPI基準）'))
        self.mode.current(0)
        self.mode.pack(side='left', padx=6)
        self.mode.bind('<<ComboboxSelected>>', lambda event: render_scene(owner))
        self.status = ttk.Label(self)
        self.status.pack(side='left', padx=6)
        self.reload()

    def reload(self):
        try:
            choices = self.owner.figure_presets.choices()
        except (OSError, StoreError, ValueError, TypeError) as exc:
            choices = list(FIGURE_PRESETS)
            messagebox.showwarning('図の個人プリセット', str(exc) + '\n規定プリセットのみ表示します。', parent=self.owner)
        self.preset.configure(values=choices)
        self.preset.set(self.owner.graph_style.preset_name)

    def choose(self, event=None):
        try:
            self.owner.graph_style = self.owner.figure_presets.apply(self.preset.get(), self.owner.graph_style)
            self.owner.refresh()
        except (OSError, StoreError, ValueError, TypeError) as exc:
            messagebox.showerror('プリセットを適用できません', str(exc), parent=self.owner)


def preview_canvas(owner, row, height=420):
    frame = ttk.Frame(owner)
    frame.grid(row=row, column=0, sticky='nsew', padx=12)
    frame.columnconfigure(0, weight=1)
    frame.rowconfigure(0, weight=1)
    canvas = tk.Canvas(frame, background='white', highlightthickness=0, height=height)
    horizontal = ttk.Scrollbar(frame, orient='horizontal', command=canvas.xview)
    vertical = ttk.Scrollbar(frame, orient='vertical', command=canvas.yview)
    canvas.configure(xscrollcommand=horizontal.set, yscrollcommand=vertical.set)
    canvas.grid(row=0, column=0, sticky='nsew')
    horizontal.grid(row=1, column=0, sticky='ew')
    vertical.grid(row=0, column=1, sticky='ns')
    return canvas


def scene_point(owner, event):
    scale = owner.preview_scale
    ox, oy = owner.preview_offset
    return (owner.canvas.canvasx(event.x)-ox)/scale, (owner.canvas.canvasy(event.y)-oy)/scale


def render_scene(owner):
    canvas, scene = owner.canvas, owner.scene
    canvas.delete('all')
    if scene is None:
        canvas.create_text(20, 25, text=getattr(owner, 'scene_error', '') or 'データを選択するとグラフを表示します。',
                           anchor='nw', width=max(100, canvas.winfo_width()-40), fill='#000000')
        return
    width, height = max(1, canvas.winfo_width()), max(1, canvas.winfo_height())
    native = canvas.winfo_fpixels('1i') / 72
    fit = owner.figure_controls.mode.current() != 1
    scale = min(max(1, width-10)/scene.width, max(1, height-10)/scene.height) if fit else native
    ox, oy = max(5, (width-scene.width*scale)/2), max(5, (height-scene.height*scale)/2)
    owner.preview_scale, owner.preview_offset = scale, (ox, oy)
    canvas.configure(scrollregion=(0, 0, max(width, scene.width*scale+10), max(height, scene.height*scale+10)))
    owner.figure_controls.preset.set(scene.style.preset_name)
    owner.figure_controls.status.configure(text=f'{scene.width_mm:g} × {scene.height_mm:g} mm / {scale/native:.0%}')
    for (kind, coords, color, fill, text, anchor, record), fmt in zip(scene.items, scene.formats):
        tags = ('record:' + record,) if record else ()
        if fmt['role'] == 'legend':
            tags += ('legend',)
        line_width = max(.2, scene.style.line_width * scale)
        dash = tuple(max(1, round(value*scale)) for value in fmt['dash'])
        if kind == 'circle':
            x, y, radius = coords
            x, y, radius = ox+x*scale, oy+y*scale, radius*scale
            canvas.create_oval(x-radius, y-radius, x+radius, y+radius,
                               outline=color, width=line_width, fill=fill, tags=tags)
            continue
        transformed = [value*scale + (ox if index % 2 == 0 else oy) for index, value in enumerate(coords)]
        if kind == 'text':
            owner.draw_text(scene, transformed, text, anchor, color, fmt, tags)
        elif kind in ('line', 'polyline'):
            canvas.create_line(*transformed, fill=color, width=line_width, dash=dash, tags=tags)
        elif kind == 'rect':
            canvas.create_rectangle(*transformed, outline=color, width=line_width,
                                    fill='' if fill == 'none' else fill, tags=tags)
        elif kind == 'polygon':
            canvas.create_polygon(*transformed, outline=color, width=line_width, fill=fill, tags=tags)
