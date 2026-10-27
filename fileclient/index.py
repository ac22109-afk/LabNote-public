"""Disposable SQLite index on the local PC. JSON and attachments remain primary.

Only explicit reconciliation stats JSON files. Cached views/searches never scan
the share. Corrupt indexes are retained under another name and rebuilt.
"""
import json
import os
import sqlite3
from contextlib import contextmanager
from functools import wraps
from pathlib import Path

from .store import CommitUnverifiedError, Snapshot, StoreError, identifier, normalized, read_json


class IndexInvalid(Exception):
    pass


def recover_query(method):
    @wraps(method)
    def call(self, *args, **kwargs):
        try:
            return method(self, *args, **kwargs)
        except (sqlite3.DatabaseError, json.JSONDecodeError) as exc:
            if isinstance(exc, sqlite3.DatabaseError) and (getattr(exc, 'sqlite_errorcode', 0) & 0xFF) not in {
                    sqlite3.SQLITE_CORRUPT, sqlite3.SQLITE_NOTADB}:
                raise
            self.failed = True
            self.ensure_clean()
            return method(self, *args, **kwargs)
    return call


class RecordIndex:
    VERSION = '2'

    def __init__(self, path, stores):
        self.path, self.stores = Path(path), dict(stores)
        if str(self.path).startswith('\\\\'):
            raise StoreError('検索データはPC内に置いてください。共有フォルダへは配置できません。')
        if os.name == 'nt':
            import ctypes
            if ctypes.windll.kernel32.GetDriveTypeW(str(self.path.resolve().anchor)) == 4:
                raise StoreError('検索データをネットワークドライブへ置くことはできません。')
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.rebuilt = False
        self.failed = False
        self.dirty = set()
        self.generation = 0
        self.last_reads = {}
        try:
            self._initialize()
        except (sqlite3.DatabaseError, IndexInvalid) as exc:
            if isinstance(exc, sqlite3.DatabaseError) and (getattr(exc, 'sqlite_errorcode', 0) & 0xFF) not in {
                    sqlite3.SQLITE_CORRUPT, sqlite3.SQLITE_NOTADB}:
                raise
            retained = self.path.with_name(self.path.name + '.' + identifier() + '.unusable')
            if self.path.exists():
                os.rename(self.path, retained)
            for suffix in ['-journal', '-wal', '-shm']:
                companion = Path(str(self.path) + suffix)
                if companion.exists():
                    os.rename(companion, Path(str(retained) + suffix))
            self.rebuilt = True
            self.failed = False
            self._initialize()
        # Recover commits written before a crash between JSON and index update.
        for name in self.stores:
            self.refresh(name)

    @contextmanager
    def connection(self):
        connection = sqlite3.connect(self.path, timeout=5)
        try:
            yield connection
            connection.commit()
        except Exception as exc:
            if isinstance(exc, sqlite3.DatabaseError) and (getattr(exc, 'sqlite_errorcode', 0) & 0xFF) in {
                    sqlite3.SQLITE_CORRUPT, sqlite3.SQLITE_NOTADB}:
                self.failed = True
            try:
                connection.rollback()
            except sqlite3.DatabaseError:
                pass
            raise
        finally:
            connection.close()

    def ensure_clean(self):
        if self.failed:
            replacement = RecordIndex(self.path, self.stores)
            self.__dict__.update(replacement.__dict__)
            self.rebuilt = True
        for name in list(self.dirty):
            self.refresh(name)

    def _initialize(self):
        expected = json.dumps({name: store.store_id for name, store in self.stores.items()}, sort_keys=True)
        with self.connection() as db:
            if db.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                raise IndexInvalid('Index integrity check failed')
            db.executescript('''
                CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS sources(name TEXT PRIMARY KEY, warnings TEXT NOT NULL, blocked TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS revisions(
                    source TEXT NOT NULL, record_id TEXT NOT NULL, revision_id TEXT NOT NULL,
                    parent_revision TEXT, kind TEXT NOT NULL, sample_id TEXT,
                    search_text TEXT NOT NULL, document TEXT NOT NULL,
                    raw_sha TEXT NOT NULL, fingerprint TEXT NOT NULL,
                    PRIMARY KEY(source, record_id, revision_id));
                CREATE INDEX IF NOT EXISTS revision_parent ON revisions(record_id, parent_revision);
                CREATE INDEX IF NOT EXISTS related_sample ON revisions(sample_id);
                CREATE INDEX IF NOT EXISTS revision_kind ON revisions(kind);
                CREATE TABLE IF NOT EXISTS acknowledgments(
                    revision_id TEXT PRIMARY KEY, document TEXT NOT NULL, fingerprint TEXT NOT NULL);
            ''')
            meta = dict(db.execute('SELECT key,value FROM meta'))
            if meta and (meta.get('version') != self.VERSION or meta.get('sources') != expected):
                raise IndexInvalid('Index version or source changed')
            db.executemany('INSERT OR IGNORE INTO meta VALUES (?,?)', [('version', self.VERSION), ('sources', expected)])

    @staticmethod
    def fingerprint(store, record_id, revision_id):
        directory = store._revision_dir(record_id, revision_id)
        result = []
        for name in ['commit.json', 'document.json']:
            info = (directory / name).stat()
            result.extend([info.st_size, info.st_mtime_ns, info.st_ctime_ns])
        return json.dumps(result)

    @staticmethod
    def row(name, item, sha, fingerprint):
        searchable = normalized(json.dumps([item['data'], item['id'], item['registrant']], ensure_ascii=False))
        return (name, item['id'], item['revision_id'], item['parent_revision'], item['kind'],
                item['data'].get('sample_id'), searchable,
                json.dumps(item, ensure_ascii=False, sort_keys=True), sha, fingerprint)

    def refresh(self, name, *, force=False):
        if self.failed:
            self.ensure_clean()
        store = self.stores[name]
        with self.connection() as db:
            known = {(record_id, revision_id): (document, sha, fingerprint) for record_id, revision_id, document, sha, fingerprint
                     in db.execute('SELECT record_id,revision_id,document,raw_sha,fingerprint FROM revisions WHERE source=?', (name,))}
        rows, reads = [], 0

        def read(record_id, revision_id):
            nonlocal reads
            fingerprint = self.fingerprint(store, record_id, revision_id)
            cached = known.get((record_id, revision_id))
            if force:
                cached = None
            if cached is not None and cached[2] == fingerprint:
                try:
                    item, sha = json.loads(cached[0]), cached[1]
                    if item.get('id') != record_id or item.get('revision_id') != revision_id:
                        cached = None
                except (ValueError, AttributeError):
                    cached = None
            if cached is None or cached[2] != fingerprint:
                item = store.read_revision(record_id, revision_id)
                commit, _ = read_json(store._revision_dir(record_id, revision_id) / 'commit.json')
                sha = commit['sha256']
                reads += 1
                if self.fingerprint(store, record_id, revision_id) != fingerprint:
                    raise StoreError('読取中に記録が変化しました。再読込してください。')
            rows.append(self.row(name, item, sha, fingerprint))
            return item

        snapshot = store.load(_reader=read)
        # Keep only revisions accepted with a complete, consistent ancestry.
        accepted = {(record_id, revision_id) for record_id, revisions in snapshot.revisions.items() for revision_id in revisions}
        rows = [row for row in rows if (row[1], row[2]) in accepted]
        with self.connection() as db:
            db.execute('DELETE FROM revisions WHERE source=?', (name,))
            db.executemany('INSERT INTO revisions VALUES (?,?,?,?,?,?,?,?,?,?)', rows)
            db.execute('INSERT OR REPLACE INTO sources VALUES (?,?,?)',
                       (name, json.dumps(snapshot.warnings, ensure_ascii=False), json.dumps(sorted(snapshot.blocked))))
        self.last_reads[name] = reads
        self.dirty.discard(name)
        self.generation += 1
        return snapshot

    def record(self, name, item):
        self.ensure_clean()
        self.dirty.add(name)
        store = self.stores[name]
        fingerprint = self.fingerprint(store, item['id'], item['revision_id'])
        validated = store.read_revision(item['id'], item['revision_id'])
        if validated != item or self.fingerprint(store, item['id'], item['revision_id']) != fingerprint:
            raise StoreError('読取中に記録が変化したため、検索データへの反映を保留しました。')
        if item['parent_revision'] is not None:
            with self.connection() as db:
                parent = db.execute('SELECT document,fingerprint FROM revisions WHERE source=? AND record_id=? AND revision_id=?',
                    (name, item['id'], item['parent_revision'])).fetchone()
            if parent is not None:
                try:
                    if self.fingerprint(store, item['id'], item['parent_revision']) != parent[1]:
                        parent = None
                except OSError:
                    parent = None
            if parent is None:
                accepted = self.refresh(name)
                if item['revision_id'] not in accepted.revisions.get(item['id'], {}):
                    raise StoreError('元の改訂が見つからないため、検索データへの反映を保留しました。')
                return
            parent = json.loads(parent[0])
            if item['kind'] != parent['kind'] or item['registrant'] != parent['registrant']:
                self.refresh(name)
                raise StoreError('元の改訂と記録の種類・登録者が一致しません。')
        commit, _ = read_json(store._revision_dir(item['id'], item['revision_id']) / 'commit.json')
        row = self.row(name, item, commit['sha256'], fingerprint)
        with self.connection() as db:
            old = db.execute('SELECT document FROM revisions WHERE source=? AND record_id=? AND revision_id=?', row[:3]).fetchone()
            if old is not None and old[0] != row[7]:
                raise StoreError('同じ改訂IDの索引内容が異なります。再読込が必要です。')
            db.execute('INSERT OR REPLACE INTO revisions VALUES (?,?,?,?,?,?,?,?,?,?)', row)
        self.generation += 1
        self.dirty.discard(name)

    @recover_query
    def snapshot(self, source=None):
        self.ensure_clean()
        snapshot = Snapshot()
        with self.connection() as db:
            statuses = db.execute('SELECT name,warnings,blocked FROM sources').fetchall()
            if source is None:
                rows = db.execute('SELECT record_id,revision_id,document FROM revisions').fetchall()
            else:
                rows = db.execute('SELECT record_id,revision_id,document FROM revisions WHERE source=?', (source,)).fetchall()
        for name, warnings, blocked in statuses:
            if source is None or name == source:
                snapshot.warnings.extend(w for w in json.loads(warnings) if not w.startswith('関連試料が見つかりません:'))
                snapshot.blocked.update(json.loads(blocked))
        for record_id, revision_id, document in rows:
            item = json.loads(document)
            revisions = snapshot.revisions.setdefault(record_id, {})
            if revision_id in revisions and revisions[revision_id] != item:
                snapshot.blocked.add(record_id)
                snapshot.warnings.append(f'同じ改訂IDの内容がPC内で異なります: {record_id}')
            revisions[revision_id] = item
        for record_id, revisions in snapshot.revisions.items():
            parents = {item['parent_revision'] for item in revisions.values()}
            snapshot.heads[record_id] = [item for key, item in revisions.items() if key not in parents]
            if len(snapshot.heads[record_id]) > 1:
                warning = f'訂正競合（自動上書きなし）: {record_id}'
                if warning not in snapshot.warnings:
                    snapshot.warnings.append(warning)
        if source is None:
            for kind in ['measurement', 'treatment']:
                for item in snapshot.of_kind(kind):
                    if not any(parent['kind'] == 'sample' for parent in snapshot.heads.get(item['data']['sample_id'], [])):
                        snapshot.warnings.append(f'関連試料が見つかりません: {item["id"]}')
        return snapshot

    def digest(self, source, item):
        with self.connection() as db:
            row = db.execute('SELECT raw_sha FROM revisions WHERE source=? AND record_id=? AND revision_id=?',
                             (source, item['id'], item['revision_id'])).fetchone()
        if row is None:
            raise StoreError('保存した記録を検索データに反映できませんでした。再読込してください。')
        return row[0]

    def record_revisions(self, source, record_id):
        self.ensure_clean()
        with self.connection() as db:
            rows = db.execute('SELECT revision_id,document FROM revisions WHERE source=? AND record_id=?',
                              (source, record_id)).fetchall()
        return {revision_id: json.loads(document) for revision_id, document in rows}

    def acknowledged(self, revision_id, expected):
        with self.connection() as db:
            row = db.execute('SELECT document FROM acknowledgments WHERE revision_id=?', (revision_id,)).fetchone()
        if row is None:
            return False
        if json.loads(row[0]) != expected:
            raise StoreError('送信確認票と検索データが一致しません。再読込してください。')
        return True

    def acknowledge(self, path, expected):
        saved, _ = read_json(path)
        if saved != expected:
            raise StoreError('送信確認票が一致しません。確認が必要です。')
        info = path.stat()
        with self.connection() as db:
            db.execute('INSERT OR REPLACE INTO acknowledgments VALUES (?,?,?)',
                       (expected['revision_id'], json.dumps(saved, sort_keys=True), json.dumps([info.st_size, info.st_mtime_ns, info.st_ctime_ns])))

    def reconcile_acknowledgments(self, receipts):
        # Confirmation files, like JSON, are authoritative over this local cache.
        with self.connection() as db:
            rows = db.execute('SELECT revision_id,document,fingerprint FROM acknowledgments').fetchall()
        for revision_id, document, fingerprint in rows:
            path = receipts / (revision_id + '.json')
            try:
                info = path.stat()
                if fingerprint == json.dumps([info.st_size, info.st_mtime_ns, info.st_ctime_ns]):
                    continue
                self.acknowledge(path, json.loads(document))
            except (OSError, StoreError, ValueError):
                with self.connection() as db:
                    db.execute('DELETE FROM acknowledgments WHERE revision_id=?', (revision_id,))

    @recover_query
    def outgoing(self):
        self.ensure_clean()
        with self.connection() as db:
            state = db.execute('SELECT blocked FROM sources WHERE name=?', ('outbox',)).fetchone()
            if state is not None and json.loads(state[0]):
                raise StoreError('PC内に読み取れない改訂があるため未送信数を確定できません。保存状態・競合を確認してください。')
            rows = db.execute('''SELECT r.document,r.raw_sha,a.document FROM revisions r
                LEFT JOIN acknowledgments a ON a.revision_id=r.revision_id WHERE r.source='outbox' ''').fetchall()
        return [(json.loads(document), digest, json.loads(receipt) if receipt is not None else None)
                for document, digest, receipt in rows]

    def rebuild(self):
        for name in self.stores:
            self.refresh(name, force=True)
        self.rebuilt = True

    @recover_query
    def search_samples(self, query):
        self.ensure_clean()
        query = normalized(query)
        with self.connection() as db:
            rows = db.execute('''
                SELECT DISTINCT s.record_id,s.revision_id FROM revisions s
                WHERE s.kind='sample' AND NOT EXISTS(
                    SELECT 1 FROM revisions child WHERE child.record_id=s.record_id AND child.parent_revision=s.revision_id)
                AND (?='' OR instr(s.search_text,?)>0 OR EXISTS(
                    SELECT 1 FROM revisions related WHERE related.sample_id=s.record_id
                    AND NOT EXISTS(SELECT 1 FROM revisions child WHERE child.record_id=related.record_id AND child.parent_revision=related.revision_id)
                    AND instr(related.search_text,?)>0))
            ''', (query, query, query)).fetchall()
            has_lifecycle = db.execute('SELECT 1 FROM revisions WHERE instr(document,?)>0 LIMIT 1', ('"lifecycle"',)).fetchone()
        candidates = set(rows)
        if not has_lifecycle:
            return candidates
        snapshot = self.snapshot()
        active_samples = {(item['id'], item['revision_id']): item for item in snapshot.of_kind('sample')}
        related = {}
        for kind in ('measurement', 'treatment'):
            for item in snapshot.of_kind(kind):
                related.setdefault(item['data']['sample_id'], []).append(item['data'])
        return {key for key in candidates if key in active_samples and (not query or query in normalized(json.dumps(
            [active_samples[key]['data'], key[0], active_samples[key]['registrant'], related.get(key[0], [])], ensure_ascii=False)))}


