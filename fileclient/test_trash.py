import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from .comparison import comparison_rows
from .session import DesktopSession
from .store import UNKNOWN, FileStore, Snapshot, StoreError, ConflictError, export_csv


class TrashTests(unittest.TestCase):
    def test_trash_operations_are_held_when_the_chain_cannot_be_followed(self):
        # 未送信記録は index 由来のスナップショットを渡されるため blocked で弾かれない。
        # たどれない記録を復元すると、たどれない部分を無視して有効と決めてしまう。
        sample = self.store.save('sample', {'label': 'S1', 'creator': 'zz00001',
            'material': 'アルミニウム合金', 'alloy': 'A5052'}, 'zz00001')
        snapshot = self.store.load()
        with patch.object(Snapshot, 'status', lambda self, item, attachment_id=None:
                          {'state': UNKNOWN, 'reason': ''}):
            with self.assertRaises(StoreError) as caught:
                self.store.save('sample', sample['data'], 'zz00001',
                                record_id=sample['id'], base_revision=sample['revision_id'],
                                _visibility=(False, '復元したい', None), _snapshot=snapshot)
        self.assertIn('たどれない', str(caught.exception))

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='labnote-trash-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.store = FileStore.create(self.root / 'data')
        self.source = self.root / 'original.PNG'
        self.source.write_bytes(b'unchanged attachment')
        self.sample = self.store.save('sample', dict(label='200d-24h-10k_01', creator='zz00001', material='アルミニウム合金', alloy='A5052'), 'zz00001', files=[self.source])
        self.measurement = self.store.save('measurement', dict(sample_id=self.sample['id'], measurer='zz00002', method='SEM', metric='結晶粒径', unit='μm', measured_on='2026-10-21', values=['1', '3']), 'zz00002')

    def test_parent_archive_hides_children_exports_and_graphs_restore_keeps_bytes(self):
        original_raw = (self.store._revision_dir(self.sample['id'], self.sample['revision_id']) / 'document.json').read_bytes()
        deleted = self.store.set_visibility(self.sample, 'zz00001', True, '誤登録')
        snapshot = self.store.load()
        self.assertEqual(snapshot.of_kind('sample'), [])
        self.assertEqual(snapshot.of_kind('measurement'), [])
        self.assertEqual(comparison_rows(snapshot)[0], [])
        self.assertEqual(len(snapshot.of_kind('measurement', include_deleted=True)), 1)
        destination = self.root / 'samples.csv'
        export_csv(snapshot, destination)
        self.assertNotIn('200d-', destination.read_text(encoding='utf-8-sig'))
        self.assertEqual(snapshot.status(deleted)['actor'], 'zz00001')
        restored = self.store.set_visibility(deleted, 'zz00001', False, '誤削除の復元')
        self.assertEqual(len(self.store.load().of_kind('measurement')), 1)
        exported = self.root / 'restored.PNG'
        self.store.export_attachment(restored, restored['attachments'][0]['id'], exported)
        self.assertEqual(exported.read_bytes(), self.source.read_bytes())
        self.assertEqual((self.store._revision_dir(self.sample['id'], self.sample['revision_id']) / 'document.json').read_bytes(), original_raw)

    def test_individually_deleted_child_stays_deleted_after_parent_restore(self):
        self.store.set_visibility(self.measurement, 'zz00002', True, '測定の誤登録')
        deleted = self.store.set_visibility(self.sample, 'zz00001', True, '試料の誤登録')
        self.store.set_visibility(deleted, 'zz00001', False, '試料のみ復元')
        self.assertEqual(self.store.load().of_kind('measurement'), [])

    def test_permission_reason_stale_base_and_duplicate_action_are_rejected(self):
        for actor, reason in [('zz00000', '理由'), ('zz00001', '')]:
            with self.assertRaises(StoreError):
                self.store.set_visibility(self.sample, actor, True, reason)
        deleted = self.store.set_visibility(self.sample, 'zz00001', True, '理由')
        with self.assertRaises(ConflictError):
            self.store.set_visibility(self.sample, 'zz00001', True, '二重操作')
        with self.assertRaises(StoreError):
            self.store.set_visibility(deleted, 'zz00001', True, '二重操作')
        with self.assertRaises(StoreError):
            self.store.save('measurement', self.measurement['data'], 'zz00002')
        with self.assertRaises(StoreError):
            self.store.save('sample', self.sample['data'], 'zz00001')

    def test_attachment_status_survives_edits_and_restores_without_blob_removal(self):
        attachment = self.sample['attachments'][0]
        deleted = self.store.set_visibility(self.sample, 'zz00001', True, '添付の取り違え', attachment_id=attachment['id'])
        self.assertFalse(self.store.load().hidden(deleted))
        edited = self.store.save('sample', {**deleted['data'], 'notes': '通常の訂正'}, 'zz00001', record_id=deleted['id'], base_revision=deleted['revision_id'])
        self.assertEqual(self.store.load().status(edited, attachment['id'])['state'], 'deleted')
        restored = self.store.set_visibility(edited, 'zz00001', False, '添付を復元', attachment_id=attachment['id'])
        self.assertEqual(self.store.load().status(restored, attachment['id'])['state'], 'active')
        self.assertEqual(len(restored['attachments']), 1)

    def test_old_client_edit_cannot_silently_resurrect_deleted_record(self):
        deleted = self.store.set_visibility(self.sample, 'zz00001', True, '理由')
        snapshot = self.store.load()
        # Model a 1.1 client that has no deletion visibility check.
        with patch.object(type(snapshot), 'hidden', return_value=False):
            old_edit = self.store.save('sample', {**deleted['data'], 'notes': 'old client'}, 'zz00001', record_id=deleted['id'], base_revision=deleted['revision_id'])
        self.assertNotIn('lifecycle', old_edit)
        self.assertTrue(self.store.load().hidden(old_edit))

    def test_unreadable_ancestor_is_unknown_and_never_looks_active(self):
        deleted = self.store.set_visibility(self.sample, 'zz00001', True, '誤登録')
        snapshot = self.store.load()
        # A later ordinary revision carries no lifecycle of its own, so the
        # state is only readable through its parent.
        with patch.object(type(snapshot), 'hidden', return_value=False):
            edited = self.store.save('sample', {**deleted['data'], 'notes': '削除後の訂正'}, 'zz00001',
                                     record_id=deleted['id'], base_revision=deleted['revision_id'])
        self.assertNotIn('lifecycle', edited)
        snapshot = self.store.load()
        self.assertTrue(snapshot.hidden(edited))
        # Model the revision holding the decision becoming unreadable, as an
        # index-built snapshot can when an ancestor is missing.
        del snapshot.revisions[edited['id']][deleted['revision_id']]
        self.assertEqual(snapshot.status(edited)['state'], 'unknown')
        self.assertTrue(snapshot.hidden(edited))
        self.assertEqual(snapshot.of_kind('sample'), [])
        self.assertEqual(snapshot.of_kind('measurement'), [])
        self.assertEqual(comparison_rows(snapshot)[0], [])
        destination = self.root / 'unknown.csv'
        export_csv(snapshot, destination)
        self.assertNotIn('200d-', destination.read_text(encoding='utf-8-sig'))
        # Still reachable for the trash list, which marks it as needing a check.
        self.assertEqual([i['id'] for i in snapshot.of_kind('sample', include_deleted=True)], [edited['id']])

    def test_blocked_record_with_unreadable_revision_is_held_not_shown(self):
        edited = self.store.save('sample', {**self.sample['data'], 'notes': '二つ目の改訂'}, 'zz00001',
                                 record_id=self.sample['id'], base_revision=self.sample['revision_id'])
        directory = self.store._revision_dir(self.sample['id'], self.sample['revision_id'])
        (directory / 'document.json').write_bytes(b'{"broken"')
        snapshot = self.store.load()
        # load() refuses the whole ancestry, so the record is held, not listed.
        self.assertIn(self.sample['id'], snapshot.blocked)
        self.assertEqual(snapshot.of_kind('sample'), [])
        self.assertEqual(snapshot.of_kind('sample', include_deleted=True), [])
        with self.assertRaises(StoreError):
            snapshot.current(self.sample['id'])
        # A snapshot that kept the orphan head must not report it as active.
        snapshot.heads[self.sample['id']] = [edited]
        snapshot.revisions.setdefault(self.sample['id'], {})[edited['revision_id']] = edited
        self.assertEqual(snapshot.status(edited)['state'], 'unknown')
        self.assertTrue(snapshot.hidden(edited))
        self.assertEqual(snapshot.of_kind('sample'), [])

    def test_unsent_delete_stays_local_and_can_be_restored_then_synced(self):
        client = DesktopSession(self.store.root, self.store.store_id, cache_home=self.root / 'pc')
        client.load()
        item = client.save('sample', {**self.sample['data'], 'label': 'UNSENT'}, 'zz00001')
        child = client.save('measurement', {**self.measurement['data'], 'sample_id': item['id']}, 'zz00002')
        deleted = client.set_visibility(item, 'zz00001', True, '送信前に取消')
        self.assertEqual(client.pending(), [])
        self.assertEqual(client.sync(), (0, [], 0))
        self.assertNotIn(item['id'], self.store.load().heads)
        reopened = DesktopSession(self.store.root, self.store.store_id, cache_home=self.root / 'pc')
        reopened.load()
        reopened.set_visibility(deleted, 'zz00001', False, '復元')
        sent, errors, remaining = reopened.sync()
        self.assertEqual((sent, errors, remaining), (4, [], 0))
        snapshot = self.store.load()
        self.assertEqual(snapshot.current(item['id'])['data']['label'], 'UNSENT')
        self.assertEqual(snapshot.current(child['id'])['data']['sample_id'], item['id'])

    def test_shared_delete_seen_by_other_cache_and_offline_delete_refused(self):
        client = DesktopSession(self.store.root, self.store.store_id, cache_home=self.root / 'pc')
        client.load()
        with patch.object(client, 'remote', side_effect=OSError('offline')):
            with self.assertRaises(OSError):
                client.set_visibility(self.sample, 'zz00001', True, '理由')
        deleted = client.set_visibility(self.sample, 'zz00001', True, '理由')
        other = DesktopSession(self.store.root, self.store.store_id, cache_home=self.root / 'other-pc')
        self.assertEqual(other.load().of_kind('sample'), [])
        other.set_visibility(deleted, 'zz00001', False, '復元')
        self.assertEqual(len(client.load().of_kind('sample')), 1)

    def test_concurrent_delete_and_edit_are_hidden_and_restore_is_blocked(self):
        original = self.store.load()
        deleted = self.store.set_visibility(self.sample, 'zz00001', True, '誤登録')
        with patch.object(self.store, 'load', return_value=original):
            self.store.save('sample', {**self.sample['data'], 'notes': '同時訂正'}, 'zz00001', record_id=self.sample['id'], base_revision=self.sample['revision_id'])
        snapshot = self.store.load()
        self.assertEqual(len(snapshot.heads[self.sample['id']]), 2)
        self.assertEqual(snapshot.of_kind('sample'), [])
        with self.assertRaises(ConflictError):
            self.store.set_visibility(deleted, 'zz00001', False, '復元')

    def test_deleted_measurement_text_no_longer_matches_search(self):
        measurement = self.store.save('measurement', {**self.measurement['data'], 'notes': 'DELETED-SEARCH-ONLY'}, 'zz00002', record_id=self.measurement['id'], base_revision=self.measurement['revision_id'])
        client = DesktopSession(self.store.root, self.store.store_id, cache_home=self.root / 'pc')
        client.load()
        self.assertTrue(client.search_samples('DELETED-SEARCH-ONLY'))
        client.set_visibility(measurement, 'zz00002', True, '理由')
        self.assertEqual(client.search_samples('DELETED-SEARCH-ONLY'), set())
