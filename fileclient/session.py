"""Local first new registrations; explicit idempotent delivery to one shared store.

Only immutable metadata is cached from other PCs. Large attachments remain on
the share until requested. No credentials, automatic deletes, or record merges.
"""
import hashlib
import json
import os
import sqlite3
from pathlib import Path

from .runtime import profile_directory
from .catalog import Catalog
from .index import RecordIndex
from .store import (CommitUnverifiedError, FileStore, StoreError, Snapshot, export_pending_path, identifier,
                    normalized, publish_export, read_json, uid)


def create_cache(directory):
    if directory.exists():
        return FileStore(directory)
    staging = directory.with_name('.' + identifier() + '.initializing')
    FileStore.create(staging)
    try:
        os.rename(staging, directory)
    except FileExistsError:
        pass
    return FileStore(directory)


def digest_file(path, maximum):
    digest, size = hashlib.sha256(), 0
    with path.open('rb') as stream:
        while chunk := stream.read(1024 * 1024):
            size += len(chunk)
            if size > maximum:
                raise StoreError('転送ファイルのサイズが一致しません。')
            digest.update(chunk)
    return size, digest.hexdigest()


def sample_key(item):
    """Duplicate-check key for a sample, normalized as FileStore.save does."""
    return normalized(item['data']['creator']), normalized(item['data']['label'])


def install_bytes(destination, raw):
    """Create exclusively; a retry accepts only exactly identical bytes."""
    if destination.exists():
        with destination.open('rb') as stream:
            if stream.read(len(raw) + 1) != raw:
                raise StoreError('既存の記録と内容が異なるため上書きしません。')
        return
    pending = export_pending_path(destination)
    with pending.open('xb') as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    try:
        publish_export(pending, destination)
    except FileExistsError:
        with destination.open('rb') as stream:
            if stream.read(len(raw) + 1) != raw:
                raise StoreError('同時保存された記録と内容が異なります。')


def transfer(source, target, item, *, blobs, attachment_ids=None):
    """Publish commit last. Reuse fixed UUIDs, resume without replacing files."""
    if not blobs:
        try:
            if target.read_revision(item['id'], item['revision_id']) == item:
                return
        except FileNotFoundError:
            pass
    verified = source.read_revision(item['id'], item['revision_id'])
    source_dir = source._revision_dir(item['id'], item['revision_id'])
    destination = target._revision_dir(item['id'], item['revision_id'])
    destination.mkdir(parents=True, exist_ok=True)
    _, raw = read_json(source_dir / 'document.json')
    commit, _ = read_json(source_dir / 'commit.json')
    if commit.get('sha256') != hashlib.sha256(raw).hexdigest():
        raise StoreError('読み取り中に記録が変化したため転送を保留しました。')
    if (destination / 'commit.json').exists():
        target.read_revision(item['id'], item['revision_id'])
        if (destination / 'document.json').read_bytes() != raw:
            raise StoreError('同じ内部IDの内容が異なります。送信を保留しました。')
    if blobs:
        for attachment in verified['attachments']:
            if attachment_ids is not None and attachment['id'] not in attachment_ids:
                continue
            blob = target._revision_dir(item['id'], attachment['event_id']) / 'files' / attachment['id']
            blob.parent.mkdir(parents=True, exist_ok=True)
            expected = (attachment['size'], attachment['sha256'])
            if not blob.exists():
                incoming = source._revision_dir(item['id'], attachment['event_id']) / 'files' / attachment['id']
                pending = export_pending_path(blob)
                with incoming.open('rb') as reader, pending.open('xb') as writer:
                    size, digest = 0, hashlib.sha256()
                    while chunk := reader.read(1024 * 1024):
                        size += len(chunk)
                        if size > attachment['size']:
                            raise StoreError('添付サイズが一致しません。未送信のまま保留します。')
                        digest.update(chunk)
                        writer.write(chunk)
                    writer.flush()
                    os.fsync(writer.fileno())
                if (size, digest.hexdigest()) != expected:
                    raise StoreError('添付ハッシュが一致しません。未送信のまま保留します。')
                try:
                    publish_export(pending, blob)
                except FileExistsError:
                    pass
            if digest_file(blob, attachment['size']) != expected:
                raise StoreError('送信先の添付が一致しません。上書きせず保留します。')
    install_bytes(destination / 'document.json', raw)
    install_bytes(destination / 'commit.json', json.dumps(
        {'sha256': hashlib.sha256(raw).hexdigest()}).encode('ascii'))
    target.read_revision(item['id'], item['revision_id'])


