"""Explicit-unit polarization curves; retain acquisition order and signed raw data."""
import csv
import math
from dataclasses import asdict, dataclass, replace

from .comparison import Scene
from .graph_style import GraphStyle, BLACK
from .figure_layout import figure_layout, legend_entries
from .store import StoreError, export_pending_path, publish_export

POTENTIAL_UNITS = {'V': 1, 'mV': .001}
CURRENT_UNITS = {'A/cm²': (1, True), 'mA/cm²': (.001, True), 'μA/cm²': (1e-6, True),
                 'A': (1, False), 'mA': (.001, False), 'μA': (1e-6, False)}
LAYOUTS = ('電流密度を横軸・電位を縦軸', '電位を横軸・電流密度を縦軸')


@dataclass(frozen=True)
class Curve:
    name: str
    source_hash: str
    sheet: str
    potential_column: str
    current_column: str
    potential_unit: str
    current_unit: str
    area: float | None
    reference: str
    points: tuple  # source row, original E, original I/j, E [V], j [A/cm²]


def make_curve(table, name, potential_column, current_column, potential_unit, current_unit, *, area=None, reference=''):
    if potential_column == current_column or potential_column not in table.headers or current_column not in table.headers:
        raise StoreError('電位と電流の異なる列を指定してください。')
    if potential_unit not in POTENTIAL_UNITS or current_unit not in CURRENT_UNITS:
        raise StoreError('元データの単位を選んでください。')
    reference = reference.strip()
    if not reference or len(reference) > 100:
        raise StoreError('参照電極を記入してください（不明なら「不明」と明記）。')
    name = name.strip()
    if not name or len(name) > 60:
        raise StoreError('凡例名を60文字以内で入力してください。')
    scale, density = CURRENT_UNITS[current_unit]
    if not density:
        try:
            area = float(area)
        except (TypeError, ValueError) as exc:
            raise StoreError('電流から電流密度へ換算するため露出面積cm²が必要です。') from exc
        if not math.isfinite(area) or area <= 0:
            raise StoreError('露出面積は0より大きい有限値で入力してください。')
    else:
        area = None
    e_index, i_index = table.headers.index(potential_column), table.headers.index(current_column)
    points = []
    for row_number, row in enumerate(table.rows, 2):
        if not any(row):
            continue
        try:
            e, current = float(row[e_index]), float(row[i_index])
            ev, j = e * POTENTIAL_UNITS[potential_unit], current * scale / (area or 1)
        except (ValueError, OverflowError) as exc:
            raise StoreError(f'{row_number}行目の電位・電流が数値ではありません。列を確認してください。') from exc
        if not all(math.isfinite(value) for value in (e, current, ev, j)):
            raise StoreError(f'{row_number}行目に無限大・NaN・換算範囲外の値があります。')
        points.append((row_number, e, current, ev, j))
    if not 2 <= len(points) <= 100000:
        raise StoreError('曲線は2〜100000点で指定してください。')
    return Curve(name, table.sha256, table.sheet, potential_column, current_column,
                 potential_unit, current_unit, area, reference, tuple(points))


def coordinates(curve, *, layout=LAYOUTS[0], logarithmic=True):
    if layout not in LAYOUTS:
        raise StoreError('軸の向きを選んでください。')
    result, previous_sign = [], None
    for row, raw_e, raw_i, ev, j in curve.points:
        if logarithmic and j == 0:
            result.append(None)
            previous_sign = None
            continue
        sign = j > 0
        if logarithmic and previous_sign is not None and sign != previous_sign:
            result.append(None)
        previous_sign = sign
        current = math.log10(abs(j)) + 6 if logarithmic else j * 1e6
        if not math.isfinite(current):
            raise StoreError('表示単位への換算が数値範囲を超えています。')
        result.append((current, ev) if layout == LAYOUTS[0] else (ev, current))
    if sum(point is not None for point in result) < 2:
        raise StoreError('対数表示で使える0以外の点が2点ありません。線形表示を試してください。')
    return result


