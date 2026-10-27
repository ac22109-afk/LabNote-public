"""Append-only JSON revisions. No shared database, counter, or mutable index.

A committed revision is immutable to this client, not to a user with SMB write
access. Concurrent children are reported as a conflict, never silently merged.
SMB crash durability and access control require separate NAS validation.
"""
import csv
import hashlib
import json
import os
import re
import stat
import sys
import unicodedata
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

FORMAT = 'labnote-file-store'
SCHEMA = 1
MAX_JSON = 2 * 1024 * 1024
MAX_FILE = 512 * 1024 * 1024
KINDS = {'sample', 'measurement', 'treatment'}
# Synthesised only: a stored lifecycle is always 'active' or 'deleted'.
UNKNOWN = 'unknown'
REGISTRANT_PATTERN = r'[a-z]{2}[0-9]{5}'  # Provisional university ID format; any prefix.


class StoreError(Exception):
    pass


class ConflictError(StoreError):
    pass


class CommitUnverifiedError(StoreError):
    def __init__(self, record_id, revision_id):
        self.record_id, self.revision_id = record_id, revision_id
        super().__init__('保存済みの可能性があります。再送せず、保存状況を確認してください。'
                         f'\n記録ID: {record_id}\n改訂ID: {revision_id}')


def uid(value):
    try:
        parsed = uuid.UUID(str(value))
    except (ValueError, TypeError, AttributeError) as exc:
        raise StoreError('内部IDが不正です。') from exc
    if str(parsed) != value:
        raise StoreError('内部IDの形式が不正です。')
    return value


def identifier():
    return str(uuid.uuid4())


def fs_path(path):
    """Internal Windows I/O paths also support long UNC and profile paths."""
    path = Path(path)
    if os.name == 'nt':
        absolute = os.path.abspath(path)
        if not absolute.startswith('\\\\?\\'):
            absolute = '\\\\?\\UNC\\' + absolute[2:] if absolute.startswith('\\\\') else '\\\\?\\' + absolute
        return Path(absolute)
    return path


def text(value, label, required=False, limit=10000):
    if not isinstance(value, str):
        raise StoreError(f'{label}は文字列で入力してください。')
    value = value.strip()
    if (required and not value) or len(value) > limit:
        raise StoreError(f'{label}を確認してください（上限{limit}文字）。')
    return value


def normalized(value):
    return unicodedata.normalize('NFKC', value).strip().casefold()


def registrant_id(value):
    value = normalized(text(value, '登録者ID', True, 150))
    if not re.fullmatch(REGISTRANT_PATTERN, value):
        raise StoreError('登録者IDは英字2文字＋数字5桁で入力してください（例：zz00001、zz00002）。大学の正式な形式は確認中です。')
    return value


def numeric(value):
    try:
        result = Decimal(str(value).strip())
    except (InvalidOperation, ValueError) as exc:
        raise StoreError('数値の形式を確認してください。') from exc
    if not result.is_finite() or result.adjusted() > 17 or result.as_tuple().exponent < -12:
        raise StoreError('数値は有限値・整数部18桁以内・小数部12桁以内で入力してください。')
    return str(result)


