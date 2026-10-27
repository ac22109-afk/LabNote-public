import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from .session import DesktopSession, transfer
from .store import FileStore, StoreError, registrant_id, publish_export, fs_path


class SessionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='labnote-offline-test-')
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        temporary.name = str(fs_path(self.base))
        self.remote = FileStore.create(self.base / 'shared')
        self.session = DesktopSession(self.remote.root, self.remote.store_id, cache_home=self.base / 'pc1')

    def sample(self, session=None, label='S-001', files=()):
        return (session or self.session).save('sample',
            {'label': label, 'creator': 'zz00001', 'material': 'アルミニウム合金', 'alloy': 'A1050'}, ' ＺＺ００００１ ', files=files)

    def test_id_normalizes_case_width_and_outer_spaces_any_prefix(self):
        for raw, expected in [(' ＺＺ００００１ ', 'zz00001'), ('ZZ00002', 'zz00002'), ('zz00000', 'zz00000')]:
            self.assertEqual(registrant_id(raw), expected)
        for raw in ['', '例：zz00001', 'ac0000', 'zz000019', 'a00001', 'abc00001', 'ac-00001',
                    'zz 00001', '山田太郎', 'zz٠٠٠٠١', 'zz00001\nzz00002']:
            with self.subTest(raw=raw), self.assertRaises(StoreError):
                registrant_id(raw)

    def test_invalid_id_cannot_create_record_or_queue(self):
        with self.assertRaises(StoreError):
            self.session.save('sample', {'label': 'S', 'creator': 'a', 'material': 'アルミニウム合金', 'alloy': 'A1050'}, '山田')
        self.assertEqual(list((self.session.local.root / 'records').iterdir()), [])

    def test_offline_save_restart_and_dependency_order(self):
        with patch.object(self.session, 'remote', side_effect=OSError('offline')):
            self.assertEqual(len(self.session.load().heads), 0)
            sample = self.sample()
            measurement = self.session.save('measurement', {'sample_id': sample['id'], 'measurer': 'zz00001',
                'method': '画像観察', 'measured_on': '2026-10-19', 'values': []}, 'zz00002')
        restarted = DesktopSession(self.remote.root, self.remote.store_id, cache_home=self.base / 'pc1')
        self.assertEqual(len(restarted.pending()), 2)
        self.assertEqual(len(self.remote.load().heads), 0)
        self.assertEqual(restarted.sync(), (2, [], 0))
        self.assertEqual(self.remote.load().current(measurement['id'])['data']['sample_id'], sample['id'])
        self.assertEqual(restarted.sync(), (0, [], 0))

    def test_lost_ack_retries_same_uuid_without_duplicate(self):
        sample = self.sample()
        # Simulate the server accepted the exact revision before receipt write.
        transfer(self.session.local, self.remote, sample, blobs=True)
        self.assertEqual(len(self.session.pending()), 1)
        self.assertEqual(self.session.sync(), (1, [], 0))
        self.assertEqual(len(self.remote.load().heads), 1)
        self.assertEqual(len(self.remote.load().revisions[sample['id']]), 1)

    def test_failure_after_remote_commit_retains_queue(self):
        self.sample()
        from .session import install_bytes
        def fail_receipt(path, raw):
            if path.parent == self.session.receipts:
                raise OSError('local disk unavailable')
            return install_bytes(path, raw)
        with patch('fileclient.session.install_bytes', side_effect=fail_receipt):
            sent, errors, remaining = self.session.sync()
        self.assertEqual((sent, remaining), (0, 1))
        self.assertTrue(errors)
        self.assertEqual(len(self.remote.load().heads), 1)
        self.assertEqual(self.session.sync(), (1, [], 0))

    def test_interrupted_blob_copy_retry_and_integrity(self):
        source = self.base / 'instrument.bin'
        source.write_bytes(b'research' * 500000)
        sample = self.sample(files=[source])
        from .session import publish_export
        calls = []
        def interrupt(pending, final):
            calls.append(final)
            if final.parent.name == 'files':
                raise OSError('connection lost')
            return publish_export(pending, final)
        with patch('fileclient.session.publish_export', side_effect=interrupt):
            sent, errors, remaining = self.session.sync()
        self.assertEqual((sent, remaining), (0, 1))
        self.assertTrue(errors)
        self.assertEqual(len(self.remote.load().heads), 0)
        self.assertEqual(self.session.sync(), (1, [], 0))
        exported = self.base / 'copy.bin'
        self.remote.export_attachment(sample, sample['attachments'][0]['id'], exported)
        self.assertEqual(hashlib.sha256(exported.read_bytes()).digest(), hashlib.sha256(source.read_bytes()).digest())

    def test_wrong_target_never_receives_queue(self):
        self.sample()
        another = FileStore.create(self.base / 'other')
        self.session.root = another.root
        with self.assertRaises(StoreError):
            self.session.sync()
        self.assertEqual(len(another.load().heads), 0)
        self.assertEqual(len(self.session.pending()), 1)

    def test_second_pc_downloads_metadata_and_attachment_on_demand(self):
        source = self.base / 'image.png'
        source.write_bytes(b'synthetic image')
        sample = self.sample(files=[source])
        self.session.sync()
        pc2 = DesktopSession(self.remote.root, self.remote.store_id, cache_home=self.base / 'pc2')
        self.assertEqual(len(pc2.load().heads), 1)
        self.assertEqual(pc2.pending(), [])
        blob = pc2.local._revision_dir(sample['id'], sample['revision_id']) / 'files' / sample['attachments'][0]['id']
        self.assertFalse(blob.exists())
        with patch.object(pc2, 'remote', side_effect=OSError('offline')):
            with self.assertRaises(StoreError):
                pc2.export_attachment(sample, sample['attachments'][0]['id'], self.base / 'offline.png')
        pc2.export_attachment(sample, sample['attachments'][0]['id'], self.base / 'online.png')
        self.assertEqual((self.base / 'online.png').read_bytes(), source.read_bytes())

    def test_offline_existing_edit_is_rejected_pending_new_still_allowed(self):
        sample = self.sample()
        with self.assertRaises(StoreError):
            self.session.save('sample', sample['data'], 'zz00001', record_id=sample['id'], base_revision=sample['revision_id'])
        self.session.sync()
        with patch.object(self.session, 'remote', side_effect=OSError('offline')):
            with self.assertRaises(OSError):
                self.session.save('sample', sample['data'], 'zz00001', record_id=sample['id'], base_revision=sample['revision_id'])
            self.sample(label='S-002')
        self.assertEqual(len(self.session.pending()), 1)

    def test_duplicate_remote_label_keeps_parent_and_child_unsent(self):
        sample = self.sample()
        self.session.save('treatment', {'sample_id': sample['id'], 'method': '焼鈍'}, 'zz00001')
        self.remote.save('sample', sample['data'], 'zz00002')
        sent, errors, remaining = self.session.sync()
        self.assertEqual((sent, remaining), (0, 2))
        self.assertEqual(len(errors), 2)
        self.assertEqual(len(self.remote.load().heads), 1)

    def test_corrupt_attachment_is_not_published_or_overwritten(self):
        source = self.base / 'data.bin'
        source.write_bytes(b'correct')
        sample = self.sample(files=[source])
        attachment = sample['attachments'][0]
        directory = self.remote._revision_dir(sample['id'], sample['revision_id'])
        (directory / 'files').mkdir(parents=True)
        blob = directory / 'files' / attachment['id']
        blob.write_bytes(b'incorrect')
        sent, errors, remaining = self.session.sync()
        self.assertEqual((sent, remaining), (0, 1))
        self.assertTrue(errors)
        self.assertEqual(blob.read_bytes(), b'incorrect')
        self.assertFalse((directory / 'commit.json').exists())

    def test_legacy_actor_is_readable_without_rewriting(self):
        sample = self.sample()
        directory = self.session.local._revision_dir(sample['id'], sample['revision_id'])
        item = json.loads((directory / 'document.json').read_text(encoding='utf-8'))
        item['actor'] = item['registrant'] = '旧表示名'
        raw = json.dumps(item, ensure_ascii=False).encode('utf-8')
        (directory / 'document.json').write_bytes(raw)
        (directory / 'commit.json').write_text(json.dumps({'sha256': hashlib.sha256(raw).hexdigest()}))
        self.assertEqual(self.session.local.load().current(sample['id'])['registrant'], '旧表示名')

    def test_corrupt_mirror_does_not_stop_other_outbox_records(self):
        sample = self.sample()
        self.session.sync()
        self.session.load()
        directory = self.session.mirror._revision_dir(sample['id'], sample['revision_id'])
        (directory / 'document.json').write_bytes(b'broken cache')
        second = self.sample(label='S-002')
        snapshot = self.session.load()
        self.assertTrue(self.session.online)
        self.assertIn(sample['id'], snapshot.blocked)
        self.assertIn(second['id'], snapshot.heads)
        self.assertEqual(self.session.sync(), (1, [], 0))

    def test_missing_shared_parent_is_blocked_for_new_child(self):
        sample = self.sample()
        self.session.sync()
        # Simulate an empty server listing without deleting user data.
        from .store import Snapshot
        original_load = FileStore.load
        def load(store, **kwargs):
            return Snapshot() if store.root == self.remote.root else original_load(store, **kwargs)
        with patch.object(FileStore, 'load', load):
            snapshot = self.session.load()
        self.assertIn(sample['id'], snapshot.blocked)
        with self.assertRaises(StoreError):
            self.session.save('treatment', {'sample_id': sample['id'], 'method': '焼鈍'}, 'zz00001')

    def test_remote_edit_success_mirror_failure_reports_success_notice(self):
        sample = self.sample()
        self.session.sync()
        with patch('fileclient.session.transfer', side_effect=OSError('cache write failed')):
            revised = self.session.save('sample', {**sample['data'], 'notes': 'updated'}, 'zz00001',
                record_id=sample['id'], base_revision=sample['revision_id'])
        self.assertEqual(self.remote.load().current(sample['id'])['revision_id'], revised['revision_id'])
        self.assertTrue(self.session.notices)

    def test_mac_publish_uses_exclusive_rename_not_hardlinks(self):
        with patch('fileclient.store.os.name', 'posix'), patch('fileclient.store.sys.platform', 'darwin'), \
                patch('fileclient.store.darwin_rename_exclusive') as rename, patch('fileclient.store.os.link') as link:
            publish_export('staged', 'final')
            rename.assert_called_once_with('staged', 'final')
            link.assert_not_called()

    def test_cache_initialization_interruption_is_retryable(self):
        pc = self.base / 'interrupted-pc'
        with patch('fileclient.session.os.rename', side_effect=OSError('interrupted')):
            with self.assertRaises(OSError):
                DesktopSession(self.remote.root, self.remote.store_id, cache_home=pc)
        session = DesktopSession(self.remote.root, self.remote.store_id, cache_home=pc)
        self.assertEqual(session.pending(), [])

    @unittest.skipUnless(os.name == 'nt', 'Windows extended-path I/O')
    def test_long_windows_record_paths_keep_attachments(self):
        store = FileStore.create(self.base / ('long-profile-' + 'x' * 100))
        source = self.base / 'source.bin'
        source.write_bytes(b'long path fixture')
        item = store.save('sample', {'label': 'LONG', 'creator': 'zz00001', 'material': 'アルミニウム合金', 'alloy': 'A1050'}, 'zz00001', files=[source])
        attachment = item['attachments'][0]
        self.assertGreater(len(str(store._revision_dir(item['id'], item['revision_id']) / 'files' / attachment['id'])), 260)
        self.assertEqual(len(store.load().heads), 1)
        store.export_attachment(item, attachment['id'], self.base / 'long-copy.bin')
        self.assertEqual((self.base / 'long-copy.bin').read_bytes(), source.read_bytes())

    def test_receipt_mismatch_names_the_record_and_revision(self):
        sample = self.sample()
        self.assertEqual(self.session.sync(), (1, [], 0))
        # Corrupt only the stored confirmation. The receipt file keeps its
        # fingerprint, so reconciliation cannot quietly drop the row.
        with self.session.index.connection() as db:
            stored = db.execute('SELECT document FROM acknowledgments WHERE revision_id=?',
                                (sample['revision_id'],)).fetchone()[0]
            wrong = dict(json.loads(stored), sha256='0' * 64)
            db.execute('UPDATE acknowledgments SET document=? WHERE revision_id=?',
                       (json.dumps(wrong, sort_keys=True), sample['revision_id']))
        with self.assertRaises(StoreError) as raised:
            self.session.pending()
        for named in [sample['id'], sample['revision_id']]:
            self.assertIn(named, str(raised.exception))
        sent, errors, remaining = self.session.sync()
        self.assertEqual((sent, remaining), (0, '確認必要'))
        self.assertTrue(any(sample['revision_id'] in error for error in errors))

    def test_mixed_queue_sync_result_and_repeat_are_stable(self):
        sample = self.sample(label='S-001')
        self.session.save('measurement', {'sample_id': sample['id'], 'measurer': 'zz00001',
            'method': '画像観察', 'measured_on': '2026-10-19', 'values': []}, 'zz00002')
        cancelled = self.sample(label='S-002')
        self.session.set_visibility(cancelled, 'zz00001', True, '送信前に取消')
        duplicate = self.sample(label='S-003')
        self.remote.save('sample', duplicate['data'], 'zz00002')
        self.assertEqual(len(self.session.pending(include_deleted=True)), 5)
        sent, errors, remaining = self.session.sync()
        self.assertEqual((sent, remaining), (2, 1))
        self.assertEqual(len(errors), 1)
        self.assertIn(duplicate['id'], errors[0])
        shared = self.remote.load()
        self.assertEqual(len(shared.heads), 3)
        self.assertNotIn(cancelled['id'], shared.heads)
        self.assertNotIn(duplicate['id'], shared.heads)
        self.assertEqual(self.session.sync(), (0, errors, 1))

    def test_sending_keeps_another_branch_visible_on_the_share(self):
        first = self.sample()
        self.session.sync()
        second = self.sample(label='S-002')
        shared = self.remote.load()
        head = shared.heads[first['id']][0]
        stray = dict(head, revision_id='9' * 8 + '-' + '0' * 4 + '-' + '0' * 4 + '-' + '0' * 4 + '-' + '0' * 12,
                     id=second['id'], parent_revision=None)
        shared.revisions.setdefault(second['id'], {})[stray['revision_id']] = stray
        shared.heads[second['id']] = [stray]
        with patch.object(FileStore, 'load', return_value=shared):
            self.session.sync()
        # 送信した改訂で単一化せず、既存の枝も head のまま残ること。
        self.assertIn(stray['revision_id'],
                      {entry['revision_id'] for entry in shared.heads[second['id']]})
        self.assertEqual(len(shared.heads[second['id']]), 2)

    def test_only_one_of_two_same_label_queued_samples_reaches_the_share(self):
        from .store import Snapshot
        first = self.sample(label='S-001')
        # A duplicate reaches the queue only when the local index missed the
        # first one; the share must still accept just one of the two.
        with patch.object(self.session, 'cached', return_value=Snapshot()):
            second = self.sample(label='Ｓ－００１')
        sent, errors, remaining = self.session.sync()
        self.assertEqual((sent, remaining), (1, 1))
        self.assertEqual(len(errors), 1)
        shared = self.remote.load()
        self.assertEqual(len(shared.heads), 1)
        unsent = {first['id'], second['id']} - shared.heads.keys()
        self.assertEqual(len(unsent), 1)
        self.assertIn(unsent.pop(), errors[0])


if __name__ == '__main__':
    unittest.main()
