import hashlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from .index import LocalSession, RecordIndex
from .session import DesktopSession
from .store import CommitUnverifiedError, FileStore, StoreError, fs_path


class IndexTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='labnote-index-test-')
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        temporary.name = str(fs_path(self.base))
        self.shared = FileStore.create(self.base / 'shared')
        self.session = DesktopSession(self.shared.root, self.shared.store_id, cache_home=self.base / 'pc')

    def sample(self, label='S-001'):
        return self.session.save('sample', {'label': label, 'creator': 'zz00001',
            'material': 'アルミニウム合金', 'alloy': 'A5052'}, 'zz00001')

    def restart(self):
        return DesktopSession(self.shared.root, self.shared.store_id, cache_home=self.base / 'pc')

    def test_related_search_is_sqlite_and_includes_unsent(self):
        sample = self.sample()
        measurement = self.session.save('measurement', {'sample_id': sample['id'], 'measurer': 'zz00002',
            'method': 'SEM', 'metric': '粒子径', 'unit': 'μm', 'values': ['1.2'],
            'measured_on': '2026-10-20', 'notes': '特別な観察'}, 'zz00001')
        with patch.object(FileStore, 'load', side_effect=AssertionError('cached operation scanned JSON')):
            self.assertEqual(self.session.search_samples('ＳＥＭ'), {(sample['id'], sample['revision_id'])})
            self.assertEqual(self.session.search_samples('特別な観察'), {(sample['id'], sample['revision_id'])})
            self.assertEqual(len(self.session.pending()), 2)
            self.assertEqual(self.session.cached().current(measurement['id'])['data']['values'], ['1.2'])

    def test_cached_save_never_scans_all_json(self):
        self.sample()
        with patch.object(FileStore, 'load', side_effect=AssertionError('new save scanned JSON')):
            item = self.sample('S-002')
        self.assertEqual(self.session.cached().current(item['id'])['data']['label'], 'S-002')

    def test_edited_head_replaces_searchable_old_value_across_sources(self):
        sample = self.sample('OLD-LABEL')
        self.session.sync()
        self.session.load()
        edited = self.session.save('sample', {**sample['data'], 'label': 'NEW-LABEL'}, 'zz00001',
                                  record_id=sample['id'], base_revision=sample['revision_id'])
        self.assertEqual(self.session.search_samples('OLD-LABEL'), set())
        self.assertEqual(self.session.search_samples('NEW-LABEL'), {(sample['id'], edited['revision_id'])})
        self.assertEqual(len(self.session.cached().revisions[sample['id']]), 2)

    def test_committed_json_not_yet_indexed_is_recovered_at_restart(self):
        item = self.session.local.save('sample', {'label': 'CRASH-WINDOW', 'creator': 'zz00001', 'material': 'Al'},
            'zz00001', origin={'store_id': self.shared.store_id, 'client_id': self.session.local.store_id})
        self.assertEqual(self.session.pending(), [])
        restarted = self.restart()
        self.assertEqual([i['id'] for i in restarted.pending()], [item['id']])

    def test_index_failure_after_commit_is_unverified_not_resubmittable_failure(self):
        with patch.object(self.session.index, 'record', side_effect=sqlite3.OperationalError('index disk full')):
            with self.assertRaises(CommitUnverifiedError) as raised:
                self.sample()
        error = raised.exception
        self.assertEqual(self.session.local.read_revision(error.record_id, error.revision_id)['registrant'], 'zz00001')
        self.assertEqual(len(self.restart().pending()), 1)

    def test_corrupt_index_rebuilds_without_touching_records_or_receipts(self):
        sample = self.sample()
        self.session.sync()
        document = self.session.local._revision_dir(sample['id'], sample['revision_id']) / 'document.json'
        original = document.read_bytes()
        receipt = self.session.receipts / (sample['revision_id'] + '.json')
        confirmation = receipt.read_bytes()
        self.session.index.path.write_bytes(b'broken disposable index')
        restarted = self.restart()
        self.assertTrue(restarted.index.rebuilt)
        self.assertEqual(restarted.pending(), [])
        self.assertEqual(document.read_bytes(), original)
        self.assertEqual(receipt.read_bytes(), confirmation)
        self.assertEqual(len(list(restarted.index.path.parent.glob('search.sqlite.*.unusable'))), 1)

    def test_unchanged_reconcile_reads_no_json_revisions(self):
        self.sample()
        restarted = self.restart()
        self.assertEqual(restarted.index.last_reads, {'outbox': 0, 'mirror': 0})

    def test_changed_invalid_json_is_blocked_on_reconcile(self):
        sample = self.sample()
        directory = self.session.local._revision_dir(sample['id'], sample['revision_id'])
        (directory / 'document.json').write_bytes(b'bad JSON')
        restarted = self.restart()
        self.assertIn(sample['id'], restarted.cached().blocked)
        with self.assertRaises(StoreError):
            restarted.pending()

    def test_lost_ack_from_index_is_restored_from_confirmation_file(self):
        self.sample()
        self.session.sync()
        with self.session.index.connection() as db:
            db.execute('DELETE FROM acknowledgments')
        self.assertEqual(self.session.pending(), [])
        self.assertEqual(len(self.shared.load().heads), 1)

    def test_corrupt_confirmation_does_not_become_sent_after_restart(self):
        sample = self.sample()
        self.session.sync()
        receipt = self.session.receipts / (sample['revision_id'] + '.json')
        receipt.write_text('{bad', encoding='utf-8')
        restarted = self.restart()
        self.assertIn(sample['id'], restarted.cached().heads)
        with self.assertRaises(StoreError):
            restarted.pending()

    def test_local_demo_uses_same_sqlite_and_keeps_revision_history(self):
        demo = LocalSession(FileStore.create(self.base / 'demo'))
        first = demo.save('sample', {'label': 'D-001', 'creator': 'zz00001', 'material': 'Al'}, 'zz00001')
        second = demo.save('sample', {**first['data'], 'label': 'D-002'}, 'zz00001',
                           record_id=first['id'], base_revision=first['revision_id'])
        self.assertEqual(demo.search_samples('D-001'), set())
        self.assertEqual(demo.search_samples('D-002'), {(first['id'], second['revision_id'])})
        self.assertEqual(len(demo.cached().revisions[first['id']]), 2)

    def test_index_for_wrong_source_is_preserved_and_rebuilt(self):
        self.sample()
        with self.session.index.connection() as db:
            db.execute('UPDATE meta SET value=? WHERE key=?', ('different-source', 'sources'))
        restarted = self.restart()
        self.assertTrue(restarted.index.rebuilt)
        self.assertEqual(len(restarted.pending()), 1)

    def test_rebuild_refuses_network_index_path_before_access(self):
        with self.assertRaises(StoreError):
            RecordIndex(Path(r'\\unreachable-test\share\search.sqlite'), {'local': self.shared})

    def test_explicit_rebuild_preserves_raw_json(self):
        sample = self.sample()
        document = self.session.local._revision_dir(sample['id'], sample['revision_id']) / 'document.json'
        digest = hashlib.sha256(document.read_bytes()).hexdigest()
        self.session.rebuild_index()
        self.assertEqual(hashlib.sha256(document.read_bytes()).hexdigest(), digest)
        self.assertEqual(len(self.session.pending()), 1)

    def test_failed_index_commit_recovers_before_next_save_or_send(self):
        with patch.object(self.session.index, 'record', side_effect=sqlite3.OperationalError('index write failed')):
            with self.assertRaises(CommitUnverifiedError):
                self.sample()
        self.assertEqual(len(self.session.pending()), 1)
        with self.assertRaises(StoreError):
            self.sample()
        self.assertEqual(self.session.sync(), (1, [], 0))

    def test_shared_edit_caches_ancestors_for_offline_restart(self):
        sample = self.sample()
        self.session.sync()
        edited = self.session.save('sample', {**sample['data'], 'notes': 'edited without a prior shared refresh'},
            'zz00001', record_id=sample['id'], base_revision=sample['revision_id'])
        restarted = self.restart()
        self.assertNotIn(sample['id'], restarted.cached().blocked)
        self.assertEqual(restarted.cached().current(sample['id'])['revision_id'], edited['revision_id'])

    def test_stale_item_is_not_cached_with_new_fingerprint(self):
        sample = self.sample()
        directory = self.session.local._revision_dir(sample['id'], sample['revision_id'])
        item = json.loads((directory / 'document.json').read_text(encoding='utf-8'))
        item['data']['notes'] = 'changed externally in test fixture'
        raw = json.dumps(item, ensure_ascii=False).encode('utf-8')
        (directory / 'document.json').write_bytes(raw)
        (directory / 'commit.json').write_text(json.dumps({'sha256': hashlib.sha256(raw).hexdigest()}))
        with self.assertRaises(StoreError):
            self.session.index.record('outbox', sample)
        self.assertEqual(self.session.cached().current(sample['id'])['data']['notes'], item['data']['notes'])

    def test_missing_parent_cannot_be_recorded_from_stale_index(self):
        demo_store = FileStore.create(self.base / 'ancestry')
        demo = LocalSession(demo_store)
        sample = demo.save('sample', {'label': 'A', 'creator': 'zz00001', 'material': 'Al'}, 'zz00001')
        child = demo_store.save('sample', {**sample['data'], 'notes': 'child'}, 'zz00001',
            record_id=sample['id'], base_revision=sample['revision_id'])
        demo_store._revision_dir(sample['id'], sample['revision_id']).rename(self.base / 'retained-parent-fixture')
        with self.assertRaises(StoreError):
            demo.index.record('local', child)
        self.assertIn(sample['id'], demo.cached().blocked)

    def test_extended_corruption_code_triggers_rebuild(self):
        self.sample()
        original = RecordIndex._initialize
        first = True
        def initialize(index):
            nonlocal first
            if first:
                first = False
                error = sqlite3.DatabaseError('simulated extended SQLITE_CORRUPT_INDEX')
                error.sqlite_errorcode = sqlite3.SQLITE_CORRUPT | (3 << 8)
                raise error
            return original(index)
        with patch.object(RecordIndex, '_initialize', initialize):
            restarted = self.restart()
        self.assertTrue(restarted.index.rebuilt)
        self.assertEqual(len(restarted.pending()), 1)

    def test_live_index_corruption_recovers_on_next_cached_query(self):
        sample = self.sample()
        self.session.index.path.write_bytes(b'broken while app running')
        self.assertIn(sample['id'], self.session.cached().heads)
        self.assertTrue(self.session.index.rebuilt)
        self.assertEqual(len(self.session.pending()), 1)

    def test_invalid_outbox_is_visible_on_refresh_and_sync_reports_unknown(self):
        sample = self.sample()
        directory = self.session.local._revision_dir(sample['id'], sample['revision_id'])
        (directory / 'document.json').write_bytes(b'broken fixture')
        snapshot = self.session.load()
        self.assertTrue(snapshot.warnings)
        self.assertIn(sample['id'], snapshot.blocked)
        sent, errors, remaining = self.session.sync()
        self.assertEqual((sent, remaining), (0, '確認必要'))
        self.assertTrue(errors)