def validate_data(kind, data):
    if kind not in KINDS or not isinstance(data, dict):
        raise StoreError('記録の種類が不正です。')
    result = {'notes': text(data.get('notes', ''), '備考')}
    if kind == 'sample':
        for name, label, required, limit in [('label', '各自の試料ID', True, 120),
                ('creator', '作製者', True, 150), ('material', '材料', True, 200),
                ('alloy', '合金種', False, 200), ('polishing', '研磨条件', False, 300)]:
            result[name] = text(data.get(name, ''), label, required, limit)
        if not isinstance(data.get('solution_treated', False), bool):
            raise StoreError('溶体化処理の値が不正です。')
        result['solution_treated'] = data.get('solution_treated', False)
    else:
        result['sample_id'] = uid(data.get('sample_id'))
        result['method'] = text(data.get('method', ''), '方法', True, 200)
        if kind == 'measurement':
            for name, label in [('measurer', '測定者'), ('metric', '測定項目'), ('unit', '単位')]:
                result[name] = text(data.get(name, ''), label, name == 'measurer', 150)
            try:
                result['measured_on'] = date.fromisoformat(data.get('measured_on', '')).isoformat()
            except (ValueError, TypeError) as exc:
                raise StoreError('測定日は YYYY-MM-DD で入力してください。') from exc
            values = data.get('values', [])
            if not isinstance(values, list) or len(values) > 5000:
                raise StoreError('数値は5000点以内で入力してください。')
            result['values'] = [numeric(v) for v in values]
        else:
            for name in ['temperature', 'duration']:
                value = data.get(name, '')
                result[name] = numeric(value) if str(value).strip() else ''
            if result['duration'] and Decimal(result['duration']) < 0:
                raise StoreError('処理時間は0以上で入力してください。')
            result['duration_unit'] = data.get('duration_unit', 'h')
            if result['duration_unit'] not in {'h', 'ks'}:
                raise StoreError('時間単位はhまたはksです。')
    return result


def read_json(path):
    with fs_path(path).open('rb') as stream:
        raw = stream.read(MAX_JSON + 1)
    if len(raw) > MAX_JSON:
        raise StoreError('記録ファイルが上限を超えています。')
    try:
        return json.loads(raw.decode('utf-8')), raw
    except (ValueError, UnicodeError) as exc:
        raise StoreError('読み取れないJSONです。') from exc


@dataclass
class Snapshot:
    revisions: dict = field(default_factory=dict)
    heads: dict = field(default_factory=dict)
    warnings: list = field(default_factory=list)
    blocked: set = field(default_factory=set)

    def current(self, record_id, *, include_deleted=False):
        if record_id in self.blocked:
            raise StoreError('読めない改訂があるため、復旧するまでこの記録の訂正・参照登録を保留します。')
        choices = self.heads.get(record_id, [])
        if not choices:
            raise StoreError('記録が見つかりません。')
        if len(choices) > 1:
            raise ConflictError('同じ記録に競合する訂正があります。履歴を確認してください。')
        if not include_deleted and self.hidden(choices[0]):
            raise StoreError('この記録または関連試料はごみ箱にあります。先に復元してください。')
        return choices[0]

    def status(self, item, attachment_id=None):
        seen = set()
        while item and item['revision_id'] not in seen:
            seen.add(item['revision_id'])
            value = item.get('attachment_lifecycle', {}).get(attachment_id) if attachment_id else item.get('lifecycle')
            if value is not None:
                return value
            if item['parent_revision'] is None:
                # The whole ancestry was readable and never recorded a decision.
                return {'state': 'active', 'reason': ''}
            item = self.revisions.get(item['id'], {}).get(item['parent_revision'])
        # The walk stopped at a missing/looping ancestor, so no decision is known.
        return {'state': UNKNOWN, 'reason': '元の改訂が読めないため、ごみ箱の状態を確認できません。'}

    def hidden(self, item):
        # Anything but a confirmed 'active' stays out of the ordinary lists.
        if any(self.status(head)['state'] != 'active' for head in self.heads.get(item['id'], [item])):
            return True
        if item['kind'] != 'sample':
            return any(self.status(parent)['state'] != 'active' for parent in self.heads.get(item['data']['sample_id'], []))
        return False

    def of_kind(self, kind, *, include_deleted=False):
        return [head for heads in self.heads.values() for head in heads if head['kind'] == kind and (include_deleted or not self.hidden(head))]


