import csv
import tempfile
import unittest
from dataclasses import replace
from datetime import date
from pathlib import Path
from unittest.mock import patch

import openpyxl

from .bulk_import import preview_import, commit_import, export_template, suggest_mapping
from .catalog import Catalog
from .session import DesktopSession
from .store import FileStore, StoreError, CommitUnverifiedError
from .tables import read_table, sheet_names, Table


class BulkImportTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='labnote-bulk-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.shared = FileStore.create(self.root / 'shared')
        self.sample = self.shared.save('sample', {'label': '200d-24h-10k_01', 'creator': 'zz00001',
                                       'material': 'アルミニウム合金', 'alloy': 'A5052'}, 'zz00001')
        self.client = DesktopSession(self.shared.root, self.shared.store_id, cache_home=self.root / 'pc')
        self.client.load()
        self.catalog = Catalog()

    def table(self, rows=None):
        return Table(('試料ID', '作製者', '測定者', '測定方法', '測定項目', '単位', '測定日', '数値', '測定セット名'),
                     tuple(rows or [('200d-24h-10k_01', 'zz00001', 'zz00002', 'SEM', '結晶粒径', 'μm', '2026-10-21', '1.2', 'batch-1'),
                                    ('200d-24h-10k_01', 'zz00001', 'zz00002', 'SEM', '結晶粒径', 'μm', '2026-10-21', '2.4', 'batch-1')]), 'a'*64)

    def preview(self, table=None):
        table = table or self.table()
        return preview_import(table, 'measurement', suggest_mapping(table.headers, 'measurement'), self.client.cached(), self.catalog, 'zz00001')

    def test_grouped_values_save_offline_send_and_reimport_from_fresh_pc_skips(self):
        entries = self.preview()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].data['values'], ['1.2', '2.4'])
        self.assertEqual(entries[0].data['sample_id'], self.sample['id'])
        before = len(self.shared.load().heads)
        saved, error = commit_import(entries, self.client, 'zz00001')
        self.assertEqual((len(saved), error), (1, ''))
        self.assertEqual(len(self.shared.load().heads), before)
        self.assertEqual(self.client.sync(), (1, [], 0))
        self.client = DesktopSession(self.shared.root, self.shared.store_id, cache_home=self.root / 'pc2')
        self.client.load()
        entries = self.preview()
        self.assertEqual(entries[0].status, '取込済み')
        self.assertEqual(commit_import(entries, self.client, 'zz00001'), ([], ''))

    def test_batch_mismatch_and_ambiguous_sample_and_unit_case_are_errors(self):
        table = self.table()
        rows = list(table.rows)
        rows[1] = (*rows[1][:6], '2026-10-20', *rows[1][7:])
        self.assertEqual(self.preview(replace(table, rows=tuple(rows)))[0].status, '要修正')
        self.shared.save('sample', {**self.sample['data'], 'creator': 'zz00002'}, 'zz00002')
        self.client.load()
        no_creator = replace(table, rows=tuple(tuple('' if i == 1 else value for i, value in enumerate(row)) for row in table.rows))
        self.assertIn('特定', self.preview(no_creator)[0].detail)
        bad = replace(table, rows=tuple(tuple('UM' if i == 5 else value for i, value in enumerate(row)) for row in table.rows))
        self.assertEqual(self.preview(bad)[0].status, '要修正')

    def test_partial_uncertain_save_can_be_previewed_without_duplicate(self):
        entries = self.preview()
        original = self.client.save
        def uncertain(*args, **kwargs):
            item = original(*args, **kwargs)
            raise CommitUnverifiedError(item['id'], item['revision_id'])
        with patch.object(self.client, 'save', side_effect=uncertain):
            saved, error = commit_import(entries, self.client, 'zz00001')
        self.assertTrue(error)
        self.assertEqual(saved, [])
        self.assertEqual(self.preview()[0].status, '取込済み')
        self.assertEqual(len(self.client.pending()), 1)

    def test_sample_template_and_normalized_candidates_round_trip(self):
        for suffix in ('.csv', '.xlsx'):
            with self.subTest(suffix=suffix):
                path = self.root / ('sample' + suffix)
                export_template('sample', path)
                table = read_table(path)
                self.assertEqual(table.rows[0][0], '200d-24h-10k_01')
                entries = preview_import(table, 'sample', suggest_mapping(table.headers, 'sample'), self.client.cached(), self.catalog, 'zz00001')
                self.assertEqual(entries[0].status, '要修正')  # Existing sample is never overwritten.
                with self.assertRaises(FileExistsError):
                    export_template('sample', path)

    def test_xlsx_dates_sheets_and_formula_refusal(self):
        path = self.root / 'book.xlsx'
        book = openpyxl.Workbook()
        book.active.append(['formula'])
        book.active.append(['=1+1'])
        sheet = book.create_sheet('測定')
        sheet.append(['日付', '数値'])
        sheet.append([date(2026, 10, 21), 1.2])
        book.save(path)
        book.close()
        self.assertEqual(sheet_names(path), ('Sheet', '測定'))
        with self.assertRaises(StoreError):
            read_table(path)
        self.assertEqual(read_table(path, sheet='測定').rows[0], ('2026-10-21', '1.2'))

    def test_csv_encoding_limits_and_duplicate_headers(self):
        path = self.root / 'table.csv'
        path.write_bytes('試料ID,数値\n200d-24h-10k_01,1\n'.encode('cp932'))
        table = read_table(path)
        self.assertEqual(table.encoding, 'cp932')
        self.assertEqual(table.rows[0][0], '200d-24h-10k_01')
        with self.assertRaises(StoreError):
            read_table(path, max_rows=0)
        path.write_text('数値,数値\n1,2', encoding='utf-8')
        with self.assertRaises(StoreError):
            read_table(path)

    def test_validation_errors_prevent_any_rows_being_saved(self):
        table = self.table()
        table = replace(table, rows=(table.rows[0], tuple('bad-date' if index == 6 else '' if index == 8 else value for index, value in enumerate(table.rows[1]))))
        entries = self.preview(table)
        self.assertTrue(any(entry.status == '要修正' for entry in entries))
        with self.assertRaises(StoreError):
            commit_import(entries, self.client, 'zz00001')
        self.assertEqual(self.client.pending(), [])