def curve_scene(curves, *, layout=LAYOUTS[0], logarithmic=True, style=None, width_mm=None, height_mm=None):
    if not curves or len(curves) > 8:
        raise StoreError('曲線を1〜8本追加してください。')
    if len({curve.reference for curve in curves}) != 1:
        raise StoreError('参照電極が異なる曲線は重ねられません。参照電極を確認し、別の図にしてください。')
    if len(curves) > 1 and curves[0].reference.casefold() in {'不明', 'unknown'}:
        raise StoreError('参照電極が不明な曲線は、同じ条件と確認できないため重ねられません。')
    style = (style or GraphStyle()).validate()
    style = replace(style, width_mm=style.width_mm if width_mm is None else width_mm,
                    height_mm=style.height_mm if height_mm is None else height_mm).validate()
    scene = Scene(style.width_mm, style.height_mm, style)
    mapped = [coordinates(curve, layout=layout, logarithmic=logarithmic) for curve in curves]
    points = [point for series in mapped for point in series if point is not None]
    xmin, xmax = min(p[0] for p in points), max(p[0] for p in points)
    ymin, ymax = min(p[1] for p in points), max(p[1] for p in points)
    def limits(low, high):
        span = high-low or max(abs(high)*.1, 1)
        low, high = low - span*.05, high + span*.05
        if not all(math.isfinite(v) for v in (span, low, high)) or span <= 0:
            raise StoreError('軸の数値範囲が大きすぎます。元データと単位を確認してください。')
        return low, high
    xmin, xmax = limits(xmin, xmax)
    ymin, ymax = limits(ymin, ymax)
    log_x = layout == LAYOUTS[0] and logarithmic
    log_y = layout == LAYOUTS[1] and logarithmic
    def tick_label(value, logarithmic_axis):
        return f'10^{value:.2g}' if logarithmic_axis else f'{value:.4g}'
    x_ticks = [tick_label(xmin+(xmax-xmin)*index/5, log_x) for index in range(6)]
    y_ticks = [tick_label(ymin+(ymax-ymin)*index/5, log_y) for index in range(6)]
    legends = [style.series.get(curve.name, {}).get('label', curve.name) for curve in curves]
    geometry = figure_layout(scene, y_ticks, x_ticks, legends)
    left, right, top, bottom = geometry.left, geometry.right, geometry.top, geometry.bottom
    px = lambda x: left+(x-xmin)/(xmax-xmin)*(right-left)
    py = lambda y: bottom-(y-ymin)/(ymax-ymin)*(bottom-top)
    for index in range(6):
        x, y = xmin+(xmax-xmin)*index/5, ymin+(ymax-ymin)*index/5
        xx, yy = px(x), py(y)
        scene.add('line', (xx, bottom, xx, bottom-style.tick_length), role='x_tick')
        scene.add('line', (left, yy, left+style.tick_length, yy), role='y_tick')
        scene.add('text', (xx, geometry.x_tick_y), text=tick_label(x, log_x), role='tick_label')
        scene.add('text', (left-style.tick_size*.4, yy), text=tick_label(y, log_y), anchor='end', role='tick_label')
    scene.add('rect', (left, top, right, bottom), role='frame')
    current_label = ('|j|' if logarithmic else 'j') + ' / (μA/cm²)'
    potential_label = 'E / V vs. ' + curves[0].reference
    default_x, default_y = (current_label, potential_label) if layout == LAYOUTS[0] else (potential_label, current_label)
    scene.add('text', ((left+right)/2, geometry.x_label_y), text=style.x_label if style.x_label is not None else default_x, size=style.label_size, bold=style.labels_bold, italics=style.x_italics, supers=style.x_supers, subs=style.x_subs, symbols=style.x_symbols, role='axis_label')
    scene.add('text', (geometry.y_label_x, (top+bottom)/2), text=style.y_label if style.y_label is not None else default_y, size=style.label_size, bold=style.labels_bold, italics=style.y_italics, supers=style.y_supers, subs=style.y_subs, symbols=style.y_symbols, angle=90, role='axis_label')
    for index, (curve, series) in enumerate(zip(curves, mapped)):
        settings = style.series.get(curve.name, {})
        color = settings.get('color', BLACK)
        segment = []
        for point in series + [None]:
            if point is None:
                if len(segment) >= 4:
                    scene.add('polyline', tuple(segment), color=color, record=curve.name, role='data', dash=((), (7, 3), (2, 3), (8, 3, 2, 3))[index % 4])
                elif segment:
                    scene.add('circle', (segment[0], segment[1], 2), color=color, fill=color, record=curve.name, role='data')
                segment = []
            else:
                segment.extend((px(point[0]), py(point[1])))
    if style.legend_position != '非表示':
        for index, (name, x, y) in enumerate(legend_entries(scene, [curve.name for curve in curves])):
            settings = style.series.get(name, {})
            scene.add('line', (x, y, x+style.legend_size, y), color=settings.get('color', BLACK), role='legend',
                      dash=((), (7, 3), (2, 3), (8, 3, 2, 3))[index%4])
            scene.add('text', (x+style.legend_size*1.3, y), text=settings.get('label', name),
                      anchor='start', size=style.legend_size, role='legend')
    scene.metadata = {'format': 'labnote-polarization-figure', 'version': 1, 'layout': layout,
        'log_absolute_current': logarithmic, 'signed_raw_data_preserved': True, 'acquisition_order_preserved': True,
        'style': asdict(style), 'curves': [{key: value for key, value in asdict(curve).items() if key != 'points'} | {'points': len(curve.points)} for curve in curves]}
    return scene


def export_curve_csv(curves, destination):
    pending = export_pending_path(destination)
    def safe(value):
        value = str(value)
        return "'" + value if value.lstrip().startswith(('=', '+', '-', '@')) else value
    with pending.open('x', encoding='utf-8-sig', newline='') as stream:
        writer = csv.writer(stream)
        writer.writerow(['系列', '元ファイルSHA256', 'シート', '元行', '元電位', '元電位単位', '元電流', '元電流単位', '露出面積cm²', '参照電極', '電位V', '電流密度A/cm²'])
        for curve in curves:
            for row, raw_e, raw_i, ev, j in curve.points:
                writer.writerow([safe(curve.name), curve.source_hash, safe(curve.sheet), row, raw_e, curve.potential_unit,
                                 raw_i, curve.current_unit, curve.area or '', safe(curve.reference), ev, j])
    publish_export(pending, destination)
