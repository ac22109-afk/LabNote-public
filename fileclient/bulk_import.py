"""Preview-first additive imports. Existing revisions are never overwritten."""
import csv
import hashlib
import json
import re
from dataclasses import dataclass

from .store import StoreError, normalized, registrant_id, validate_data, export_pending_path, publish_export

FIELDS = {
    'sample': [('label', '試料ID'), ('creator', '作製者'), ('material', '材料'), ('alloy', '合金種'),
               ('polishing', '研磨条件'), ('solution_treated', '溶体化処理'), ('notes', '備考')],
    'measurement': [('batch_key', '測定セット名'), ('sample_label', '試料ID'), ('creator', '作製者'),
                    ('measurer', '測定者'), ('method', '測定方法'), ('metric', '測定項目'),
                    ('unit', '単位'), ('measured_on', '測定日'), ('values', '数値'), ('notes', '備考')],
}
ALIASES = {'label': ['sample_id', 'sample_label'], 'sample_label': ['sample_id', 'label'],
           'measurer': ['operator'], 'measured_on': ['measured_at'], 'values': ['value'],
           'batch_key': ['batch', 'batch_id', 'batch_key'], 'creator': ['owner']}


@dataclass(frozen=True)
class ImportEntry:
    row: int
    kind: str
    data: dict
    token: str
    status: str
    detail: str


def suggest_mapping(headers, kind):
    result = {}
    for key, label in FIELDS[kind]:
        aliases = {normalized(value) for value in [key, label] + ALIASES.get(key, [])}
        result[key] = next((value for value in headers if normalized(value) in aliases), '')
    return result


def import_marker(token):
    return '[LabNote取込:' + token + ']'


def existing_tokens(snapshot):
    # Include old revisions, so editing a measurement later does not reimport it.
    items = [item for revisions in snapshot.revisions.values() for item in revisions.values()]
    items += [item for heads in snapshot.heads.values() for item in heads]
    return {token for item in items for token in re.findall(r'\[LabNote取込:([0-9a-f]{64})\]', item['data'].get('notes', ''))}


def preview_import(table, kind, mapping, snapshot, catalog, actor):
    actor = registrant_id(actor)
    if kind not in FIELDS or len(table.rows) > 10000:
        raise StoreError('取り込み種類・行数を確認してください。')
    if any(column and column not in table.headers for column in mapping.values()):
        raise StoreError('列の対応を確認してください。')
    groups = {}
    for number, row in enumerate(table.rows, 2):
        if not any(row):
            continue
        values = {key: row[table.headers.index(mapping[key])] if mapping.get(key) else '' for key, _ in FIELDS[kind]}
        group = ('batch', values['batch_key']) if kind == 'measurement' and values.get('batch_key') else ('row', number)
        groups.setdefault(group, []).append((number, values))
    existing = existing_tokens(snapshot)
    entries, sample_keys = [], set()
    samples = snapshot.of_kind('sample', include_deleted=kind == 'sample')
    for group, members in groups.items():
        number, row = members[0]
        token = hashlib.sha256(json.dumps([table.sha256, table.sheet, kind, group], ensure_ascii=False).encode('utf-8')).hexdigest()
        try:
            if token in existing:
                entries.append(ImportEntry(number, kind, {}, token, '取込済み', '同じファイルの同じ行／セットを再登録しません'))
                continue
            notes = row.get('notes', '')
            if kind == 'sample':
                flag = normalized(row['solution_treated'])
                if flag not in {'', '0', '1', 'false', 'true', 'なし', 'あり', '無', '有'}:
                    raise StoreError('溶体化処理は「あり／なし」またはtrue／falseを使ってください。')
                data = {**row, 'creator': registrant_id(row['creator'] or actor),
                        'material': row['material'] or 'アルミニウム合金',
                        'solution_treated': flag in {'1', 'true', 'あり', '有'}}
                key = normalized(data['creator']), normalized(data['label'])
                matches = [sample for sample in samples if (normalized(sample['data']['creator']), normalized(sample['data']['label'])) == key]
                if matches or key in sample_keys:
                    raise StoreError('同じ作製者の試料IDが既にあります。試料は上書きしません。')
            else:
                label, creator = row['sample_label'], row['creator']
                if not label:
                    raise StoreError('登録済みの試料IDを指定してください。')
                matches = [sample for sample in samples if (sample['id'] == label or normalized(sample['data']['label']) == normalized(label))
                           and (not creator or normalized(sample['data']['creator']) == normalized(creator))]
                if len(matches) != 1:
                    raise StoreError('試料を一つに特定できません。試料IDと作製者を確認してください。')
                parent = snapshot.current(matches[0]['id'])
                if any({key: value for key, value in other.items() if key != 'values'} != {key: value for key, value in row.items() if key != 'values'} for _, other in members):
                    raise StoreError('同じ測定セット内で試料・方法・日付等が異なります。セット名を分けてください。')
                data = {key: value for key, value in row.items() if key not in {'batch_key', 'sample_label', 'creator'}}
                data.update(sample_id=parent['id'], measurer=registrant_id(row['measurer'] or actor),
                            values=[value for _, member in members for value in re.split(r'[\s,;]+', member['values']) if value])
            data['notes'] = (notes + '\n' + import_marker(token)).strip()
            data = validate_data(kind, catalog.validate(kind, data))
            if kind == 'sample':
                sample_keys.add(key)
            entries.append(ImportEntry(number, kind, data, token, '登録可能', f'{len(members)}行 → 1記録'))
        except (StoreError, ValueError) as exc:
            entries.append(ImportEntry(number, kind, {}, token, '要修正', str(exc)))
    return entries


def commit_import(entries, store, actor):
    """Stop on the first failure. Report partial completion; retry via preview."""
    if not entries or any(entry.status == '要修正' for entry in entries):
        raise StoreError('要修正の行があります。ファイルを修正してプレビューし直してください。')
    completed = []
    for entry in entries:
        if entry.status != '登録可能':
            continue
        try:
            snapshot = store.cached() if hasattr(store, 'cached') else store.load()
            if entry.token in existing_tokens(snapshot):
                continue
            completed.append(store.save(entry.kind, entry.data, actor))
        except Exception as exc:
            return completed, f'{entry.row}行目で停止しました。完了{len(completed)}件。保存済みの可能性がある行も含め、再プレビューしてから再実行してください。\n{exc}'
    return completed, ''


def export_template(kind, destination):
    fields = FIELDS[kind]
    examples = {'label': '200d-24h-10k_01', 'sample_label': '200d-24h-10k_01', 'creator': 'zz00001',
                'material': 'アルミニウム合金', 'alloy': 'A5052', 'polishing': '未研磨', 'solution_treated': 'なし',
                'batch_key': 'set-01', 'measurer': 'zz00001', 'method': 'SEM', 'metric': '結晶粒径',
                'unit': 'μm', 'measured_on': '2026-10-21', 'values': '1.2', 'notes': '記入例です。実データに置き換えてください。'}
    pending = export_pending_path(destination)
    if str(destination).lower().endswith('.xlsx'):
        import openpyxl
        workbook = openpyxl.Workbook()
        worksheet = workbook.active
        worksheet.title = '測定登録' if kind == 'measurement' else '試料登録'
        worksheet.append([label for _, label in fields])
        worksheet.append([examples.get(key, '') for key, _ in fields])
        workbook.save(pending)
        workbook.close()
    else:
        with pending.open('x', encoding='utf-8-sig', newline='') as stream:
            writer = csv.writer(stream)
            writer.writerow([label for _, label in fields])
            writer.writerow([examples.get(key, '') for key, _ in fields])
    publish_export(pending, destination)
