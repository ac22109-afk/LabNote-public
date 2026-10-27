import csv
import hashlib
import json
import multiprocessing
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from .store import CommitUnverifiedError, ConflictError, FileStore, StoreError, export_csv, identifier


def race_edit(root, item, barrier, queue):
    """Independent processes deliberately read the same base before saving."""
    try:
        store = FileStore(root)
        original_load = store.load
        def synchronized_load():
            result = original_load()
            barrier.wait(timeout=15)
            return result
        store.load = synchronized_load
        result = store.save('sample', {**item['data'], 'notes': str(multiprocessing.current_process().pid)},
                            'zz00001', record_id=item['id'], base_revision=item['revision_id'])
        queue.put(('ok', result['revision_id']))
    except Exception as exc:
        queue.put(('error', str(exc)))


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='labnote-file-test-')
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.store = FileStore.create(self.base / 'records-data')

    def sample(self, **changes):
        return self.store.save('sample', {'label': 'S-001', 'creator': 'ab00001', 'material': 'Al', **changes}, 'zz00001')

    def measurement(self, sample, **changes):
        return self.store.save('measurement', {'sample_id': sample['id'], 'measurer': 'mc00001', 'method': 'SEM',
            'measured_on': '2026-10-19', 'values': [], **changes}, 'zz00002')

    def test_existing_folder_is_never_adopted_or_overwritten(self):
        existing = self.base / 'existing'
        existing.mkdir()
        (existing / 'important.txt').write_text('preserve', encoding='utf-8')
        with self.assertRaises(FileExistsError):
            FileStore.create(existing)
        self.assertEqual((existing / 'important.txt').read_text(), 'preserve')
        with self.assertRaises(OSError):
            FileStore(existing)

    def test_unicode_sample_reopens_from_a_new_client(self):
        item = self.sample(notes='日本語と画像だけの記録も扱う')
        snapshot = FileStore(self.store.root).load()
        self.assertEqual(snapshot.current(item['id'])['data']['notes'], item['data']['notes'])
        self.assertEqual(snapshot.warnings, [])

    def test_multiple_independent_clients_register_different_records(self):
        first = self.sample()
        second = FileStore(self.store.root).save('sample', {'label': 'S-002', 'creator': 'zz00001', 'material': 'Fe'}, 'zz00001')
        self.assertNotEqual(first['id'], second['id'])
        self.assertEqual(len(self.store.load().of_kind('sample')), 2)

    def test_duplicate_label_checked_after_width_case_normalization(self):
        self.sample()
        with self.assertRaises(StoreError):
            self.sample(label='ｓ-００１')
        other = self.sample(creator='他の作製者')
        self.assertEqual(other['data']['label'], 'S-001')

    def test_duplicate_label_in_the_trash_says_where_the_match_is(self):
        item = self.sample()
        self.store.set_visibility(item, 'zz00001', True, '誤登録')
        with self.assertRaises(StoreError) as trashed:
            self.sample()
        # The match is in no list, so the message has to name the trash.
        self.assertIn('ごみ箱', str(trashed.exception))
        self.assertEqual(self.store.load().of_kind('sample'), [])
        self.sample(label='S-002')
        with self.assertRaises(StoreError) as visible:
            self.sample(label='S-002')
        self.assertNotIn('ごみ箱', str(visible.exception))
        self.assertEqual(str(visible.exception), '同じ作製者の試料IDが既にあります。')

    def test_file_only_measurement_keeps_immutable_sample_reference(self):
        sample = self.sample()
        measurement = self.measurement(sample)
        revised = self.store.save('sample', {**sample['data'], 'label': '訂正後ID'}, 'zz00001',
                                  record_id=sample['id'], base_revision=sample['revision_id'])
        self.assertEqual(revised['id'], measurement['data']['sample_id'])
        self.assertEqual(self.store.load().current(measurement['id'])['data']['values'], [])

    def test_missing_sample_cannot_be_referenced(self):
        with self.assertRaises(StoreError):
            self.measurement({'id': identifier()})

    def test_invalid_numbers_and_dates_never_commit(self):
        sample = self.sample()
        for values in [['NaN'], ['Infinity'], ['1e100'], ['0.1234567890123']]:
            with self.subTest(values=values), self.assertRaises(StoreError):
                self.measurement(sample, values=values)
        with self.assertRaises(StoreError):
            self.measurement(sample, measured_on='2026-02-30')
        self.assertEqual(len(self.store.load().heads), 1)

    def test_treatment_has_valid_units_and_nonnegative_duration(self):
        sample = self.sample()
        for changes in [{'duration': '-1'}, {'duration_unit': 'seconds'}]:
            with self.assertRaises(StoreError):
                self.store.save('treatment', {'sample_id': sample['id'], 'method': 'heat', **changes}, 'zz00001')
        item = self.store.save('treatment', {'sample_id': sample['id'], 'method': 'heat', 'duration': '3.6', 'duration_unit': 'ks'}, 'zz00001')
        self.assertEqual(item['data']['duration'], '3.6')

    def test_revision_preserves_original_and_rejects_stale_edits(self):
        first = self.sample()
        original = self.store._revision_dir(first['id'], first['revision_id']) / 'document.json'
        original_bytes = original.read_bytes()
        second = self.store.save('sample', {**first['data'], 'notes': '訂正'}, 'zz00001', record_id=first['id'], base_revision=first['revision_id'])
        with self.assertRaises(ConflictError):
            self.store.save('sample', first['data'], 'zz00001', record_id=first['id'], base_revision=first['revision_id'])
        self.assertEqual(original.read_bytes(), original_bytes)
        self.assertEqual(len(self.store.load().revisions[first['id']]), 2)
        self.assertEqual(self.store.load().current(first['id'])['revision_id'], second['revision_id'])

    def test_display_name_rules_do_not_grant_sample_owner_measurement_control(self):
        sample = self.sample()
        measurement = self.measurement(sample)
        with self.assertRaises(StoreError):
            self.store.save('measurement', measurement['data'], 'ab00001', record_id=measurement['id'], base_revision=measurement['revision_id'])
        with self.assertRaises(StoreError):
            self.store.save('sample', {**sample['data'], 'creator': '別の作製者'}, 'zz00001', record_id=sample['id'], base_revision=sample['revision_id'])

    def test_partial_save_is_reported_but_not_listed(self):
        sample = self.sample()
        incomplete = self.store._revision_dir(sample['id'], identifier())
        incomplete.mkdir()
        (incomplete / 'document.json').write_bytes(b'{"partial":')
        snapshot = self.store.load()
        self.assertEqual(snapshot.current(sample['id'])['revision_id'], sample['revision_id'])
        self.assertTrue(any('未完了' in warning for warning in snapshot.warnings))

    def test_corrupt_json_does_not_hide_all_other_valid_records(self):
        sample = self.sample()
        other = self.sample(label='S-002')
        target = self.store._revision_dir(other['id'], other['revision_id']) / 'document.json'
        target.write_bytes(b'broken')
        snapshot = self.store.load()
        self.assertEqual(len(snapshot.heads), 1)
        self.assertIn(sample['id'], snapshot.heads)
        self.assertTrue(snapshot.warnings)

    def test_uuid_path_traversal_is_rejected(self):
        with self.assertRaises(StoreError):
            self.store.read_revision('../outside', identifier())
        with self.assertRaises(StoreError):
            self.store.read_revision(identifier(), '../../outside')

    def test_attachment_stream_hash_and_export_without_overwriting(self):
        source = self.base / '測定データ.html'
        payload = b'<script>not executed</script>'
        source.write_bytes(payload)
        item = self.store.save('sample', {'label': 'S', 'creator': 'aa00001', 'material': 'Al'}, 'aa00001', files=[source])
        attachment = item['attachments'][0]
        self.assertEqual(attachment['sha256'], hashlib.sha256(payload).hexdigest())
        target = self.base / 'download.dat'
        self.store.export_attachment(item, attachment['id'], target)
        self.assertEqual(target.read_bytes(), payload)
        with self.assertRaises(FileExistsError):
            self.store.export_attachment(item, attachment['id'], target)
        self.assertEqual(target.read_bytes(), payload)
        revised = self.store.save('sample', {**item['data'], 'notes': 'new'}, 'aa00001', record_id=item['id'], base_revision=item['revision_id'])
        self.assertEqual(revised['attachments'], item['attachments'])

    def test_attachment_tamper_is_detected_on_export(self):
        source = self.base / 'raw.dat'
        source.write_bytes(b'original')
        item = self.store.save('sample', {'label': 'S', 'creator': 'aa00001', 'material': 'Al'}, 'aa00001', files=[source])
        attachment = item['attachments'][0]
        blob = self.store._revision_dir(item['id'], attachment['event_id']) / 'files' / attachment['id']
        blob.write_bytes(b'tampered')
        destination = self.base / 'untrusted-copy.dat'
        with self.assertRaises(StoreError):
            self.store.export_attachment(item, attachment['id'], destination)
        self.assertFalse(destination.exists())
        self.assertEqual(len(list(self.base.glob('.*.part'))), 1)

    def test_oversize_attachment_does_not_commit(self):
        source = self.base / 'big.dat'
        source.write_bytes(b'12345')
        with patch('fileclient.store.MAX_FILE', 4), self.assertRaises(StoreError):
            self.store.save('sample', {'label': 'S', 'creator': 'aa00001', 'material': 'Al'}, 'aa00001', files=[source])
        snapshot = self.store.load()
        self.assertFalse(snapshot.heads)
        self.assertFalse(snapshot.warnings)

    def test_csv_escapes_formulas_and_never_replaces_existing_file(self):
        self.sample(notes='=HYPERLINK("bad")')
        target = self.base / 'samples.csv'
        export_csv(self.store.load(), target)
        with target.open(encoding='utf-8-sig', newline='') as stream:
            rows = list(csv.reader(stream))
        self.assertTrue(rows[1][6].startswith("'="))
        with self.assertRaises(FileExistsError):
            export_csv(self.store.load(), target)

    def test_two_processes_racing_keep_both_revisions_and_report_conflict(self):
        sample = self.sample()
        context = multiprocessing.get_context('spawn')
        barrier, queue = context.Barrier(2), context.Queue()
        processes = [context.Process(target=race_edit, args=(str(self.store.root), sample, barrier, queue)) for _ in range(2)]
        for process in processes:
            process.start()
        results = [queue.get(timeout=25) for _ in processes]
        for process in processes:
            process.join(timeout=10)
            self.assertEqual(process.exitcode, 0)
        self.assertTrue(all(result[0] == 'ok' for result in results), results)
        snapshot = self.store.load()
        self.assertEqual(len(snapshot.revisions[sample['id']]), 3)
        self.assertEqual(len(snapshot.heads[sample['id']]), 2)
        with self.assertRaises(ConflictError):
            snapshot.current(sample['id'])
        self.assertTrue(any('競合' in warning for warning in snapshot.warnings))

    def test_unreadable_newer_revision_blocks_editing_from_older_base(self):
        first = self.sample()
        second = self.store.save('sample', {**first['data'], 'notes': '新'}, 'zz00001', record_id=first['id'], base_revision=first['revision_id'])
        directory = self.store._revision_dir(first['id'], second['revision_id'])
        (directory / 'document.json').write_bytes(b'broken')
        snapshot = self.store.load()
        self.assertIn(first['id'], snapshot.blocked)
        with self.assertRaises(StoreError):
            snapshot.current(first['id'])
        with self.assertRaises(StoreError):
            self.store.save('sample', first['data'], 'zz00001', record_id=first['id'], base_revision=first['revision_id'])
        self.assertEqual(len(list(directory.parent.iterdir())), 2)

    def test_unreadable_middle_revision_blocks_its_descendants(self):
        first = self.sample()
        second = self.store.save('sample', first['data'], 'zz00001', record_id=first['id'], base_revision=first['revision_id'])
        self.store.save('sample', first['data'], 'zz00001', record_id=first['id'], base_revision=second['revision_id'])
        original = self.store.read_revision
        def flaky(record_id, revision_id):
            if revision_id == second['revision_id']:
                raise OSError('temporary unreadable parent')
            return original(record_id, revision_id)
        with patch.object(self.store, 'read_revision', side_effect=flaky):
            snapshot = self.store.load()
        self.assertIn(first['id'], snapshot.blocked)
        self.assertTrue(any('元の改訂' in warning for warning in snapshot.warnings))

    def test_unreadable_record_folder_does_not_hide_other_records(self):
        first, second = self.sample(), self.sample(label='S-002')
        original = Path.iterdir
        def flaky(path):
            if path.name == second['id']:
                raise PermissionError('denied')
            return original(path)
        with patch.object(Path, 'iterdir', flaky):
            snapshot = self.store.load()
        self.assertIn(first['id'], snapshot.heads)
        self.assertIn(second['id'], snapshot.blocked)

    def test_unreadable_commit_is_not_treated_as_unpublished(self):
        sample = self.sample()
        original = Path.stat
        def flaky(path, *args, **kwargs):
            if path.name == 'commit.json':
                raise PermissionError('denied')
            return original(path, *args, **kwargs)
        with patch.object(Path, 'stat', flaky):
            snapshot = self.store.load()
        self.assertIn(sample['id'], snapshot.blocked)
        self.assertFalse(any('保存途中' in warning for warning in snapshot.warnings))

    def test_read_failure_after_commit_is_reported_as_unverified(self):
        with patch.object(self.store, 'read_revision', side_effect=OSError('smb')):
            with self.assertRaises(CommitUnverifiedError) as caught:
                self.sample()
        self.assertEqual(self.store.load().current(caught.exception.record_id)['revision_id'], caught.exception.revision_id)

    def test_rename_succeeded_but_reported_error_is_unverified(self):
        import os
        original = os.rename
        def uncertain(source, target):
            original(source, target)
            raise OSError('reply lost')
        with patch('fileclient.store.os.rename', uncertain):
            with self.assertRaises(CommitUnverifiedError):
                self.sample()
        self.assertEqual(len(self.store.load().of_kind('sample')), 1)

    def test_failed_csv_never_uses_requested_final_filename(self):
        self.sample()
        snapshot = self.store.load()
        snapshot.of_kind('sample')[0]['data'].pop('label')
        destination = self.base / 'incomplete.csv'
        with self.assertRaises(KeyError):
            export_csv(snapshot, destination)
        self.assertFalse(destination.exists())
        self.assertEqual(len(list(self.base.glob('.*.part'))), 1)

    def test_missing_optional_fields_are_normalized_on_read(self):
        sample = self.sample()
        directory = self.store._revision_dir(sample['id'], sample['revision_id'])
        document = json.loads((directory / 'document.json').read_bytes())
        del document['data']['alloy'], document['data']['notes']
        raw = json.dumps(document, ensure_ascii=False).encode('utf-8')
        (directory / 'document.json').write_bytes(raw)
        (directory / 'commit.json').write_text(json.dumps({'sha256': hashlib.sha256(raw).hexdigest()}))
        destination = self.base / 'normalized.csv'
        export_csv(self.store.load(), destination)
        with destination.open(encoding='utf-8-sig', newline='') as stream:
            self.assertEqual(list(csv.reader(stream))[1][5], '')


if __name__ == '__main__':
    unittest.main()
