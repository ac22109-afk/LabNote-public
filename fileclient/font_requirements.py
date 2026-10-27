"""Declarative system-font preflight. No downloads, installers, or font copies."""
import unicodedata
from pathlib import Path

from .store import StoreError, read_json, text


def load_font_requirements(path=None):
    path = Path(path) if path is not None else Path(__file__).with_name('font-requirements.json')
    data, _ = read_json(path)
    if not isinstance(data, dict) or data.get('version') != 1 or not isinstance(data.get('fonts'), list):
        raise StoreError('必要フォントの一覧を読み取れません。')
    if not 1 <= len(data['fonts']) <= 30:
        raise StoreError('必要フォントの一覧の件数が不正です。')
    seen = set()
    for item in data['fonts']:
        if not isinstance(item, dict) or set(item) != {'name', 'aliases', 'purpose', 'future'}:
            raise StoreError('必要フォントの項目が不正です。')
        name = text(item['name'], '書体名', True, 100)
        if name in seen:
            raise StoreError('必要フォントが重複しています。')
        seen.add(name)
        text(item['purpose'], '用途', True, 100)
        if not isinstance(item['future'], bool) or not isinstance(item['aliases'], list) or not 1 <= len(item['aliases']) <= 10:
            raise StoreError('必要フォントの別名・用途が不正です。')
        for alias in item['aliases']:
            text(alias, 'フォントの別名', True, 100)
    return data['fonts']


def inspect_fonts(available, requirements=None):
    def key(value):
        return unicodedata.normalize('NFKC', value).strip().casefold()
    available = {key(value): value for value in available if not value.startswith('@')}
    result = []
    for item in load_font_requirements() if requirements is None else requirements:
        match = next((available[key(alias)] for alias in [item['name']] + item['aliases'] if key(alias) in available), None)
        result.append({'name': item['name'], 'purpose': item['purpose'], 'future': item['future'],
                       'installed': match is not None, 'matched_family': match})
    return result


def format_font_status(items):
    rows = ['このPCの必要フォント', '']
    rows.extend(f'{item["name"]}：{"導入済み" if item["installed"] else "未導入"} ／ {item["purpose"]}' for item in items)
    rows.extend(['', 'Symbolは次の表示機能用の準備確認です。今の版でSymbolを指定する機能は未実装です。',
                 '', '未導入の書体は担当者へ連絡してください。正規の導入後にアプリを再起動して確認します。',
                 'フォント本体はこのアプリに同梱していません。フォントの購入や自動ダウンロード・コピーは行いません。'])
    return '\n'.join(rows)
