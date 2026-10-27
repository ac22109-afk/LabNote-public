"""Read-only measurement comparisons, derived from one loaded snapshot.

Never infer a measurement's treatment state from a sample's treatment history.
One row is one measurement revision; repeat values are not independent samples.
"""
import csv
import json
import math
import os
import statistics
from dataclasses import asdict, dataclass, replace
from datetime import date
from decimal import Decimal
from html import escape

from .store import StoreError, export_pending_path, normalized, publish_export
from .graph_style import (BLACK, MARKERS, SCRIPT_DROP, SCRIPT_RISE, SCRIPT_SIZE, SYMBOL_FONT,
                          GraphStyle, PT_PER_MM, error_extent, font_family, font_runs, symbol_text, text_width_pt)
from .figure_layout import figure_layout, legend_entries

KINDS = ('散布図', '折れ線', '棒グラフ', '箱ひげ図')
AXES = {'試料ID': 'label', '合金種': 'alloy', '測定日': 'measured_on',
        '測定日（実日付）': 'measured_on'}
# Equal-interval category axes hide the gaps between experiment days; this one
# places each measurement at its real date.
CONTINUOUS_AXES = {'測定日（実日付）'}
GROUPS = {'合金種': 'alloy', '材料': 'material', '研磨条件': 'polishing',
          '溶体化処理': 'solution_treated', '作製者': 'creator', '測定者': 'measurer'}
COLORS = ('#14634f', '#386fb0', '#b65b22', '#8255a3', '#b83868', '#517738',
          '#477b85', '#8c602f', '#644080', '#c15243', '#455267', '#737528')


@dataclass(frozen=True)
class ComparisonRow:
    sample_id: str
    sample_revision: str
    measurement_id: str
    measurement_revision: str
    label: str
    material: str
    alloy: str
    polishing: str
    solution_treated: bool
    creator: str
    measurer: str
    method: str
    metric: str
    unit: str
    measured_on: str
    values: tuple
    state: str

    @property
    def signature(self):
        # Unit case matters (mV is not MV). No implicit conversions.
        return self.method, self.metric, self.unit

    @property
    def mean(self):
        return statistics.mean(self.values)

    @property
    def sd(self):
        return statistics.stdev(self.values) if len(self.values) > 1 else None


REASONS = ('競合・要確認', '表示できない状態', '関連試料不明', '数値なし', '項目・単位なし')


def plottable_samples(snapshot):
    return {key: heads[0] for key, heads in snapshot.heads.items()
            if len(heads) == 1 and key not in snapshot.blocked and heads[0]['kind'] == 'sample'}


def measurement_problem(snapshot, key, heads, samples):
    """Why a measurement cannot be plotted. None means it can, '' means it is hidden."""
    if key in snapshot.blocked or len(heads) != 1:
        return '競合・要確認'
    item = heads[0]
    if snapshot.hidden(item):
        # ごみ箱にある記録は図に出なくて当然なので黙って外す。それ以外の理由で
        # 隠れている記録は、どの画面からも見えなくならないよう理由として返す。
        parent = samples.get(item['data']['sample_id'])
        if (snapshot.status(item)['state'] == 'deleted'
                or (parent is not None and snapshot.status(parent)['state'] == 'deleted')):
            return ''
        return '表示できない状態'
    data = item['data']
    if samples.get(data['sample_id']) is None:
        return '関連試料不明'
    if not data['values']:
        return '数値なし'
    if not data['metric'] or not data['unit']:
        return '項目・単位なし'
    return None


def unplotted_measurements(snapshot, actor=''):
    """Measurements the comparison screen drops, with the reason.

    The snapshot is the same one the comparison screen uses, so records still
    waiting in this PC's queue are included and carry their own saving state.
    """
    samples = plottable_samples(snapshot)
    who = normalized(actor)
    found = []
    for key, heads in snapshot.heads.items():
        if not any(item['kind'] == 'measurement' for item in heads):
            continue
        reason = measurement_problem(snapshot, key, heads, samples)
        if not reason:
            continue
        item = heads[0]
        if who and who not in {normalized(item['registrant']),
                               normalized(item['data'].get('measurer') or '')}:
            continue
        found.append({'id': key, 'item': item, 'reason': reason})
    return sorted(found, key=lambda row: (REASONS.index(row['reason']), row['id']))


