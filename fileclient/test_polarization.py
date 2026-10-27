import csv
import math
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from .graph_style import GraphStyle
from .polarization import make_curve, coordinates, curve_scene, export_curve_csv, LAYOUTS
from .store import StoreError
from .tables import Table


class PolarizationTests(unittest.TestCase):
    def table(self, rows=None):
        return Table(('電位', '電流'), tuple(rows or [('-500', '-0.1'), ('-400', '0'), ('-300', '0.2'), ('-350', '0.3')]), 'a'*64)

    def curve(self, **kwargs):
        return make_curve(self.table(), '200d-24h-10k_01', '電位', '電流', 'mV', 'mA', area=2, reference='Ag/AgCl', **kwargs)

    def test_explicit_conversion_retains_sign_and_acquisition_order(self):
        curve = self.curve()
        self.assertEqual([point[3] for point in curve.points], [-.5, -.4, -.3, -.35000000000000003])
        self.assertAlmostEqual(curve.points[0][4], -5e-5)
        self.assertEqual(curve.points[0][2], -.1)
        linear = coordinates(curve, logarithmic=False)
        self.assertAlmostEqual(linear[0][0], -50)
        self.assertAlmostEqual(linear[-1][0], 150)
        logarithmic = coordinates(curve)
        self.assertIsNone(logarithmic[1])
        self.assertAlmostEqual(logarithmic[0][0], math.log10(50))
        self.assertEqual(coordinates(curve, layout=LAYOUTS[1], logarithmic=False)[0][0], -.5)

    def test_density_does_not_divide_by_area_twice(self):
        curve = make_curve(self.table(), 'density', '電位', '電流', 'V', 'μA/cm²', area=999, reference='SCE')
        self.assertIsNone(curve.area)
        self.assertAlmostEqual(curve.points[0][4], -1e-7)

    def test_missing_area_nonfinite_values_same_column_and_reference_rejected(self):
        for changes in ({'area': ''}, {'area': 0}, {'area': 'nan'}, {'reference': ''}):
            arguments = dict(area=2, reference='SCE') | changes
            with self.subTest(changes=changes), self.assertRaises(StoreError):
                make_curve(self.table(), 'curve', '電位', '電流', 'V', 'A', **arguments)
        for value in ('NaN', 'inf', 'not-numeric'):
            with self.assertRaises(StoreError):
                make_curve(self.table([('1', '1'), ('2', value)]), 'curve', '電位', '電流', 'V', 'A/cm²', reference='SCE')
        with self.assertRaises(StoreError):
            make_curve(self.table(), 'curve', '電位', '電位', 'V', 'A/cm²', reference='SCE')

    def test_different_or_unknown_electrodes_cannot_be_overlaid(self):
        curve = self.curve()
        with self.assertRaises(StoreError):
            curve_scene([curve, replace(curve, reference='SCE')])
        with self.assertRaises(StoreError):
            curve_scene([replace(curve, reference='不明'), replace(curve, reference='不明')])

    def test_svg_uses_full_ordered_data_black_axes_and_custom_series(self):
        curve = self.curve()
        style = GraphStyle(series={curve.name: {'color': '#0077CC', 'label': 'test curve'}})
        scene = curve_scene([curve], style=style)
        svg = scene.svg()
        self.assertIn('<polyline', svg)
        self.assertIn('#0077CC', svg)
        self.assertIn('test curve', svg)
        self.assertTrue(scene.metadata['acquisition_order_preserved'])
        self.assertEqual(scene.metadata['curves'][0]['points'], 4)
        self.assertTrue(all(item[2] == '#000000' for item, fmt in zip(scene.items, scene.formats) if fmt['role'] in {'frame', 'x_tick', 'y_tick', 'axis_label'}))

    def test_sign_switches_are_not_joined_and_singletons_remain_visible(self):
        curve = make_curve(self.table([('1', '-1'), ('2', '1')]), 'sign', '電位', '電流', 'V', 'A/cm²', reference='SCE')
        series = coordinates(curve)
        self.assertIsNone(series[1])
        scene = curve_scene([curve])
        self.assertEqual(sum(item[0] == 'circle' for item in scene.items), 2)

    def test_raw_csv_preserves_signed_values_and_refuses_overwrite(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'curve.csv'
            export_curve_csv([self.curve()], path)
            with path.open(encoding='utf-8-sig', newline='') as stream:
                rows = list(csv.reader(stream))
            self.assertEqual(float(rows[1][-1]), -5e-5)
            self.assertEqual([int(row[3]) for row in rows[1:]], [2, 3, 4, 5])
            with self.assertRaises(FileExistsError):
                export_curve_csv([self.curve()], path)