class LocalSession:
    """Single-PC demo using the same bundled SQLite search as shared sessions."""
    def __init__(self, store):
        self.base, self.root, self.store_id = store, store.root, store.store_id
        self.index = RecordIndex(self.root / 'search.sqlite', {'local': store})

    def cached(self):
        return self.index.snapshot()

    def load(self):
        self.index.refresh('local')
        return self.cached()

    def search_samples(self, query):
        return self.index.search_samples(query)

    def rebuild_index(self):
        index = RecordIndex(self.index.path, self.index.stores)
        index.rebuild()
        self.index = index

    def save(self, kind, data, actor, **kwargs):
        item = self.base.save(kind, data, actor, _snapshot=self.cached(), **kwargs)
        try:
            self.index.record('local', item)
        except (sqlite3.DatabaseError, StoreError, OSError) as exc:
            self.index.dirty.add('local')
            raise CommitUnverifiedError(item['id'], item['revision_id']) from exc
        return item

    def read_revision(self, record_id, revision_id):
        item = self.base.read_revision(record_id, revision_id)
        self.index.record('local', item)
        return item

    def set_visibility(self, item, actor, deleted, reason, *, attachment_id=None):
        return self.save(item['kind'], item['data'], actor,
            record_id=item['id'], base_revision=item['revision_id'], _visibility=(deleted, reason, attachment_id))

    def can_edit(self, item, actor):
        return self.base.can_edit(item, actor)

    def export_attachment(self, item, attachment_id, destination):
        return self.base.export_attachment(item, attachment_id, destination)