def comparison_rows(snapshot, state=None):
    rows, excluded = [], {reason: 0 for reason in REASONS}
    samples = plottable_samples(snapshot)
    for key, heads in snapshot.heads.items():
        if not any(item['kind'] == 'measurement' for item in heads):
            continue
        reason = measurement_problem(snapshot, key, heads, samples)
        if reason is not None:
            if reason:
                excluded[reason] += 1
            continue
        item = heads[0]
        data = item['data']
        sample = samples[data['sample_id']]
        values = tuple(float(Decimal(value)) for value in data['values'])
        if not all(math.isfinite(value) for value in values):
            raise StoreError('比較できない数値があります。記録を確認してください。')
        sd = sample['data']
        rows.append(ComparisonRow(sample['id'], sample['revision_id'], key, item['revision_id'],
            sd['label'], sd['material'], sd['alloy'], sd['polishing'],
            sd['solution_treated'], sd['creator'], data['measurer'],
            data['method'], data['metric'], data['unit'], data['measured_on'], values,
            state(item) if state else '未判定'))
    return sorted(rows, key=lambda row: (row.measured_on, row.label, row.measurement_id)), excluded


def select_rows(rows, signature, *, alloy='', creator='', measurer='', since='', until=''):
    # measured_on is ISO 8601, so text order is date order. Empty bounds stay open.
    for bound in (since, until):
        if bound:
            try:
                date.fromisoformat(bound)
            except ValueError as exc:
                raise StoreError('測定日の範囲は YYYY-MM-DD で入力してください。') from exc
    if since and until and since > until:
        raise StoreError('測定日の範囲は開始日が終了日より後になっています。')
    return [row for row in rows if row.signature == signature and
            (not alloy or row.alloy == alloy) and (not creator or row.creator == creator) and
            (not measurer or row.measurer == measurer) and
            (not since or row.measured_on >= since) and
            (not until or row.measured_on <= until)]


def series_of(row, group):
    """Sample attributes carry no treatment order, so they group measurements safely."""
    value = getattr(row, GROUPS[group])
    if isinstance(value, bool):
        return '溶体化処理あり' if value else '溶体化処理なし'
    return value or '未設定'


def quartiles(values):
    """Linear interpolation at (n-1)*p; whiskers in the UI are min/max."""
    values = sorted(values)
    def quantile(p):
        index = (len(values) - 1) * p
        low, high = math.floor(index), math.ceil(index)
        return values[low] + (values[high] - values[low]) * (index - low)
    return values[0], quantile(.25), quantile(.5), quantile(.75), values[-1]


def checked_rows(rows):
    if not rows:
        raise StoreError('条件に合う数値データがありません。測定項目・単位と絞り込みを確認してください。')
    if len({row.signature for row in rows}) != 1:
        raise StoreError('測定方法・項目・単位が異なる記録は同じグラフにできません。')