class FileStore:
    def __init__(self, root):
        self.root = Path(root).resolve()
        manifest, _ = read_json(self.root / 'labnote-store.json')
        if not isinstance(manifest, dict) or manifest.get('format') != FORMAT or manifest.get('schema') != SCHEMA:
            raise StoreError('対応するLabNote専用保存先ではありません。')
        uid(manifest.get('store_id'))
        self.store_id = manifest['store_id']

    @classmethod
    def create(cls, root):
        root = Path(root).resolve()
        # Refuse existing directories, including empty ones. Never adopt Public.
        root.mkdir(parents=False, exist_ok=False)
        (root / 'records').mkdir()
        manifest = {'format': FORMAT, 'schema': SCHEMA, 'store_id': identifier()}
        with (root / 'labnote-store.json').open('x', encoding='utf-8') as stream:
            json.dump(manifest, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        return cls(root)

    def _revision_dir(self, record_id, revision_id):
        return fs_path(self.root / 'records' / uid(record_id) / uid(revision_id))

    def load(self, *, _reader=None):
        snapshot = Snapshot()
        for record_dir in sorted(fs_path(self.root / 'records').iterdir()):
            try:
                if not stat.S_ISDIR(record_dir.stat().st_mode):
                    continue
                uid(record_dir.name)
                revision_dirs = sorted(record_dir.iterdir())
            except StoreError:
                snapshot.warnings.append(f'不明な記録フォルダ: {record_dir.name}')
                continue
            except OSError as exc:
                snapshot.warnings.append(f'記録フォルダを読めません {record_dir.name}: {exc}')
                snapshot.blocked.add(record_dir.name)
                continue
            valid = {}
            for revision_dir in revision_dirs:
                try:
                    if not stat.S_ISDIR(revision_dir.stat().st_mode):
                        continue
                    uid(revision_dir.name)
                    try:
                        commit_stat = (revision_dir / 'commit.json').stat()
                    except FileNotFoundError:
                        snapshot.warnings.append(f'保存途中／未完了: {record_dir.name}/{revision_dir.name}')
                        continue
                    if not stat.S_ISREG(commit_stat.st_mode):
                        raise StoreError('完了印が通常のファイルではありません。')
                    item = (_reader or self.read_revision)(record_dir.name, revision_dir.name)
                    valid[item['revision_id']] = item
                except (OSError, StoreError, ValueError, KeyError, TypeError) as exc:
                    snapshot.warnings.append(f'読取保留 {record_dir.name}/{revision_dir.name}: {exc}')
                    snapshot.blocked.add(record_dir.name)
            # A child is usable only when its full ancestry exists and is valid.
            accepted = {}
            pending = dict(valid)
            while pending:
                progressed = False
                for key, item in list(pending.items()):
                    parent_id = item['parent_revision']
                    if parent_id is None or parent_id in accepted:
                        if parent_id is not None:
                            parent = accepted[parent_id]
                            if item['kind'] != parent['kind'] or item['registrant'] != parent['registrant']:
                                snapshot.warnings.append(f'記録の種別／登録者が不整合: {key}')
                                snapshot.blocked.add(record_dir.name)
                                del pending[key]
                                continue
                        accepted[key] = item
                        del pending[key]
                        progressed = True
                if not progressed:
                    break
            if pending:
                snapshot.warnings.append(f'元の改訂が欠損／不正: {record_dir.name}')
                snapshot.blocked.add(record_dir.name)
            if not accepted:
                continue
            used_parents = {item['parent_revision'] for item in accepted.values()}
            heads = [item for key, item in accepted.items() if key not in used_parents]
            snapshot.revisions[record_dir.name] = accepted
            snapshot.heads[record_dir.name] = heads
            if len(heads) > 1:
                snapshot.warnings.append(f'訂正競合（自動上書きなし）: {record_dir.name}')
        labels = {}
        for sample in snapshot.of_kind('sample'):
            data = sample['data']
            key = (normalized(data['creator']), normalized(data['label']))
            if key in labels and labels[key] != sample['id']:
                snapshot.warnings.append(f'試料IDの重複候補: {data["label"]} / {data["creator"]}')
            labels[key] = sample['id']
        for kind in ['measurement', 'treatment']:
            for item in snapshot.of_kind(kind):
                parents = snapshot.heads.get(item['data']['sample_id'], [])
                if not any(parent['kind'] == 'sample' for parent in parents):
                    snapshot.warnings.append(f'関連試料が見つかりません: {item["id"]}')
        return snapshot

    def read_revision(self, record_id, revision_id):
        directory = self._revision_dir(record_id, revision_id)
        commit, _ = read_json(directory / 'commit.json')
        item, raw = read_json(directory / 'document.json')
        if not isinstance(commit, dict) or commit.get('sha256') != hashlib.sha256(raw).hexdigest():
            raise StoreError('記録のハッシュが一致しません。')
        if not isinstance(item, dict) or item.get('schema') != SCHEMA or item.get('id') != record_id or item.get('revision_id') != revision_id:
            raise StoreError('記録の形式が一致しません。')
        if item.get('parent_revision') is not None:
            uid(item['parent_revision'])
        text(item.get('actor'), '訂正者', True, 150)
        text(item.get('registrant'), '登録者', True, 150)
        item['data'] = validate_data(item.get('kind'), item.get('data'))
        statuses = [item['lifecycle']] if 'lifecycle' in item else []
        if 'attachment_lifecycle' in item:
            if not isinstance(item['attachment_lifecycle'], dict) or len(item['attachment_lifecycle']) > 1000:
                raise StoreError('添付のごみ箱情報が不正です。')
            for key, value in item['attachment_lifecycle'].items():
                uid(key)
                statuses.append(value)
        for status in statuses:
            if not isinstance(status, dict) or status.get('state') not in {'active', 'deleted'}:
                raise StoreError('ごみ箱情報が不正です。')
            text(status.get('reason'), '削除・復元の理由', True, 500)
        if not isinstance(item.get('attachments'), list) or len(item['attachments']) > 1000:
            raise StoreError('添付一覧が不正です。')
        for attachment in item['attachments']:
            uid(attachment['id'])
            uid(attachment['event_id'])
            text(attachment['name'], '添付名', True, 255)
            if not isinstance(attachment['size'], int) or not 0 <= attachment['size'] <= MAX_FILE:
                raise StoreError('添付サイズが不正です。')
            digest = attachment['sha256']
            if not isinstance(digest, str) or len(digest) != 64 or any(c not in '0123456789abcdef' for c in digest):
                raise StoreError('添付ハッシュが不正です。')
        return item

    @staticmethod
    def can_edit(item, actor):
        responsible = item['data'].get('creator') if item['kind'] == 'sample' else item['data'].get('measurer')
        return normalized(actor) in {normalized(item['registrant']), normalized(responsible or item['registrant'])}

    def save(self, kind, data, actor, *, record_id=None, base_revision=None, files=(), origin=None, _snapshot=None, _visibility=None):
        actor = registrant_id(actor)
        if origin is not None:
            origin = {'store_id': uid(origin['store_id']), 'client_id': uid(origin['client_id'])}
        data = validate_data(kind, data)
        current = _snapshot if _snapshot is not None else self.load()
        previous = None
        if record_id is not None:
            uid(record_id)
            previous = current.current(record_id, include_deleted=_visibility is not None)
            if previous['revision_id'] != base_revision:
                raise ConflictError('別の訂正が保存されています。一覧を更新して開き直してください。')
            if previous['kind'] != kind:
                raise StoreError('記録の種別を変更できません。')
            if not self.can_edit(previous, actor):
                raise StoreError('画面上では担当者または登録者の表示名で訂正してください。')
            owner = 'creator' if kind == 'sample' else 'measurer' if kind == 'measurement' else None
            if owner and data[owner] != previous['data'][owner]:
                raise StoreError('担当者の変更はこの検証版では保留です。')
            if kind != 'sample' and data['sample_id'] != previous['data']['sample_id']:
                raise StoreError('関連試料を訂正で変更できません。')
        elif base_revision is not None:
            raise StoreError('新規記録に元の改訂は指定できません。')
        if _visibility is not None:
            if previous is None:
                raise StoreError('ごみ箱操作には登録済みの記録が必要です。')
            deleted, reason, attachment_id = _visibility
            if not isinstance(deleted, bool):
                raise StoreError('ごみ箱の操作が不正です。')
            reason = text(reason, '削除・復元の理由', True, 500)
            if attachment_id and attachment_id not in {a['id'] for a in previous['attachments']}:
                raise StoreError('対象の添付がありません。')
            state = current.status(previous, attachment_id)['state']
            if state == UNKNOWN:
                # 改訂のつながりをたどれない記録を復元すると、たどれない部分を
                # 無視して有効と決めてしまう。復元も削除も保留する。
                raise StoreError('改訂のつながりをたどれないため、ごみ箱の操作を保留します。履歴を確認してください。')
            if state == ('deleted' if deleted else 'active'):
                raise StoreError('対象の状態が変わりました。再読込してください。')
            if attachment_id and current.hidden(previous):
                raise StoreError('先に記録・親試料を復元してください。')
        if kind != 'sample' and (_visibility is None or not _visibility[0]):
            if current.current(data['sample_id'])['kind'] != 'sample':
                raise StoreError('関連先が試料ではありません。')
        if kind == 'sample':
            for sample in current.of_kind('sample', include_deleted=True):
                if sample['id'] != record_id and normalized(sample['data']['creator']) == normalized(data['creator']) and normalized(sample['data']['label']) == normalized(data['label']):
                    # A match in the trash is invisible in the lists, so say where it is.
                    if current.status(sample)['state'] == 'deleted':
                        raise StoreError('同じ作製者の試料IDがごみ箱にあります。復元するか、別の試料IDにしてください。')
                    raise StoreError('同じ作製者の試料IDが既にあります。')
        # Reject ordinary input errors before creating a partial revision.
        if len(previous['attachments'] if previous else []) + len(files) > 1000:
            raise StoreError('添付は一記録1000件までです。')
        for source in files:
            source = Path(source)
            if not source.is_file() or source.stat().st_size > MAX_FILE:
                raise StoreError('添付は通常のファイル、1件512MiB以内にしてください。')
            text(source.name, '添付名', True, 255)
        record_id = record_id or identifier()
        revision_id = identifier()
        directory = self._revision_dir(record_id, revision_id)
        directory.parent.mkdir(exist_ok=True)
        directory.mkdir(exist_ok=False)
        attachments = list(previous['attachments']) if previous else []
        for source in files:
            source = Path(source)
            if not source.is_file() or source.stat().st_size > MAX_FILE:
                raise StoreError('添付は通常のファイル、1件512MiB以内にしてください。')
            blob_id = identifier()
            (directory / 'files').mkdir(exist_ok=True)
            digest = hashlib.sha256()
            size = 0
            with source.open('rb') as incoming, (directory / 'files' / blob_id).open('xb') as output:
                while chunk := incoming.read(1024 * 1024):
                    size += len(chunk)
                    if size > MAX_FILE:
                        raise StoreError('転送中に添付が512MiBを超えました。')
                    output.write(chunk)
                    digest.update(chunk)
                output.flush()
                os.fsync(output.fileno())
            attachments.append({'id': blob_id, 'event_id': revision_id,
                'name': text(source.name, '添付名', True, 255), 'size': size, 'sha256': digest.hexdigest()})
        item = {'schema': SCHEMA, 'id': record_id, 'revision_id': revision_id,
                'parent_revision': base_revision, 'kind': kind, 'data': data,
                'actor': actor, 'registrant': previous['registrant'] if previous else actor,
                'created_at': datetime.now(timezone.utc).isoformat(), 'attachments': attachments}
        if origin is not None:
            item['origin'] = origin
        if _visibility is not None:
            status = {'state': 'deleted' if deleted else 'active', 'reason': reason,
                      'actor': actor, 'at': item['created_at']}
            if attachment_id:
                item['attachment_lifecycle'] = {attachment_id: status}
            else:
                item['lifecycle'] = status
        raw = json.dumps(item, ensure_ascii=False, indent=2, allow_nan=False).encode('utf-8')
        if len(raw) > MAX_JSON:
            raise StoreError('記録が大きすぎます。')
        with (directory / 'document.json').open('xb') as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        commit = json.dumps({'sha256': hashlib.sha256(raw).hexdigest()}).encode('ascii')
        with (directory / 'commit.pending').open('xb') as stream:
            stream.write(commit)
            stream.flush()
            os.fsync(stream.fileno())
        # Unique revision directory. Rename only publishes this new commit marker.
        try:
            os.rename(directory / 'commit.pending', directory / 'commit.json')
            return self.read_revision(record_id, revision_id)
        except (OSError, StoreError, ValueError, KeyError, TypeError) as exc:
            # A network error can occur after the NAS accepted the publish.
            raise CommitUnverifiedError(record_id, revision_id) from exc

    def set_visibility(self, item, actor, deleted, reason, *, attachment_id=None):
        return self.save(item['kind'], item['data'], actor,
                         record_id=item['id'], base_revision=item['revision_id'],
                         _visibility=(deleted, reason, attachment_id))

    def export_attachment(self, item, attachment_id, destination):
        verified = self.read_revision(item['id'], item['revision_id'])
        matches = [a for a in verified['attachments'] if a['id'] == uid(attachment_id)]
        if len(matches) != 1:
            raise StoreError('添付が見つかりません。')
        attachment = matches[0]
        source = self._revision_dir(item['id'], attachment['event_id']) / 'files' / attachment['id']
        destination = Path(destination)
        pending = export_pending_path(destination)
        digest, size = hashlib.sha256(), 0
        # Only a fully verified copy receives the requested final filename.
        with source.open('rb') as incoming, pending.open('xb') as output:
            while chunk := incoming.read(1024 * 1024):
                output.write(chunk)
                digest.update(chunk)
                size += len(chunk)
            output.flush()
            os.fsync(output.fileno())
        if size != attachment['size'] or digest.hexdigest() != attachment['sha256']:
            raise StoreError('添付の整合性が一致しません。指定名での保存は行っていません。途中ファイルは.part名で残します。')
        publish_export(pending, destination)


def export_pending_path(destination):
    destination = Path(destination)
    if destination.exists():
        raise FileExistsError(destination)
    # A short sibling name also works under Windows' legacy path limit.
    return destination.with_name(f'.{identifier()}.part')


def publish_export(pending, destination):
    # Never use a check-then-replacing rename on POSIX.
    if os.name == 'nt':
        os.rename(pending, destination)
    elif sys.platform == 'darwin':
        darwin_rename_exclusive(pending, destination)
    else:
        os.link(pending, destination)


def darwin_rename_exclusive(pending, destination):
    import ctypes
    library = ctypes.CDLL(None, use_errno=True)
    rename = library.renamex_np
    rename.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
    rename.restype = ctypes.c_int
    # Apple's sys/stdio.h: RENAME_EXCL = 0x00000004.
    if rename(os.fsencode(pending), os.fsencode(destination), 0x00000004):
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), str(destination))


def export_csv(snapshot, destination, *, states=None):
    def cell(value):
        value = str(value)
        return "'" + value if value.lstrip().startswith(('=', '+', '-', '@')) or value.startswith(('\t', '\r', '\n')) else value
    pending = export_pending_path(destination)
    with pending.open('x', encoding='utf-8-sig', newline='') as stream:
        writer = csv.writer(stream)
        writer.writerow(['内部ID', '各自の試料ID', '作製者', '登録者', '材料', '合金種', '備考', '改訂ID'] + (['保存状態'] if states is not None else []))
        for item in snapshot.of_kind('sample'):
            data = item['data']
            row = [item['id'], data['label'], data['creator'], item['registrant'], data['material'], data['alloy'], data['notes'], item['revision_id']]
            if states is not None:
                row.append(states.get(item['id'], '未判定'))
            writer.writerow([cell(v) for v in row])
        stream.flush()
        os.fsync(stream.fileno())
    publish_export(pending, destination)
