"""Physical figure geometry shared by comparison and polarization scenes."""
import math
from dataclasses import dataclass

from .graph_style import text_width_pt
from .store import StoreError


@dataclass(frozen=True)
class FigureLayout:
    left: float
    right: float
    top: float
    bottom: float
    x_tick_y: float
    x_label_y: float
    y_label_x: float
    legend_width: float
    legend_height: float
    legend_columns: int
    legend_cell: float
    legend_step: float


def figure_layout(scene, y_ticks, x_ticks, legends, *, x_inset=0):
    style = scene.style
    pad = max(5, style.line_width * 3)
    gap = max(3, style.tick_size * .4)
    y_width = max((text_width_pt(value, style.tick_size) for value in y_ticks), default=0)
    x_half = max((text_width_pt(value, style.tick_size)/2 for value in x_ticks), default=0)
    left = pad + style.label_size * 1.4 + gap + y_width + gap
    right_pad = pad
    # Category ticks are inset into the plot; reserving half a label at each
    # page edge wastes most of a small figure, especially for a single sample.
    inset = min(.45, max(0, x_inset))
    for _ in range(64):
        inner = max(0, scene.width-left-right_pad)
        extra_left = max(0, x_half+pad-left-inset*inner)
        extra_right = max(0, x_half+pad-right_pad-inset*inner)
        left += extra_left
        right_pad += extra_right
        if max(extra_left, extra_right) < .001:
            break
    right = scene.width - right_pad
    bottom_gap = pad + style.label_size * 1.4 + style.tick_size * 1.6 + gap
    bottom = scene.height - bottom_gap
    top = pad + style.tick_size * .7
    available = right - left
    visible = style.legend_position != '非表示'
    cell = max((text_width_pt(value, style.legend_size) + style.legend_size * 2
                for value in legends), default=0)
    columns = max(1, min(3, len(legends), int(available / max(1, cell))))
    rows = math.ceil(len(legends) / columns) if visible else 0
    step = style.legend_size * 1.6
    legend_width, legend_height = cell * columns, rows * step
    if visible and cell > available:
        raise StoreError('凡例が図の幅に収まりません。凡例を短くするか、書式で幅 mmを広げてください。')
    if visible and style.legend_position == '上（枠外）':
        top += legend_height + gap
    if visible and style.legend_position == '下（枠外）':
        bottom -= legend_height + gap
    if available < max(40, style.tick_size * 4) or bottom-top < max(35, style.tick_size * 3):
        raise StoreError('文字・凡例を置く余白が足りません。書式で図の幅・高さ mmを広げるか文字を小さくしてください。')
    if visible and '枠内' in style.legend_position and legend_height > bottom-top-gap*2:
        raise StoreError('凡例が作図領域に収まりません。高さ mmを広げるか凡例を枠外へ移してください。')
    result = FigureLayout(left, right, top, bottom, bottom + gap + style.tick_size * .6,
        bottom + gap * 2 + style.tick_size * 1.4 + style.label_size * .65,
        pad + style.label_size * .6, legend_width, legend_height, columns, cell, step)
    scene.layout = result
    return result


def legend_origin(scene):
    layout, style = scene.layout, scene.style
    position = style.legend_position
    pad = 5
    lx = layout.left + (layout.right-layout.left-layout.legend_width)/2
    ly = pad
    if position == '下（枠外）':
        ly = scene.height - pad - layout.legend_height
    elif position == '自由配置':
        lx = pad + (scene.width-layout.legend_width-pad*2)*style.legend_x/100
        ly = pad + (scene.height-layout.legend_height-pad*2)*style.legend_y/100
    elif '枠内' in position:
        lx = layout.left + pad if position.startswith('左') else layout.right-layout.legend_width-pad
        ly = layout.top + pad if '上' in position else layout.bottom-layout.legend_height-pad
    scene.legend_bounds = lx, ly, lx+layout.legend_width, ly+layout.legend_height
    return lx, ly


def legend_entries(scene, names):
    lx, ly = legend_origin(scene)
    layout = scene.layout
    for index, name in enumerate(names):
        yield name, lx + index % layout.legend_columns * layout.legend_cell, \
            ly + (index // layout.legend_columns + .5) * layout.legend_step