class Scene:
    """Same explicit primitives for Tk display and standalone SVG export."""
    def __init__(self, width_mm, height_mm, style=None):
        self.width_mm, self.height_mm = width_mm, height_mm
        self.width, self.height, self.items = width_mm * PT_PER_MM, height_mm * PT_PER_MM, []
        self.style = style or GraphStyle()
        self.formats = []
        self.legend_bounds = None
        self.metadata = {}

    def add(self, kind, coords, *, color=BLACK, fill='none', text='', anchor='middle', record=None,
            size=None, bold=False, italics=(), supers=(), subs=(), symbols=(), angle=0, role='', dash=()):
        if kind == 'text' and role == 'axis_label':
            extent = text_width_pt(text, size or self.style.tick_size,
                                  italics=italics, supers=supers, subs=subs, symbols=symbols) / 2
            half_height = (size or self.style.tick_size) * .65
            dx, dy = (half_height, extent) if angle == 90 else (extent, half_height)
            if coords[0]-dx < 0 or coords[0]+dx > self.width or coords[1]-dy < 0 or coords[1]+dy > self.height:
                raise StoreError('軸名が図の中に収まりません。軸名を短くするか、図の幅・高さ mmを広げてください。')
        self.items.append((kind, coords, color, fill, text, anchor, record))
        self.formats.append(dict(size=size or self.style.tick_size, bold=bold, italics=italics,
                                 supers=supers, subs=subs, symbols=symbols,
                                 angle=angle, role=role, dash=dash))

    def svg(self):
        out = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{self.width_mm:g}mm" height="{self.height_mm:g}mm" viewBox="0 0 {self.width} {self.height}">',
               '<rect width="100%" height="100%" fill="white"/>',
               '<metadata>' + escape(json.dumps(self.metadata, ensure_ascii=False)) + '</metadata>', '<g>']
        for (kind, coords, color, fill, text, anchor, record), fmt in zip(self.items, self.formats):
            style = f'stroke="{escape(color, quote=True)}" fill="{escape(fill, quote=True)}" stroke-width="{self.style.line_width}"'
            if fmt['dash']:
                style += ' stroke-dasharray="' + ' '.join(str(value) for value in fmt['dash']) + '"'
            coords = [round(value, 3) for value in coords]
            if kind == 'line':
                x1, y1, x2, y2 = coords
                element = f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" {style}/>'
            elif kind == 'rect':
                x1, y1, x2, y2 = coords
                element = f'<rect x="{x1}" y="{y1}" width="{x2-x1}" height="{y2-y1}" {style}/>'
            elif kind == 'circle':
                x, y, radius = coords
                element = f'<circle cx="{x}" cy="{y}" r="{radius}" {style}/>'
            elif kind in {'polygon', 'polyline'}:
                points = ' '.join(f'{coords[i]},{coords[i+1]}' for i in range(0, len(coords), 2))
                element = f'<{kind} points="{points}" {style}/>'
            else:
                x, y = coords
                transform = f' transform="rotate({-fmt["angle"]} {x} {y})"' if fmt['angle'] else ''
                element = f'<text x="{x}" y="{y}" xml:space="preserve" style="white-space:pre" dominant-baseline="central" text-anchor="{anchor}" fill="{escape(color, quote=True)}" font-size="{fmt["size"]}" font-weight="{"bold" if fmt["bold"] else "normal"}"{transform}>'
                shift = 0
                for run, language, italic, script, symbol in font_runs(
                        text, fmt['italics'], fmt['supers'], fmt['subs'], fmt['symbols']):
                    family = SYMBOL_FONT if symbol else (
                        self.style.japanese_font if language == 'jp' else self.style.english_font)
                    size = fmt['size'] * SCRIPT_SIZE if script else fmt['size']
                    wanted = (-fmt['size'] * SCRIPT_RISE if script == 'super'
                              else fmt['size'] * SCRIPT_DROP if script == 'sub' else 0)
                    # dy は累積するため差分で戻す。baseline-shift は描画側の対応がまちまち。
                    step = round(wanted - shift, 2)
                    shift = wanted
                    attributes = f' font-size="{round(size, 2)}"' if script else ''
                    attributes += f' dy="{step}"' if step else ''
                    body = symbol_text(run) if symbol else run
                    element += f'<tspan font-family="{escape(font_family(family), quote=True)}" font-style="{"italic" if italic else "normal"}"{attributes}>{escape(body)}</tspan>'
                element += '</text>'
            if record:
                element = f'<g><title>{escape(record)}</title>{element}</g>'
            out.append(element)
        return '\n'.join(out + ['</g>', '</svg>'])