class DesktopSession:
    def __init__(self, root, store_id, *, cache_home=None):
        self.root, self.store_id = Path(root), uid(store_id)
        home = Path(cache_home) if cache_home is not None else profile_directory() / 'stores'
        home.mkdir(parents=True, exist_ok=True)
        directory = home / self.store_id
        directory.mkdir(exist_ok=True)
        self.local = create_cache(directory / 'outbox')
        self.mirror = create_cache(directory / 'mirror')
        self.receipts = directory / 'sent'
        self.receipts.mkdir(exist_ok=True)
        self.index = RecordIndex(directory / 'search.sqlite', {'outbox': self.local, 'mirror': self.mirror})
        self.index.reconcile_acknowledgments(self.receipts)
        self.online = False
        self.catalog = Catalog()
        self.blocked = set()
        self.connection_message = '接続未確認（PC内の記録）'
        self.notices = []

    @classmethod
    def connect(cls, store):
        return cls(store.root, store.store_id)

    def remote(self):
        store = FileStore(self.root)
        if store.store_id != self.store_id:
            raise StoreError('接続先のデータIDが前回と異なります。送信しません。')
        return store

    def pending(self, *, include_deleted=False):
        result = []
        for item, digest, acknowledged in self.index.outgoing():
            if item.get('origin') != {'store_id': self.store_id, 'client_id': self.local.store_id}:
                continue
            expected = self.receipt_digest(item, digest)
            if acknowledged is not None:
                if acknowledged != expected:
                    raise StoreError('送信確認票と記録が一致しません。確認が必要です。'
                        f'記録ID: {item["id"]} / 改訂ID: {item["revision_id"]}')
                continue
            receipt = self.receipts / (item['revision_id'] + '.json')
            if receipt.exists():
                self.index.acknowledge(receipt, expected)
            else:
                result.append(item)
        if not include_deleted:
            snapshot = self.cached()
            shared_ids = self.index.snapshot('mirror').heads
            result = [item for item in result if item['id'] in shared_ids or not snapshot.hidden(item)]
        return sorted(result, key=lambda i: (i['kind'] != 'sample', i['created_at'], i['id']))

    def receipt(self, item, raw):
        return self.receipt_digest(item, hashlib.sha256(raw).hexdigest())

    def receipt_digest(self, item, digest):
        return {'store_id': self.store_id, 'record_id': item['id'],
                'revision_id': item['revision_id'], 'sha256': digest}

    def cached(self):
        snapshot = self.index.snapshot()
        if self.index.rebuilt:
            snapshot.warnings.append('検索データをJSON原本から再構築しました。元の検索ファイルは.unusable名で保持しています。')
        snapshot.blocked.update(self.blocked)
        return snapshot

    def search_samples(self, query):
        return self.index.search_samples(query)

    def cache_shared(self, remote, item):
        """A mirror revision must retain its complete ancestry, even offline."""
        known = self.index.record_revisions('mirror', item['id'])
        chain, current, seen = [item], item, {item['revision_id']}
        while current['parent_revision'] is not None and current['parent_revision'] not in known:
            parent_id = current['parent_revision']
            if parent_id in seen:
                raise StoreError('改訂履歴に循環があるため、取得を保留します。')
            parent = remote.read_revision(item['id'], parent_id)
            if parent['kind'] != item['kind'] or parent['registrant'] != item['registrant']:
                raise StoreError('改訂履歴の種別・登録者が一致しません。')
            seen.add(parent_id)
            chain.append(parent)
            current = parent
        for revision in reversed(chain):
            transfer(remote, self.mirror, revision, blobs=False)
            self.index.record('mirror', revision)

    def rebuild_index(self):
        index = RecordIndex(self.index.path, self.index.stores)
        index.rebuild()
        index.reconcile_acknowledgments(self.receipts)
        self.index = index

    def load(self):
        self.index.refresh('outbox')
        self.index.refresh('mirror')
        self.index.reconcile_acknowledgments(self.receipts)
        try:
            remote = self.remote()
            shared = remote.load()
        except (OSError, StoreError, ValueError) as exc:
            self.online = False
            self.connection_message = 'オフライン／接続確認未完了'
            snapshot = self.cached()
            snapshot.warnings.append(f'共有先を確認できません。PC内の記録を表示しています。\n{exc}')
            return snapshot
        self.online = True
        self.connection_message = '今回の再読込で共有先を確認'
        warnings = []
        known = self.index.snapshot('mirror').revisions
        for revisions in shared.revisions.values():
            for item in revisions.values():
                if known.get(item['id'], {}).get(item['revision_id']) == item:
                    continue
                try:
                    self.cache_shared(remote, item)
                except (OSError, StoreError, ValueError, sqlite3.DatabaseError) as exc:
                    warnings.append(f'PC内への反映を保留 {item["id"]}: {exc}')
        self.blocked = set(shared.blocked)
        # Refresh statuses so a previously corrupt/repaired cache is re-evaluated.
        self.index.refresh('mirror')
        snapshot = self.cached()
        snapshot.warnings.extend(shared.warnings + warnings)
        try:
            pending_ids = {i['id'] for i in self.pending(include_deleted=True)}
        except (StoreError, sqlite3.DatabaseError, ValueError) as exc:
            pending_ids = set(self.index.snapshot('outbox').heads)
            snapshot.warnings.append(f'未送信の確認が必要です: {exc}')
        for record_id in snapshot.heads.keys() - shared.heads.keys() - pending_ids:
            snapshot.blocked.add(record_id)
            snapshot.warnings.append(f'共有先で確認できない記録（PC内に保持）: {record_id}')
        self.blocked = set(snapshot.blocked)
        return snapshot

    can_edit = staticmethod(FileStore.can_edit)

    def set_visibility(self, item, actor, deleted, reason, *, attachment_id=None):
        # Unsent records stay local; shared records require a fresh online base.
        if any(entry['id'] == item['id'] for entry in self.pending(include_deleted=True)):
            snapshot = self.cached()
            result = self.local.save(item['kind'], item['data'], actor,
                record_id=item['id'], base_revision=item['revision_id'],
                origin={'store_id': self.store_id, 'client_id': self.local.store_id},
                _snapshot=snapshot, _visibility=(deleted, reason, attachment_id))
            try:
                self.index.record('outbox', result)
            except (OSError, StoreError, sqlite3.DatabaseError) as exc:
                self.index.dirty.add('outbox')
                raise CommitUnverifiedError(result['id'], result['revision_id']) from exc
            return result
        remote = self.remote()
        result = remote.set_visibility(item, actor, deleted, reason, attachment_id=attachment_id)
        try:
            self.cache_shared(remote, result)
        except (OSError, StoreError, sqlite3.DatabaseError) as exc:
            self.notices.append(f'共有先にはごみ箱操作を保存済みです。再読込してください。\n{exc}')
        return result

    def save(self, kind, data, actor, **kwargs):
        previous = self.cached().current(kwargs['record_id'])['data'] if kwargs.get('record_id') else None
        data = self.catalog.validate(kind, data, previous)
        if kwargs.get('record_id') is not None:
            record_id = kwargs['record_id']
            if any(i['id'] == record_id for i in self.pending()):
                raise StoreError('未送信記録の訂正は、まとめて送信した後で行ってください。')
            remote = self.remote()  # Existing records are edited online only.
            item = remote.save(kind, data, actor, **kwargs)
            try:
                self.cache_shared(remote, item)
            except (OSError, StoreError, sqlite3.DatabaseError) as exc:
                self.notices.append(f'共有先には訂正保存済み。PC内への反映は「再読込」で確認してください。\n改訂ID: {item["revision_id"]}\n{exc}')
            return item
        if kind != 'sample' and data.get('sample_id') in self.blocked:
            raise StoreError('関連試料が要確認のため新規登録を保留します。')
        item = self.local.save(kind, data, actor, origin={
            'store_id': self.store_id, 'client_id': self.local.store_id}, _snapshot=self.cached(), **kwargs)
        try:
            self.index.record('outbox', item)
        except (OSError, StoreError, sqlite3.DatabaseError) as exc:
            self.index.dirty.add('outbox')
            raise CommitUnverifiedError(item['id'], item['revision_id']) from exc
        return item

    def read_revision(self, record_id, revision_id):
        try:
            item = self.local.read_revision(record_id, revision_id)
            self.index.record('outbox', item)
            return item
        except (OSError, StoreError):
            try:
                item = self.mirror.read_revision(record_id, revision_id)
                self.index.record('mirror', item)
                return item
            except (OSError, StoreError):
                pass
            remote = self.remote()
            item = remote.read_revision(record_id, revision_id)
            self.cache_shared(remote, item)
            return item

    def sync(self):
        remote = self.remote()
        sent, errors = 0, []
        try:
            queued = self.pending(include_deleted=True)
        except (StoreError, ValueError, sqlite3.DatabaseError) as exc:
            return 0, [f'未送信の確認が必要です: {exc}'], '確認必要'
        pending_ids = {i['id'] for i in queued}
        shared = remote.load()
        # Sending adds revisions to the share only, so the local snapshot stays
        # valid for the whole loop; rebuilding it per record re-read every
        # revision. The shared sample labels are indexed once for the same
        # reason, and newly sent samples are added below.
        local_snapshot = self.cached()
        shared_sample_ids = {}
        for sample in shared.of_kind('sample', include_deleted=True):
            shared_sample_ids.setdefault(sample_key(sample), set()).add(sample['id'])
        for item in queued:
            try:
                if local_snapshot.hidden(item) and item['id'] not in shared.heads:
                    continue  # Never upload a discarded, never-shared registration.
                if item['kind'] == 'sample':
                    if shared_sample_ids.get(sample_key(item), set()) - {item['id']}:
                        raise StoreError('同じ作製者の試料IDが共有先にあります。自動統合せず保留します。')
                else:
                    if shared.current(item['data']['sample_id'], include_deleted=local_snapshot.status(item)['state'] == 'deleted')['kind'] != 'sample':
                        raise StoreError('関連試料が共有先にありません。')
                    if item['data']['sample_id'] in pending_ids:
                        raise StoreError('関連試料の送信がまだ完了していません。')
                if item['id'] in shared.blocked:
                    raise StoreError('共有先の記録が要確認のため送信を保留します。')
                transfer(self.local, remote, item, blobs=True)
                _, raw = read_json(self.local._revision_dir(item['id'], item['revision_id']) / 'document.json')
                install_bytes(self.receipts / (item['revision_id'] + '.json'), json.dumps(
                    self.receipt(item, raw), sort_keys=True).encode('ascii'))
                self.index.acknowledge(self.receipts / (item['revision_id'] + '.json'), self.receipt(item, raw))
                sent += 1
                pending_ids.discard(item['id'])
                revisions = shared.revisions.setdefault(item['id'], {})
                revisions[item['revision_id']] = item
                # 共有先に別の枝があればそれも head のまま残す。単一化すると、
                # この sync の残りで競合に気づけなくなる。
                parents = {entry['parent_revision'] for entry in revisions.values()}
                shared.heads[item['id']] = [entry for key, entry in revisions.items()
                                            if key not in parents]
                if item['kind'] == 'sample':
                    shared_sample_ids.setdefault(sample_key(item), set()).add(item['id'])
            except (OSError, StoreError, ValueError, sqlite3.DatabaseError) as exc:
                errors.append(f'{item["kind"]} / {item["id"]}: {exc}')
        try:
            remaining = len(self.pending())
        except (StoreError, ValueError, sqlite3.DatabaseError) as exc:
            remaining = '確認必要'
            errors.append(f'残りの未送信数を確認できません: {exc}')
        return sent, errors, remaining

    def export_attachment(self, item, attachment_id, destination):
        try:
            verified = self.local.read_revision(item['id'], item['revision_id'])
            store = self.local
        except FileNotFoundError:
            store = self.mirror
            verified = store.read_revision(item['id'], item['revision_id'])
        matches = [a for a in verified['attachments'] if a['id'] == attachment_id]
        if len(matches) != 1:
            raise StoreError('指定された添付が見つかりません。')
        attachment = matches[0]
        blob = store._revision_dir(item['id'], attachment['event_id']) / 'files' / attachment_id
        if not blob.exists():
            try:
                transfer(self.remote(), store, item, blobs=True, attachment_ids={attachment_id})
            except (OSError, StoreError) as exc:
                raise StoreError('この添付はPCに未取得です。共有先へ接続してから保存してください。') from exc
        return store.export_attachment(item, attachment_id, destination)
