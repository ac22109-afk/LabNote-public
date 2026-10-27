import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from .catalog import Catalog, DEFAULT_CATALOG, canonical_notation, choice_key
from .runtime import load_catalog, load_deployment, save_catalog
from .store import StoreError, identifier


class CatalogTests(unittest.TestCase):
    def test_every_default_metric_has_units_and_no_unit_is_orphaned(self):
        metrics = {metric for values in DEFAULT_CATALOG['methods'].values() for metric in values}
        self.assertEqual(sorted(metrics - set(DEFAULT_CATALOG['units'])), [])
        self.assertEqual(sorted(set(DEFAULT_CATALOG['units']) - metrics), [])

    def test_default_methods_cover_what_the_lab_actually_measures(self):
        # 報告会pptx 1214図の軸名から確認できた方法。欠けていると全員が候補追加を強いられる。
        observed = {'XPS', 'FTIR', 'DSC', 'エリプソメトリー', '疲労試験', '接着試験', '接触角測定'}
        self.assertEqual(observed - set(DEFAULT_CATALOG['methods']), set())
        Catalog(DEFAULT_CATALOG)
    def test_display_notation_uses_one_spelling_per_character(self):
        self.assertEqual(canonical_notation('膜厚 / µm'), '膜厚 / μm')
        self.assertEqual(canonical_notation('25 ℃'), '25 °C')
        self.assertEqual(canonical_notation('(90˚）'), '(90°)')
        # 中黒は単位の積にだけ意味がある。日本語の名前は壊さない。
        self.assertEqual(canonical_notation('mW・g-1', unit=True), 'mW·g-1')
        self.assertEqual(canonical_notation('引張・圧縮試験'), '引張・圧縮試験')
        self.assertEqual(canonical_notation('2θ  (degree)'), '2θ (degree)')

    def test_collation_key_is_separate_from_the_display_spelling(self):
        # NFKCは照合専用。上付き文字を潰すため表示には使わない。
        self.assertEqual(choice_key('µm', unit=True), choice_key('μm', unit=True))
        # NFKC は U+207B を U+2212 へ潰す。正本の表示は ASCII のハイフンと上付き範囲。
        self.assertEqual(choice_key('cm⁻¹', unit=True), 'cm−1')
        self.assertEqual(canonical_notation('cm−1'), 'cm-1')
        self.assertEqual(canonical_notation('cm⁻¹'), 'cm⁻¹')

    def setUp(self):
        self.catalog = Catalog()

    def test_material_restricts_alloys_and_normalizes_to_canonical_label(self):
        data = self.catalog.validate('sample', {'material': 'アルミニウム合金', 'alloy': 'ａ５０５２', 'polishing': '未研磨'})
        self.assertEqual(data['alloy'], 'A5052')
        with self.assertRaises(StoreError):
            self.catalog.validate('sample', {'material': 'マグネシウム合金', 'alloy': 'A5052'})

    def test_unlisted_and_placeholder_values_do_not_become_categories(self):
        for value in ['', 'その他', 'A9999', '選択してください']:
            with self.subTest(value=value), self.assertRaises(StoreError):
                self.catalog.validate('sample', {'material': 'アルミニウム合金', 'alloy': value})

    def test_method_metric_and_unit_dependencies_are_checked(self):
        self.catalog.validate('measurement', {'method': 'SEM', 'metric': '結晶粒径', 'unit': 'μm', 'values': ['1']})
        for data in [dict(method='SEM', metric='腐食電位', unit='mV', values=['1']),
                     dict(method='SEM', metric='結晶粒径', unit='MPa', values=['1']),
                     dict(method='SEM', metric='', unit='', values=['1'])]:
            with self.subTest(data=data), self.assertRaises(StoreError):
                self.catalog.validate('measurement', data)

    def test_images_only_need_no_metric_or_unit(self):
        data = self.catalog.validate('measurement', {'method': '画像観察', 'values': []})
        self.assertEqual((data['metric'], data['unit']), ('', ''))

    def test_scientific_unit_case_is_not_folded(self):
        accepted = {'method': '分極測定', 'metric': '腐食電位', 'unit': 'mV', 'values': ['1']}
        self.catalog.validate('measurement', accepted)
        with self.assertRaises(StoreError):
            self.catalog.validate('measurement', {**accepted, 'unit': 'MV'})

    def test_legacy_literals_are_retained_only_when_dependency_unchanged(self):
        previous = dict(material='Al', alloy='', polishing='旧研磨条件')
        self.assertEqual(self.catalog.validate('sample', {**previous, 'notes': '訂正'}, previous)['material'], 'Al')
        with self.assertRaises(StoreError):
            self.catalog.validate('sample', {**previous, 'material': 'アルミニウム合金'}, previous)

    def test_addition_is_explicit_and_duplicate_variants_rejected(self):
        updated = self.catalog.add('alloys', 'A9999', 'アルミニウム合金')
        self.assertNotIn('A9999', self.catalog.options('sample', 'alloy', {'material': 'アルミニウム合金'}))
        self.assertIn('A9999', updated.options('sample', 'alloy', {'material': 'アルミニウム合金'}))
        with self.assertRaises(StoreError):
            updated.add('alloys', 'ａ９９９９', 'アルミニウム合金')

    def test_admin_can_add_method_then_metric_then_unit(self):
        updated = self.catalog.add('method', '架空測定').add('methods', '架空項目', '架空測定').add('units', 'arb', '架空項目')
        result = updated.validate('measurement', {'method': '架空測定', 'metric': '架空項目', 'unit': 'arb', 'values': ['1']})
        self.assertEqual(result['unit'], 'arb')

    def test_malformed_catalog_is_rejected(self):
        data = copy.deepcopy(self.catalog.data)
        data['alloys']['アルミニウム合金'].append('ａ１０５０')
        with self.assertRaises(StoreError):
            Catalog(data)

    def test_local_catalog_persists_and_deployment_is_default_only(self):
        with tempfile.TemporaryDirectory(prefix='labnote-catalog-') as temporary:
            root = Path(temporary)
            with patch('fileclient.runtime.profile_directory', return_value=root):
                updated = self.catalog.add('alloys', 'A9999', 'アルミニウム合金')
                self.assertEqual(load_catalog({'catalog': updated.data}).data, updated.data)
                save_catalog(updated)
                self.assertEqual(load_catalog().data, updated.data)

    def test_deployment_supplies_preset_target_and_catalog_without_credentials(self):
        with tempfile.TemporaryDirectory(prefix='labnote-deployment-') as temporary:
            root = Path(temporary)
            data = {'version': 1, 'store_id': identifier(), 'windows_path': r'\\test-nas\share\LabNote-Data',
                    'mac_path': '/Volumes/share/LabNote-Data', 'catalog': self.catalog.data}
            path = root / 'labnote-deployment.json'
            path.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
            with patch('fileclient.runtime.deployment_directory', return_value=root):
                self.assertEqual(load_deployment(persist=False), data)
                data['windows_path'] = 'relative-folder'
                path.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
                with self.assertRaises(StoreError):
                    load_deployment(persist=False)