def marker(scene, name, x, y, *, color=BLACK, record=None, role='data', radius=3.75):
    r = radius
    kwargs = dict(color=color, record=record, role=role)
    if name == '丸':
        scene.add('circle', (x, y, r), fill=color, **kwargs)
    elif name == '四角':
        scene.add('rect', (x-r, y-r, x+r, y+r), fill='white', **kwargs)
    elif name in {'三角', 'ひし形'}:
        coords = (x, y-r, x+r, y+r, x-r, y+r) if name == '三角' else (x, y-r, x+r, y, x, y+r, x-r, y)
        scene.add('polygon', coords, fill='white', **kwargs)
    else:
        lines = ((x-r, y, x+r, y), (x, y-r, x, y+r)) if name == '十字' else ((x-r, y-r, x+r, y+r), (x-r, y+r, x+r, y-r))
        for coords in lines:
            scene.add('line', coords, **kwargs)


def chart_scene(rows, *, kind='散布図', axis='試料ID', group='合金種', errors=False, width_mm=None, height_mm=None, style=None):
    checked_rows(rows)
    if len(rows) > 200:
        raise StoreError('グラフは200測定までです。測定日の範囲や合金種・作製者・測定者で絞ってください。')
    if kind not in KINDS or axis not in AXES or group not in GROUPS:
        raise StoreError('グラフの設定が不正です。')
    style = (style or GraphStyle()).validate()
    if errors and style.error_mode == 'なし':
        style = replace(style, error_mode='標本SD')
    style = replace(style, width_mm=style.width_mm if width_mm is None else width_mm,
                    height_mm=style.height_mm if height_mm is None else height_mm).validate()
    scene = Scene(style.width_mm, style.height_mm, style)
    scene.metadata = {'format': 'labnote-comparison-figure', 'version': 1,
        'method_metric_unit': rows[0].signature, 'x_axis': axis, 'group': group,
        'kind': kind, 'style': asdict(style),
        'measurement_revisions': [[row.measurement_id, row.measurement_revision] for row in rows],
        'sample_revisions': [[row.sample_id, row.sample_revision] for row in rows]}
    width, height = scene.width, scene.height
    # A label can be shared by different creators. Keep samples separate.
    def key(row):
        return row.sample_id if axis == '試料ID' else getattr(row, AXES[axis])
    ordered = sorted(rows, key=lambda r: (getattr(r, AXES[axis]), r.creator, r.measured_on, r.measurement_id))
    categories = list(dict.fromkeys(key(row) for row in ordered))
    if axis == '試料ID':
        labels = {row.sample_id: row.label + ' / ' + row.creator for row in ordered}
    else:
        labels = {key(row): key(row) or '未設定' for row in ordered}
    groups = sorted({series_of(row, group) for row in rows})
    if len(groups) > 12:
        raise StoreError('系列が12種類を超えています。条件を絞るか系列の分類を変えてください。')
    colors = {name: style.series.get(name, {}).get('color', BLACK) for name in groups}
    markers = {name: style.series.get(name, {}).get('marker', MARKERS[i % len(MARKERS)]) for i, name in enumerate(groups)}
    legends = {name: style.series.get(name, {}).get('label', name) for name in groups}
    lows, highs = [], []
    for row in rows:
        if kind == '箱ひげ図':
            low, high = min(row.values), max(row.values)
        else:
            error = error_extent(row, style) or (0, 0)
            low, high = row.mean - error[0], row.mean + error[1]
        lows.append(low)
        highs.append(high)
    low, high = min(lows), max(highs)
    if kind == '棒グラフ':
        low, high = min(0, low), max(0, high)
    span = high - low or max(abs(high) * .1, 1)
    raw_step = span / 5
    magnitude = 10 ** math.floor(math.log10(raw_step))
    tick_step = next(value * magnitude for value in (1, 2, 2.5, 5, 10) if value * magnitude >= raw_step)
    low, high = math.floor(low / tick_step) * tick_step, math.ceil(high / tick_step) * tick_step
    if low == high:
        low, high = low - tick_step, high + tick_step
    y_tick_values = [low + tick_step * i for i in range(round((high-low) / tick_step) + 1)]
    y_tick_labels = [f'{0 if abs(value) < tick_step * 1e-8 else value:.5g}' for value in y_tick_values]
    tick_labels = {key: value[:20] + ('…' if len(value) > 20 else '') for key, value in labels.items()}
    layout = figure_layout(scene, y_tick_labels, list(tick_labels.values()), list(legends.values()),
                           x_inset=.06 if axis in CONTINUOUS_AXES else .5/len(categories))
    left, right, top, bottom = layout.left, layout.right, layout.top, layout.bottom
    def y(value):
        return bottom - (value - low) / (high - low) * (bottom - top)
    if axis in CONTINUOUS_AXES:
        # categories already run in date order, so the first one is the earliest.
        offsets = {}
        for category in categories:
            try:
                offsets[category] = (date.fromisoformat(category)
                                     - date.fromisoformat(categories[0])).days
            except ValueError as exc:
                raise StoreError('測定日を日付として読めません: ' + category) from exc
        first, last = min(offsets.values()), max(offsets.values())
        if first == last:
            first, last = first - 1, last + 1
        pad = (right - left) * .06
        inner_left, inner_right = left + pad, right - pad
        def place(category):
            return inner_left + (offsets[category] - first) / (last - first) * (inner_right - inner_left)
        # Dodging and bar widths come from the smallest real gap between two days,
        # so a measurement never drifts past its neighbour and two days one apart
        # do not overlap. Days that really are close together draw close together.
        spread = sorted(place(category) for category in categories)
        gaps = [after - before for before, after in zip(spread, spread[1:])]
        step = min(gaps) if gaps else inner_right - inner_left
        step = min(step, 60)
    else:
        step = (right - left) / len(categories)
        def place(category):
            return left + (categories.index(category) + .5) * step

    def center(row):
        return place(key(row))
    for i in range(round((high-low) / tick_step) + 1):
        value = low + tick_step * i
        yy = y(value)
        scene.add('line', (left, yy, left + style.tick_length, yy), role='y_tick')
        scene.add('text', (left - style.tick_size * .4, yy), text=f'{0 if abs(value) < tick_step * 1e-8 else value:.5g}', anchor='end', role='tick_label')
    scene.add('rect', (left, top, right, bottom), role='frame')
    if axis in CONTINUOUS_AXES:
        # Uneven positions, so keep a minimum gap instead of a fixed stride.
        ticks, previous = [], None
        for category in categories:
            xx = place(category)
            if previous is None or xx - previous >= max(text_width_pt(label, style.tick_size) for label in tick_labels.values()) + style.tick_size:
                ticks.append((category, xx))
                previous = xx
    else:
        max_ticks = max(1, int((right-left) / (max(text_width_pt(label, style.tick_size) for label in tick_labels.values()) + style.tick_size)))
        stride = max(1, math.ceil(len(categories) / max_ticks))
        ticks = [(category, place(category)) for i, category in enumerate(categories) if i % stride == 0]
    for category, xx in ticks:
        label = labels[category]
        scene.add('line', (xx, bottom, xx, bottom-style.tick_length), role='x_tick')
        scene.add('text', (xx, layout.x_tick_y), text=tick_labels[category], role='tick_label')
    scene.add('text', ((left + right) / 2, layout.x_label_y), text=style.x_label if style.x_label is not None else axis,
              size=style.label_size, bold=style.labels_bold, italics=style.x_italics,
              supers=style.x_supers, subs=style.x_subs, symbols=style.x_symbols, role='axis_label')
    scene.add('text', (layout.y_label_x, (top+bottom)/2), text=style.y_label if style.y_label is not None else f'{rows[0].metric} / {rows[0].unit}',
              size=style.label_size, bold=style.labels_bold, italics=style.y_italics,
              supers=style.y_supers, subs=style.y_subs, symbols=style.y_symbols, angle=90, role='axis_label')
    by_category = {category: [row for row in ordered if key(row) == category] for category in categories}
    previous = {}
    for row in ordered:
        series = series_of(row, group)
        color = colors[series]
        members = by_category[key(row)]
        # Separate measurement sets stay distinct for every chart kind, never average
        # averages. A category with one member keeps the centre, so charts that already
        # had one measurement per category are unchanged.
        offset = ((members.index(row) + .5) / len(members) - .5) * step * .8
        x, yy = center(row) + offset, y(row.mean)
        record = row.measurement_id
        if kind == '折れ線':
            if series in previous:
                px, py = previous[series]
                scene.add('line', (px, py, x, yy), color=color, role='data')
            previous[series] = x, yy
        if kind == '棒グラフ':
            half = min(18, step * .32 / len(members))
            scene.add('rect', (x - half, min(y(0), yy), x + half, max(y(0), yy)), color=color, fill='white', record=record, role='data')
        elif kind == '箱ひげ図':
            minimum, q1, median, q3, maximum = quartiles(row.values)
            half = min(18, step * .32 / len(members))
            scene.add('line', (x, y(minimum), x, y(maximum)), color=color, record=record, role='data')
            for bound in (minimum, maximum):
                scene.add('line', (x - half / 2, y(bound), x + half / 2, y(bound)), color=color, record=record, role='data')
            scene.add('rect', (x - half, y(q3), x + half, y(q1)), color=color, fill='white', record=record, role='data')
            scene.add('line', (x - half, y(median), x + half, y(median)), color=color, record=record, role='data')
        if kind != '箱ひげ図':
            error = error_extent(row, style)
            if error is not None:
                scene.add('line', (x, y(row.mean-error[0]), x, y(row.mean+error[1])), color=color, record=record, role='error_bar')
                for deviation in (-error[0], error[1]):
                    if deviation:
                        scene.add('line', (x - style.error_cap, y(row.mean+deviation), x + style.error_cap, y(row.mean+deviation)), color=color, record=record, role='error_cap')
            if kind != '棒グラフ':
                marker(scene, markers[series], x, yy, color=color, record=record)
    if style.legend_position != '非表示':
        for series, xx, yy in legend_entries(scene, groups):
            marker(scene, markers[series], xx+style.legend_size*.35, yy,
                   color=colors[series], role='legend', radius=style.legend_size*.23)
            scene.add('text', (xx+style.legend_size*1.3, yy), text=legends[series],
                      size=style.legend_size, anchor='start', role='legend')
    return scene


