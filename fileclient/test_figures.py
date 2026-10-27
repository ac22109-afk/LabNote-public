import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch
from xml.etree import ElementTree as ET

from .comparison import ComparisonRow, chart_scene
from .figure_presets import FIGURE_PRESETS, FigurePresets, FIGURE_FIELDS
from .graph_style import GraphStyle, PT_PER_MM, load_style, save_style, text_width_pt, font_runs
from .polarization import curve_scene, make_curve
from .store import StoreError
from .tables import Table


def example_row():
    return ComparisonRow('sample', 'sample-rev', 'measurement', 'measurement-rev',
        '200d-24h-10k_01', 'アルミニウム合金', 'A5052', '研磨', False,
        'zz00001', 'zz00001', 'SEM', '結晶粒径', 'μm', '2026-10-22', (1, 3, 5), '送信済み')


class PhysicalFigureTests(unittest.TestCase):
    def test_only_left_and_bottom_ticks_are_drawn_in_both_tools(self):
        table = Table(('E', 'I'), (('-.5', '-.1'), ('-.4', '.2'), ('-.3', '.3')), 'a'*64)
        curve = make_curve(table, 'curve', 'E', 'I', 'V', 'mA', area=2, reference='Ag/AgCl')
        for scene in (chart_scene([example_row()]), curve_scene([curve])):
            left, right, top, bottom = scene.layout.left, scene.layout.right, scene.layout.top, scene.layout.bottom
            ticks = [(item, fmt) for item, fmt in zip(scene.items, scene.formats) if fmt['role'] in ('x_tick', 'y_tick')]
            self.assertTrue(ticks)
            for item, fmt in ticks:
                x1, y1, x2, y2 = item[1]
                if fmt['role'] == 'x_tick':
                    self.assertEqual(y1, bottom)
                    self.assertEqual(x1, x2)
                    self.assertLess(y2, y1)
                else:
                    self.assertEqual(x1, left)
                    self.assertEqual(y1, y2)
                    self.assertGreater(x2, x1)

    def test_greek_uses_symbol_automatically_and_source_text_is_preserved(self):
        style = GraphStyle(y_label='応力 σ / μm', series={'A5052': {'label':'α phase'}})
        scene = chart_scene([example_row()], style=style)
        root = ET.fromstring(scene.svg())
        spans = root.findall('.//{http://www.w3.org/2000/svg}tspan')
        symbolic = ''.join(node.text or '' for node in spans if node.get('font-family') == 'Symbol')
        for value in ('\uf073', '\uf06d', '\uf061'):
            self.assertIn(value, symbolic)
        self.assertEqual(scene.style.y_label, '応力 σ / μm')
        self.assertTrue(font_runs('µ')[0][-1])

    def test_svg_dimensions_are_mm_and_coordinate_system_is_points(self):
        for name, values in FIGURE_PRESETS.items():
            with self.subTest(preset=name):
                style = GraphStyle(**values)
                scene = chart_scene([example_row()], style=style)
                root = ET.fromstring(scene.svg())
                self.assertEqual(root.get('width'), f'{style.width_mm:g}mm')
                self.assertEqual(root.get('height'), f'{style.height_mm:g}mm')
                box = list(map(float, root.get('viewBox').split()))
                self.assertAlmostEqual(box[2], style.width_mm*PT_PER_MM)
                self.assertAlmostEqual(box[3], style.height_mm*PT_PER_MM)
                self.assertEqual([fmt['size'] for fmt in scene.formats if fmt['role']=='axis_label'],
                                 [style.label_size, style.label_size])

    def test_same_style_is_reproducible_and_physical_resize_keeps_point_size(self):
        style = GraphStyle(label_size=10.5)
        scene = chart_scene([example_row()], style=style)
        self.assertEqual(scene.svg(), chart_scene([example_row()], style=style).svg())
        larger = chart_scene([example_row()], width_mm=190, height_mm=120, style=style)
        self.assertNotEqual(scene.width, larger.width)
        for figure in (scene, larger):
            self.assertIn('font-size="10.5"', figure.svg())

    def test_tick_and_axis_margins_fit_small_paper_and_grow_for_wide_numbers(self):
        style = GraphStyle(**FIGURE_PRESETS['論文（1段・暫定）'])
        scene = chart_scene([example_row()], style=style)
        for item, fmt in zip(scene.items, scene.formats):
            if fmt['role'] == 'tick_label' and item[5] == 'end':
                left = item[1][0] - text_width_pt(item[4], fmt['size'])
                self.assertGreater(left, scene.layout.y_label_x + style.label_size*.5)
        error_style = replace(style, error_mode='最小–最大')
        narrow = chart_scene([replace(example_row(), label='S')], style=error_style)
        wide = chart_scene([replace(example_row(), label='S', values=(.00012345, .00023456))], style=error_style)
        self.assertGreater(wide.layout.left, narrow.layout.left)

    def test_legends_never_silently_clip_and_positions_share_bounded_geometry(self):
        for position in ('上（枠外）', '下（枠外）', '自由配置', '右下（枠内）'):
            style = GraphStyle(legend_position=position, legend_x=100, legend_y=100)
            scene = chart_scene([example_row()], style=style)
            left, top, right, bottom = scene.legend_bounds
            self.assertGreaterEqual(left, 0)
            self.assertGreaterEqual(top, 0)
            self.assertLessEqual(right, scene.width)
            self.assertLessEqual(bottom, scene.height)
        with self.assertRaisesRegex(StoreError, '凡例'):
            chart_scene([example_row()], style=GraphStyle(series={'A5052':{'label':'長い凡例'*12}}))

    def test_single_category_does_not_waste_half_label_at_both_page_edges(self):
        style = GraphStyle(**FIGURE_PRESETS['ポスター（A0）'],
            series={'A5052': {'label':'Package legend'}}, legend_position='自由配置')
        scene = chart_scene([replace(example_row(), label='PACKAGE-DEMO')], style=style)
        self.assertGreater(scene.layout.right-scene.layout.left, scene.layout.legend_width)
        for item, fmt in zip(scene.items, scene.formats):
            if item[0] == 'text' and fmt['role'] == 'tick_label' and item[5] == 'middle':
                half = text_width_pt(item[4], fmt['size']) / 2
                self.assertGreaterEqual(item[1][0]-half, 0)
                self.assertLessEqual(item[1][0]+half, scene.width)

    def test_polarization_uses_same_physical_layout_and_keeps_signed_source(self):
        table = Table(('E', 'I'), (('-.5', '-.1'), ('-.4', '.2'), ('-.3', '.3')), 'a'*64)
        curve = make_curve(table, 'curve', 'E', 'I', 'V', 'mA', area=2, reference='Ag/AgCl')
        for name, values in FIGURE_PRESETS.items():
            with self.subTest(name=name):
                style = GraphStyle(**values)
                scene = curve_scene([curve], style=style)
                self.assertEqual(ET.fromstring(scene.svg()).get('width'), f'{style.width_mm:g}mm')
                self.assertEqual(scene.style.label_size, values['label_size'])
                self.assertTrue(scene.metadata['signed_raw_data_preserved'])
                self.assertLess(curve.points[0][4], 0)

    def test_invalid_dimensions_and_nonfinite_or_boolean_sizes_are_rejected(self):
        for values in (dict(width_mm=0), dict(height_mm=float('inf')), dict(label_size=True),
                       dict(label_size=float('nan')), dict(width_mm=True)):
            with self.subTest(values=values), self.assertRaises(StoreError):
                chart_scene([example_row()], style=GraphStyle(**values))
        with self.assertRaisesRegex(StoreError, '軸名'):
            chart_scene([example_row()], style=GraphStyle(y_label='Very long axis label ' * 7))
        with self.assertRaisesRegex(StoreError, '改行'):
            chart_scene([example_row()], style=GraphStyle(series={'A5052': {'label':'two\nlines'}}))

    def test_old_style_migrates_all_pixel_lengths_without_rewriting_original(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'legacy.json'
            original = json.dumps({'version':1, 'style':{'label_size':14, 'tick_size':16,
                'legend_size':16, 'line_width':1.2, 'tick_length':7, 'error_cap':5}})
            path.write_text(original)
            style = load_style(path)
            self.assertEqual((style.label_size, style.tick_size), (10.5, 12))
            self.assertAlmostEqual(style.line_width, .9)
            self.assertEqual(style.error_cap, 3.75)
            self.assertEqual(path.read_text(), original)
            second = Path(temporary) / 'v2.json'
            save_style(style, second)
            self.assertEqual(json.loads(second.read_text(encoding='utf-8'))['version'], 2)
            self.assertEqual(load_style(second), style)


class FigurePresetTests(unittest.TestCase):
    def test_switching_use_cases_switches_both_fonts_without_erasing_graph_annotations(self):
        with tempfile.TemporaryDirectory() as temporary:
            presets = FigurePresets(Path(temporary) / 'presets.json')
            style = GraphStyle(y_label='応力 σ', y_symbols=((3, 4),), series={'A5052': {'color':'#0066ff'}})
            for name in ('論文（1段・暫定）', '報告会', '論文（2段・暫定）', 'ポスター（A0）'):
                style = presets.apply(name, style)
                expected = ('Times New Roman', 'MS 明朝') if name.startswith('論文') else ('Arial', 'MS Pゴシック')
                self.assertEqual((style.english_font, style.japanese_font), expected)
                self.assertEqual(style.y_label, '応力 σ')
                self.assertEqual(style.y_symbols, ((3, 4),))
                self.assertEqual(style.series['A5052']['color'], '#0066ff')

    def test_named_preset_preserves_geometry_but_never_copies_axis_or_series_data(self):
        with tempfile.TemporaryDirectory() as temporary:
            presets = FigurePresets(Path(temporary) / 'presets.json')
            style = GraphStyle(width_mm=160, height_mm=76, label_size=10.5,
                x_label='private-axis', series={'private-series':{'label':'private-name'}})
            saved = presets.save('横長', style, make_default=True)
            self.assertEqual(set(saved['data']), set(FIGURE_FIELDS))
            self.assertNotIn('private', presets.path.read_text(encoding='utf-8'))
            loaded = presets.default(GraphStyle(y_label='keep-this'))
            self.assertEqual((loaded.width_mm, loaded.label_size, loaded.y_label), (160, 10.5, 'keep-this'))
            self.assertIsNone(loaded.x_label)
            with self.assertRaises(StoreError):
                presets.save('横長', style)
            presets.save('横長', replace(style, height_mm=100), update=True)
            self.assertEqual(presets.default(GraphStyle()).height_mm, 100)
            presets.set_default(None)
            self.assertEqual(presets.default(GraphStyle()), GraphStyle())
            with self.assertRaises(StoreError):
                presets.save('報告会', style, update=True)

    def test_interrupted_write_preserves_previous_presets_and_builtins_remain_available(self):
        with tempfile.TemporaryDirectory() as temporary:
            presets = FigurePresets(Path(temporary) / 'presets.json')
            presets.save('自分用', GraphStyle())
            previous = presets.path.read_bytes()
            with patch('fileclient.figure_presets.os.replace', side_effect=OSError('interrupted')):
                with self.assertRaises(OSError):
                    presets.save('新規', GraphStyle())
            self.assertEqual(presets.path.read_bytes(), previous)
            presets.path.write_text('{broken')
            with self.assertRaises(StoreError):
                presets.choices()
            self.assertEqual(presets.apply('ポスター（A0）', GraphStyle()).label_size, 24)
