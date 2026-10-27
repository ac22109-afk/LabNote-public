import copy
import json
import tempfile
import unittest
from pathlib import Path

from .font_requirements import format_font_status, inspect_fonts, load_font_requirements
from .store import StoreError


class FontPrerequisiteTests(unittest.TestCase):
    def test_manifest_includes_five_fonts_all_used_by_the_current_display(self):
        requirements = load_font_requirements()
        self.assertEqual({item['name'] for item in requirements}, {'Arial', 'Times New Roman', 'MS 明朝', 'MS Pゴシック', 'Symbol'})
        # Symbol は軸名のギリシャ文字で実際に使うため、将来の準備ではなくなった。
        self.assertEqual([item['name'] for item in requirements if item['future']], [])

    def test_windows_fullwidth_names_and_english_aliases_match_without_substitution(self):
        available = ['Arial', 'Times New Roman', 'ＭＳ 明朝', 'ＭＳ Ｐゴシック', 'Symbol']
        self.assertTrue(all(item['installed'] for item in inspect_fonts(available)))
        available = ['Arial', 'Times New Roman', 'MS Mincho', 'MS PGothic', 'Symbol']
        self.assertTrue(all(item['installed'] for item in inspect_fonts(available)))

    def test_similarly_named_fonts_do_not_satisfy_exact_requirements(self):
        available = ['Arial Unicode MS', 'Segoe UI Symbol', 'MS Gothic', 'MS PMincho']
        self.assertFalse(any(item['installed'] for item in inspect_fonts(available)))

    def test_vertical_only_font_does_not_count_as_horizontal_family(self):
        results = inspect_fonts(['@ＭＳ 明朝'])
        self.assertFalse(next(item for item in results if item['name'] == 'MS 明朝')['installed'])

    def test_missing_fonts_are_reported_without_failing_or_changing_input(self):
        available = ['Arial']
        requirements = load_font_requirements()
        before = copy.deepcopy(requirements)
        results = inspect_fonts(available, requirements)
        self.assertEqual(sum(item['installed'] for item in results), 1)
        self.assertEqual(requirements, before)
        self.assertEqual(available, ['Arial'])

    def test_status_explains_symbol_preparation_and_does_not_claim_renderer_is_ready(self):
        content = format_font_status(inspect_fonts(['Symbol']))
        self.assertIn('Symbol：導入済み', content)
        self.assertIn('Arial：未導入', content)
        self.assertIn('未実装', content)
        self.assertIn('自動ダウンロード', content)

    def test_manifest_rejects_unknown_fields_or_installer_commands(self):
        data = {'version': 1, 'fonts': load_font_requirements()}
        data['fonts'][0]['install_command'] = 'untrusted input'
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'fonts.json'
            path.write_text(json.dumps(data), encoding='utf-8')
            with self.assertRaises(StoreError):
                load_font_requirements(path)

    def test_manifest_rejects_duplicates_and_invalid_future_flag(self):
        with tempfile.TemporaryDirectory() as temporary:
            for duplicate in (True, False):
                data = {'version': 1, 'fonts': load_font_requirements()}
                if duplicate:
                    data['fonts'].append(data['fonts'][0])
                else:
                    data['fonts'][0]['future'] = 'yes'
                path = Path(temporary) / ('duplicate.json' if duplicate else 'flag.json')
                path.write_text(json.dumps(data), encoding='utf-8')
                with self.assertRaises(StoreError):
                    load_font_requirements(path)