def export_comparison(rows, destination, *, raw=False):
    checked_rows(rows)
    pending = export_pending_path(destination)
    def cell(value):
        if isinstance(value, (int, float)):
            return value
        value = str(value)
        return "'" + value if value.lstrip().startswith(('=', '+', '-', '@')) or value.startswith(('\t', '\r', '\n')) else value
    with pending.open('x', encoding='utf-8-sig', newline='') as stream:
        writer = csv.writer(stream)
        writer.writerow(['試料内部ID', '試料改訂ID', '測定内部ID', '測定改訂ID', '試料ID', '作製者', '材料', '合金種',
                         '測定者', '方法', '項目', '単位', '測定日', '保存状態', '点数', '平均', '標本標準偏差'] + (['反復番号', '数値'] if raw else []))
        for row in rows:
            head = [row.sample_id, row.sample_revision, row.measurement_id, row.measurement_revision, row.label,
                    row.creator, row.material, row.alloy, row.measurer, row.method, row.metric, row.unit,
                    row.measured_on, row.state, len(row.values), row.mean, row.sd if row.sd is not None else '']
            for tail in ([[]] if not raw else [[i, value] for i, value in enumerate(row.values, 1)]):
                writer.writerow([cell(value) for value in head + tail])
        stream.flush()
        os.fsync(stream.fileno())
    publish_export(pending, destination)


def export_svg(scene, destination):
    pending = export_pending_path(destination)
    with pending.open('x', encoding='utf-8') as stream:
        stream.write(scene.svg())
        stream.flush()
        os.fsync(stream.fileno())
    publish_export(pending, destination)
