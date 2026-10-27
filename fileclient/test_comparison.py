import csv
import json
import math
import tempfile
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET
from dataclasses import replace, fields as dc_fields
from pathlib import Path

from .comparison import (GROUPS, KINDS, chart_scene, comparison_rows, export_comparison,
                         export_svg, quartiles, select_rows, unplotted_measurements)
from .store import FileStore, StoreError
from .graph_style import (GraphStyle, error_extent, font_family, font_runs, load_style,
                          missing_fonts, save_style, symbol_text, LEGEND_POSITIONS,
                          PER_GRAPH_FIELDS)


class ComparisonTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name)
        self.store = FileStore.create(self.home / 'records')
        self.sample = self.store.save('sample', {'label': 'S1', 'creator': 'zz00001',
            'material': 'アルミニウム合金', 'alloy': 'A5052'}, 'zz00001')
        self.measurement = self.add_measurement(['1', '3', '5'])

    def add_measurement(self, values, **changes):
        data = dict(sample_id=self.sample['id'], measurer='zz00001', method='SEM',
                    metric='結晶粒径', unit='μm', measured_on='2026-10-21', values=values)
        data.update(changes)
        return self.store.save('measurement', data, 'zz00001')

    def row(self):
        return comparison_rows(self.store.load())[0][0]

    def test_one_row_per_latest_measurement_with_sample_and_revision_provenance(self):
        updated = self.store.save('measurement', {**self.measurement['data'], 'values': ['2', '6']},
            'zz00001', record_id=self.measurement['id'], base_revision=self.measurement['revision_id'])
        row = self.row()
        self.assertEqual((row.mean, row.values), (4, (2, 6)))
        self.assertEqual(row.measurement_revision, updated['revision_id'])
        self.assertEqual(row.sample_revision, self.sample['revision_id'])
        self.assertEqual(len(comparison_rows(self.store.load())[0]), 1)

    def test_sample_sd_is_not_population_sd_and_single_value_has_no_sd(self):
        self.assertEqual(self.row().sd, 2)
        self.assertIsNone(replace(self.row(), values=(3,)).sd)

    def test_excludes_conflicts_blocked_records_and_missing_sample(self):
        for mode in ('conflict', 'blocked', 'missing'):
            snapshot = self.store.load()
            if mode == 'conflict':
                snapshot.heads[self.measurement['id']].append(self.measurement)
            elif mode == 'blocked':
                snapshot.blocked.add(self.measurement['id'])
            else:
                snapshot.blocked.add(self.sample['id'])
            rows, excluded = comparison_rows(snapshot)
            self.assertFalse(rows)
            self.assertEqual(sum(excluded.values()), 1)

    def test_image_only_and_legacy_missing_unit_excluded_with_counts(self):
        self.add_measurement([], method='画像観察', metric='', unit='')
        self.add_measurement(['1'], unit='')
        rows, excluded = comparison_rows(self.store.load())
        self.assertEqual(len(rows), 1)
        self.assertEqual(excluded['数値なし'], 1)
        self.assertEqual(excluded['項目・単位なし'], 1)

    def test_different_method_metric_and_unit_are_isolated_and_unit_case_matters(self):
        self.add_measurement(['1'], unit='mV')
        self.add_measurement(['2'], unit='MV')
        rows, _ = comparison_rows(self.store.load())
        self.assertEqual(len(select_rows(rows, ('SEM', '結晶粒径', 'mV'))), 1)
        with self.assertRaises(StoreError):
            chart_scene(rows)

    def test_filter_is_exact_and_multiple_repeats_are_not_extra_measurements(self):
        rows = [self.row(), replace(self.row(), alloy='A1050', creator='zz00002')]
        selected = select_rows(rows, rows[0].signature, alloy='A5052', creator='zz00001')
        self.assertEqual(len(selected), 1)
        self.assertEqual(len(selected[0].values), 3)
        self.assertFalse(select_rows(rows, rows[0].signature, alloy='5052'))

    def test_quartiles_use_raw_values_and_singleton_is_well_defined(self):
        self.assertEqual(quartiles([1, 2, 3, 8]), (1, 1.75, 2.5, 4.25, 8))
        self.assertEqual(quartiles([4]), (4, 4, 4, 4, 4))

    def test_all_chart_types_export_valid_svg_with_negative_and_constant_values(self):
        row = self.row()
        for values in ((-4, -2, -1), (0,), (4, 4)):
            for kind in KINDS:
                scene = chart_scene([replace(row, values=values)], kind=kind, errors=True)
                root = ET.fromstring(scene.svg())
                self.assertEqual(root.tag, '{http://www.w3.org/2000/svg}svg')
                self.assertTrue(all(math.isfinite(value) for item in scene.items for value in item[1]))

    def test_repeated_sample_names_across_creators_stay_separate_on_axis(self):
        row = self.row()
        other = replace(row, sample_id='different', creator='zz00002', measurement_id='other')
        scene = chart_scene([row, other])
        points = [item[1][0] for item, fmt in zip(scene.items, scene.formats) if item[0] == 'circle' and fmt['role'] == 'data']
        self.assertEqual(len(set(points)), 2)

    def test_measurements_sharing_category_are_separate_bars(self):
        row = self.row()
        scene = chart_scene([row, replace(row, measurement_id='other', values=(7,))], kind='棒グラフ')
        bars = [item for item, fmt in zip(scene.items, scene.formats) if item[0] == 'rect' and fmt['role'] == 'data']
        self.assertEqual(len(bars), 2)
        self.assertNotEqual(bars[0][1][0], bars[1][1][0])

    def dated_rows(self):
        row = self.row()
        return [replace(row, sample_id='s%d' % i, measurement_id='m%d' % i, measured_on=day)
                for i, day in enumerate(('2026-01-01', '2026-01-02', '2026-03-01'))]

    def data_points(self, scene):
        return sorted(item[1][0] for item, fmt in zip(scene.items, scene.formats)
                      if item[0] == 'circle' and fmt['role'] == 'data')

    def test_real_date_axis_spaces_measurements_by_the_actual_gap(self):
        rows = self.dated_rows()
        equal = self.data_points(chart_scene(rows, axis='測定日'))
        self.assertAlmostEqual(equal[1] - equal[0], equal[2] - equal[1])
        real = self.data_points(chart_scene(rows, axis='測定日（実日付）'))
        self.assertLess(real[1] - real[0], (real[2] - real[1]) / 10)

    def test_real_date_axis_handles_a_single_day_without_dividing_by_zero(self):
        row = self.row()
        same = [row, replace(row, measurement_id='other', values=(7,))]
        scene = chart_scene(same, axis='測定日（実日付）')
        points = self.data_points(scene)
        self.assertEqual(len(points), 2)
        self.assertTrue(all(math.isfinite(value) for value in points))
        # 同じ日の重なりの解消は #11 の範囲。ここでは0除算しないことだけを見る。
        self.assertTrue(all(math.isfinite(value) for item in scene.items for value in item[1]))

    def test_real_date_axis_keeps_adjacent_days_apart(self):
        rows = self.dated_rows()
        scene = chart_scene(rows, kind='棒グラフ', axis='測定日（実日付）')
        bars = sorted(item[1] for item, fmt in zip(scene.items, scene.formats)
                      if item[0] == 'rect' and fmt['role'] == 'data')
        self.assertEqual(len(bars), 3)
        # 1日違いの2本が重ならない。幅は隣接日の最小間隔から決まる。
        self.assertLess(bars[0][2], bars[1][0])
        # ずらし幅が最小間隔の半分未満なので、点が隣の日付を越えて並び替わらない。
        gap = min(bars[1][0] - bars[0][0], bars[2][0] - bars[1][0])
        self.assertLess(bars[0][2] - bars[0][0], gap)

    def test_real_date_axis_reports_an_unreadable_date_as_a_store_error(self):
        row = replace(self.row(), measured_on='2026/01/01')
        with self.assertRaises(StoreError):
            chart_scene([row], axis='測定日（実日付）')

    def test_measured_on_range_filters_inclusively_and_rejects_bad_bounds(self):
        rows = self.dated_rows()
        signature = rows[0].signature
        self.assertEqual(len(select_rows(rows, signature, since='2026-01-02')), 2)
        self.assertEqual(len(select_rows(rows, signature, until='2026-01-02')), 2)
        self.assertEqual(len(select_rows(rows, signature, since='2026-01-02', until='2026-01-02')), 1)
        self.assertEqual(len(select_rows(rows, signature)), 3)
        with self.assertRaises(StoreError):
            select_rows(rows, signature, since='2026-03-01', until='2026-01-01')
        with self.assertRaises(StoreError):
            select_rows(rows, signature, since='2026/01/01')

    def test_chart_limit_points_at_the_measured_on_range(self):
        with self.assertRaisesRegex(StoreError, '測定日'):
            chart_scene([self.row()] * 201)
    def test_measurements_sharing_category_do_not_overlap_in_scatter_and_line(self):
        row = self.row()
        pair = [row, replace(row, measurement_id='other', values=(7,))]
        for kind in ('散布図', '折れ線'):
            scene = chart_scene(pair, kind=kind)
            points = [item[1][0] for item, fmt in zip(scene.items, scene.formats)
                      if item[0] == 'circle' and fmt['role'] == 'data']
            self.assertEqual(len(points), 2, kind)
            self.assertNotEqual(points[0], points[1], kind)

    def test_one_measurement_per_category_keeps_the_category_centre(self):
        row = self.row()
        bars = chart_scene([row], kind='棒グラフ')
        bar = next(item for item, fmt in zip(bars.items, bars.formats)
                   if item[0] == 'rect' and fmt['role'] == 'data')
        centre = (bar[1][0] + bar[1][2]) / 2
        self.assertAlmostEqual(centre, (bars.layout.left + bars.layout.right) / 2)
        for kind in ('散布図', '折れ線'):
            scene = chart_scene([row], kind=kind)
            points = [item[1][0] for item, fmt in zip(scene.items, scene.formats)
                      if item[0] == 'circle' and fmt['role'] == 'data']
            self.assertEqual(len(points), 1, kind)
            # Tick-dependent margins can differ by kind (bars include zero).
            self.assertAlmostEqual(points[0], (scene.layout.left + scene.layout.right) / 2, msg=kind)
    def legends(self, scene):
        return [item[4] for item, fmt in zip(scene.items, scene.formats)
                if item[0] == 'text' and fmt['role'] == 'legend']

    def test_sample_attributes_can_group_series(self):
        row = replace(self.row(), polishing='バフ研磨')
        other = replace(row, sample_id='other', measurement_id='other', polishing='電解研磨')
        for group in GROUPS:
            chart_scene([row, other], group=group)
        self.assertEqual(self.legends(chart_scene([row, other], group='研磨条件')),
                         ['バフ研磨', '電解研磨'])
        self.assertEqual(self.legends(chart_scene([row, other], group='材料')),
                         ['アルミニウム合金'])

    def test_solution_treated_series_separates_false_from_unset(self):
        treated = replace(self.row(), solution_treated=True)
        untreated = replace(treated, sample_id='other', measurement_id='other',
                            solution_treated=False)
        self.assertEqual(self.legends(chart_scene([treated, untreated], group='溶体化処理')),
                         ['溶体化処理あり', '溶体化処理なし'])
    def test_unplotted_lists_each_dropped_measurement_with_its_reason(self):
        self.add_measurement([])
        self.add_measurement(['2'], metric='', unit='')
        snapshot = self.store.load()
        rows, excluded = comparison_rows(snapshot)
        found = unplotted_measurements(snapshot)
        self.assertEqual(len(rows), 1)
        self.assertEqual(sorted(record['reason'] for record in found), ['数値なし', '項目・単位なし'])
        # 件数の集計と一覧が食い違わないこと
        self.assertEqual(len(found), sum(excluded.values()))

    def test_a_record_hidden_without_a_delete_stays_visible_somewhere(self):
        snapshot = self.store.load()
        # ごみ箱以外の理由で隠れる記録（lifecycle を辿れない等）は、図から消えても
        # 件数と一覧のどちらかには必ず出る。どの画面からも消えてはいけない。
        with patch.object(type(snapshot), 'hidden',
                          lambda self, item: item['kind'] == 'measurement'):
            rows, excluded = comparison_rows(snapshot)
            found = unplotted_measurements(snapshot)
        self.assertEqual(rows, [])
        self.assertEqual(excluded['表示できない状態'], 1)
        self.assertEqual([record['reason'] for record in found], ['表示できない状態'])

    def test_unplotted_can_be_limited_to_one_registrant(self):
        self.add_measurement([])
        snapshot = self.store.load()
        self.assertEqual(len(unplotted_measurements(snapshot, 'ZZ00001')), 1)
        self.assertEqual(unplotted_measurements(snapshot, 'nb23102'), [])

    def test_unplotted_is_empty_when_every_measurement_can_be_plotted(self):
        self.assertEqual(unplotted_measurements(self.store.load()), [])

    def test_error_bars_are_missing_for_n1_and_no_inferred_treatment_axis(self):
        row = replace(self.row(), values=(1,))
        self.assertEqual(chart_scene([row], errors=True).items, chart_scene([row], errors=False).items)
        with self.assertRaises(StoreError):
            chart_scene([row], axis='処理温度')

    def test_csv_exports_all_raw_values_ids_states_and_formula_safe_text(self):
        row = replace(self.row(), label='=1+1', state='未送信')
        path = self.home / 'raw.csv'
        export_comparison([row], path, raw=True)
        with path.open(encoding='utf-8-sig', newline='') as stream:
            data = list(csv.reader(stream))
        self.assertEqual(len(data), 4)
        self.assertEqual([line[-1] for line in data[1:]], ['1.0', '3.0', '5.0'])
        self.assertEqual(data[1][4], "'=1+1")
        self.assertEqual(data[1][13], '未送信')
        self.assertEqual(data[1][3], row.measurement_revision)

    def test_exports_refuse_overwrite_and_svg_escapes_untrusted_labels(self):
        row = replace(self.row(), metric='<script>&"')
        scene = chart_scene([row])
        path = self.home / 'figure.svg'
        export_svg(scene, path)
        ET.parse(path)
        self.assertNotIn('<script>', path.read_text(encoding='utf-8'))
        before = path.read_bytes()
        with self.assertRaises(FileExistsError):
            export_svg(scene, path)
        with self.assertRaises(FileExistsError):
            export_comparison([row], path)
        self.assertEqual(path.read_bytes(), before)

    def test_empty_or_overly_large_charts_require_filtering_without_truncation(self):
        with self.assertRaises(StoreError):
            chart_scene([])
        with self.assertRaises(StoreError):
            chart_scene([self.row()] * 201)

    def test_default_frame_text_ticks_and_series_are_pure_black_without_gridlines(self):
        scene = chart_scene([self.row()])
        self.assertEqual({item[2] for item in scene.items}, {'#000000'})
        frame = next(item[1] for item, fmt in zip(scene.items, scene.formats) if fmt['role'] == 'frame')
        left, top, right, bottom = frame
        for item, fmt in zip(scene.items, scene.formats):
            if fmt['role'] == 'x_tick':
                self.assertTrue(top <= item[1][3] <= bottom)
                self.assertTrue(item[1][1] in (top, bottom))
            if fmt['role'] == 'y_tick':
                self.assertTrue(left < item[1][2] < right)
                self.assertTrue(item[1][0] in (left, right))
        self.assertFalse(any(fmt['role'] == 'grid' for fmt in scene.formats))

    def test_partial_italics_and_mixed_script_fonts_are_explicit_in_svg(self):
        style = GraphStyle(english_font='Times New Roman', japanese_font='MS 明朝',
                           x_label='Time, t / s', x_italics=((6, 7),), y_label='濃度 / at%')
        scene = chart_scene([self.row()], style=style)
        svg = ET.fromstring(scene.svg())
        ns = {'s': 'http://www.w3.org/2000/svg'}
        texts = svg.findall('.//s:text', ns)
        axis = next(node for node in texts if ''.join(node.itertext()) == style.x_label)
        italics = [node.text for node in axis if node.get('font-style') == 'italic']
        self.assertEqual(italics, ['t'])
        self.assertEqual({node.get('font-family') for node in axis}, {'Times New Roman'})
        japanese = next(node for node in texts if ''.join(node.itertext()) == style.y_label)
        self.assertEqual({node.get('font-family') for node in japanese}, {'Times New Roman', 'MS Mincho'})
        self.assertEqual(japanese.get('fill'), '#000000')

    def test_sem_minmax_multiplier_sidedness_and_singletons_have_defined_meaning(self):
        row = self.row()
        sem = error_extent(row, GraphStyle(error_mode='標準誤差（SD/√n）'))
        self.assertAlmostEqual(sem[0], 2 / math.sqrt(3))
        self.assertEqual(error_extent(row, GraphStyle(error_mode='標本SD', error_multiplier=2, error_direction='上側のみ')), (0, 4))
        asymmetric = replace(row, values=(1, 2, 9))
        self.assertEqual(error_extent(asymmetric, GraphStyle(error_mode='最小–最大')), (3, 5))
        self.assertIsNone(error_extent(replace(row, values=(3,)), GraphStyle(error_mode='標本SD')))
        self.assertIsNone(error_extent(row, GraphStyle()))

    def test_error_mode_applies_to_scatter_line_and_bars_but_not_boxplot(self):
        for kind in KINDS:
            scene = chart_scene([self.row()], kind=kind, style=GraphStyle(error_mode='標本SD', error_cap=9))
            caps = [item for item, fmt in zip(scene.items, scene.formats) if fmt['role'] == 'error_cap']
            self.assertEqual(len(caps), 0 if kind == '箱ひげ図' else 2)
            for cap in caps:
                self.assertEqual(cap[1][2]-cap[1][0], 18)

    def test_series_alias_color_marker_and_legend_position_do_not_mutate_data(self):
        row = self.row()
        for position in LEGEND_POSITIONS:
            style = GraphStyle(series={'A5052': {'label': 'Si reference', 'color': '#FF8000', 'marker': '三角'}}, legend_position=position, legend_x=25, legend_y=60)
            scene = chart_scene([row], style=style)
            self.assertEqual(row.alloy, 'A5052')
            for item, fmt in zip(scene.items, scene.formats):
                if item[0] == 'text':
                    self.assertEqual(item[2], '#000000')
            if position == '非表示':
                self.assertIsNone(scene.legend_bounds)
            else:
                self.assertIn('Si reference', scene.svg())
                self.assertIsNotNone(scene.legend_bounds)
            self.assertTrue(any(item[0] == 'polygon' and item[2] == '#FF8000' for item in scene.items))

    def test_saved_standard_excludes_axis_names_units_italics_and_series_aliases(self):
        path = self.home / 'style.json'
        style = GraphStyle(english_font='Times New Roman', x_label='Custom, t / s', x_italics=((8, 9),),
            error_mode='標本SD', series={'A5052': {'label': 'Private label'}})
        save_style(style, path)
        loaded = load_style(path)
        self.assertEqual(loaded.english_font, 'Times New Roman')
        self.assertEqual(loaded.error_mode, '標本SD')
        self.assertIsNone(loaded.x_label)
        self.assertFalse(loaded.x_italics)
        self.assertFalse(loaded.series)
        self.assertNotIn('Private label', path.read_text(encoding='utf-8'))

    def test_font_aliases_resolve_mincho_and_missing_fonts_are_reported(self):
        self.assertEqual(font_family('MS 明朝'), 'MS Mincho')
        self.assertFalse(missing_fonts(GraphStyle(japanese_font='MS 明朝'), ['Arial', 'ＭＳ 明朝']))
        self.assertEqual(missing_fonts(GraphStyle(japanese_font='MS 明朝'), ['Arial']), ['MS 明朝'])
        self.assertEqual(font_runs('濃度, t', ((4, 5),))[-1], ('t', 'en', True, '', False))

    def test_scripts_and_symbols_split_runs_without_touching_the_text(self):
        runs = font_runs('cm-1', supers=((2, 4),))
        self.assertEqual(runs, [('cm', 'en', False, '', False), ('-1', 'en', False, 'super', False)])
        self.assertEqual([run[3] for run in font_runs('HIT', subs=((1, 3),))], ['', 'sub'])
        self.assertEqual(font_runs('2\u03b8', symbols=((1, 2),))[-1], ('\u03b8', 'en', False, '', True))

    def test_symbol_text_uses_the_symbol_font_code_points(self):
        # 2025年の論文PDFと同じ符号位置。
        self.assertEqual(symbol_text('\u03c3'), '\uf073')
        self.assertEqual(symbol_text('\u03bc'), '\uf06d')
        self.assertEqual(symbol_text('\u03b8'), '\uf071')
        self.assertEqual(symbol_text('\u0394'), '\uf044')
        self.assertEqual(symbol_text('/ MPa'), '/ MPa')

    def test_axis_label_renders_symbol_font_and_baseline_shift_in_svg(self):
        style = GraphStyle(x_label='\u03c3 / mW g-1', x_symbols=((0, 1),), x_supers=((8, 10),))
        svg = chart_scene([self.row()], style=style).svg()
        self.assertIn('font-family="Symbol"', svg)
        self.assertIn('\uf073', svg)
        self.assertNotIn('>\u03c3<', svg)
        self.assertIn('dy="', svg)

    def test_script_ranges_are_validated_like_italics(self):
        with self.assertRaises(StoreError):
            GraphStyle(x_label='cm-1', x_supers=((2, 9),)).validate()
        with self.assertRaises(StoreError):
            GraphStyle(x_label='cm-1', x_symbols=((3, 2),)).validate()
        GraphStyle(x_label='cm-1', x_supers=((2, 4),), x_symbols=((0, 1),)).validate()

    def test_standard_style_never_keeps_ranges_without_their_axis_name(self):
        # 範囲だけが残ると、軸名の無い状態で validate() が必ず失敗する。
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'style.json'
            save_style(GraphStyle(x_label='cm-1', x_supers=((2, 4),), x_symbols=((0, 1),),
                                  y_label='σ', y_symbols=((0, 1),)), path)
            restored = load_style(path)
            self.assertEqual(restored.x_supers, ())
            self.assertEqual(restored.x_symbols, ())
            self.assertIsNone(restored.x_label)
        leftovers = {field.name for field in dc_fields(GraphStyle)
                     if field.name.startswith(('x_', 'y_')) or field.name == 'series'}
        self.assertEqual(leftovers - set(PER_GRAPH_FIELDS), set())

    def test_invalid_style_inputs_are_rejected_before_rendering_or_saving(self):
        for options in (dict(label_size=300), dict(english_font='<script>'),
                dict(line_width=float('nan')), dict(legend_x=101), dict(error_cap=-1),
                dict(x_label='t', x_italics=((0, 3),)), dict(series={'A5052': {'color': 'javascript:'}})):
            with self.subTest(options=options), self.assertRaises(StoreError):
                chart_scene([self.row()], style=GraphStyle(**options))

    def test_svg_metadata_preserves_style_and_revision_provenance(self):
        row = self.row()
        style = GraphStyle(error_mode='標準誤差（SD/√n）', error_multiplier=2)
        root = ET.fromstring(chart_scene([row], style=style).svg())
        info = json.loads(root.find('{http://www.w3.org/2000/svg}metadata').text)
        self.assertEqual(info['style']['error_mode'], style.error_mode)
        self.assertEqual(info['style']['error_multiplier'], 2)
        self.assertEqual(info['measurement_revisions'], [[row.measurement_id, row.measurement_revision]])


if __name__ == '__main__':
    unittest.main()
