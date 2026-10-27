"""Built-in figure sizes and atomic, PC-local personal presentation presets."""
import json
import os
from dataclasses import asdict, replace
from pathlib import Path

from .graph_style import GraphStyle
from .runtime import profile_directory
from .store import StoreError, identifier, normalized, read_json, text, uid

FIGURE_FIELDS = ('width_mm', 'height_mm', 'label_size', 'tick_size', 'legend_size',
    'english_font', 'japanese_font', 'labels_bold', 'line_width', 'tick_length',
    'error_cap', 'legend_position')
FIGURE_PRESETS = {
    '報告会': dict(width_mm=120, height_mm=76, label_size=14, tick_size=12, legend_size=12,
                  english_font='Arial', japanese_font='MS Pゴシック'),
    'ポスター（A0）': dict(width_mm=190, height_mm=120, label_size=24, tick_size=20, legend_size=20,
                        english_font='Arial', japanese_font='MS Pゴシック'),
    '論文（1段・暫定）': dict(width_mm=84, height_mm=63, label_size=9, tick_size=8, legend_size=8,
                            english_font='Times New Roman', japanese_font='MS 明朝'),
    '論文（2段・暫定）': dict(width_mm=174, height_mm=110, label_size=9, tick_size=8, legend_size=8,
                            english_font='Times New Roman', japanese_font='MS 明朝'),
    '日本語資料': dict(width_mm=120, height_mm=76, label_size=10.5, tick_size=9, legend_size=9,
                      english_font='Arial', japanese_font='MS Pゴシック'),
}


class FigurePresets:
    def __init__(self, path=None):
        self.path = Path(path) if path is not None else profile_directory() / 'figure-presets.json'

    def load(self):
        if not self.path.exists():
            return {'version': 1, 'default_id': None, 'presets': []}
        data, _ = read_json(self.path)
        if (not isinstance(data, dict) or data.get('version') != 1
                or not isinstance(data.get('presets'), list) or len(data['presets']) > 100):
            raise StoreError('図の個人プリセットを読み取れません。')
        ids, names = set(), {normalized(name) for name in FIGURE_PRESETS}
        for item in data['presets']:
            if not isinstance(item, dict):
                raise StoreError('図の個人プリセットが不正です。')
            key = uid(item.get('id'))
            name = text(item.get('name'), 'プリセット名', True, 100)
            values = item.get('data')
            if (key in ids or normalized(name) in names or not isinstance(values, dict)
                    or set(values) != set(FIGURE_FIELDS)):
                raise StoreError('図のプリセット名・ID・項目を確認してください。')
            GraphStyle(**values).validate()
            ids.add(key)
            names.add(normalized(name))
        if data.get('default_id') is not None and data['default_id'] not in ids:
            raise StoreError('既定の図プリセットが見つかりません。')
        return data

    def choices(self):
        return list(FIGURE_PRESETS) + [item['name'] for item in self.load()['presets']]

    def apply(self, name, style):
        if name in FIGURE_PRESETS:
            return replace(style, **FIGURE_PRESETS[name], preset_name=name).validate()
        for item in self.load()['presets']:
            if item['name'] == name:
                return replace(style, **item['data'], preset_name=name).validate()
        raise StoreError('図のプリセットが見つかりません。')

    def default(self, style):
        saved = self.load()
        for item in saved['presets']:
            if item['id'] == saved['default_id']:
                return self.apply(item['name'], style)
        return style

    def save(self, name, style, *, update=False, make_default=False):
        style.validate()
        saved = self.load()
        name = text(name, 'プリセット名', True, 100)
        if normalized(name) in {normalized(key) for key in FIGURE_PRESETS}:
            raise StoreError('規定プリセットは更新できません。別の名前で保存してください。')
        existing = next((item for item in saved['presets'] if normalized(item['name']) == normalized(name)), None)
        if existing is not None and not update:
            raise StoreError('同じ名前があります。「このプリセットを更新」を使うか、別の名前にしてください。')
        if existing is None and update:
            raise StoreError('更新する個人プリセットを選んでください。')
        if existing is None and len(saved['presets']) >= 100:
            raise StoreError('個人プリセットは100件までです。')
        values = asdict(style)
        item = dict(id=existing['id'] if existing else identifier(), name=name,
                    data={key: values[key] for key in FIGURE_FIELDS})
        if existing:
            saved['presets'][saved['presets'].index(existing)] = item
        else:
            saved['presets'].append(item)
        if make_default:
            saved['default_id'] = item['id']
        self.write(saved)
        return item

    def set_default(self, key):
        saved = self.load()
        if key is not None and not any(item['id'] == key for item in saved['presets']):
            raise StoreError('既定のプリセットが見つかりません。')
        saved['default_id'] = key
        self.write(saved)

    def write(self, saved):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        pending = self.path.with_name(identifier() + '.pending')
        with pending.open('x', encoding='utf-8') as stream:
            json.dump(saved, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(pending, self.path)
