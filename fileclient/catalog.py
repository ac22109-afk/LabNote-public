"""Canonical input vocabulary. Prototype categories are reference data only."""
import copy
import re
import unicodedata

from .store import StoreError, normalized, text

DEFAULT_CATALOG = {
    'version': 1,
    'alloys': {
        'アルミニウム合金': ['A1050', 'A1100', 'A2024', 'A3003', 'A5052', 'A5083', 'A6061', 'A6063', 'A7075', 'ADC12', '4N'],
        'マグネシウム合金': ['AZ31', 'AZ61', 'AZ91', 'AM60', 'ZK60', 'WE43'],
        'チタン合金': ['純Ti（Grade 1）', '純Ti（Grade 2）', 'Ti-6Al-4V', 'Ti-6Al-7Nb', 'Ti-5Al-2.5Sn'],
        '亜鉛合金': ['Zamak 2', 'Zamak 3', 'Zamak 5', 'ZA-8', 'ZA-12', 'ZA-27'],
    },
    'polishing': ['未研磨', '湿式研磨', 'バフ研磨', '湿式研磨＋バフ研磨', '電解研磨'],
    'treatments': ['水蒸気処理', '時効処理', '焼鈍', '溶体化処理', '熱処理（その他）', '表面処理（その他）', '処理なし'],
    'methods': {
        'SEM': ['結晶粒径', '粒子径', '結晶粒面積', '膜厚（断面）'],
        'AFM': ['表面粗さ Sa', '表面粗さ Sq', '表面粗さ Rq', '段差', '表面電位'],
        '膜厚測定': ['膜厚', '結晶層膜厚', '緻密層膜厚'],
        '分極測定': ['腐食電位', '腐食電流密度', '分極抵抗'],
        '硬さ試験': ['ビッカース硬さ', 'ロックウェル硬さ'],
        'XRD': ['回折ピーク位置', '半値幅', '結晶子サイズ'],
        '引張試験': ['引張強さ', '降伏応力', '破断伸び'],
        'XPS': ['元素濃度', '結合エネルギー', 'ピーク強度'],
        'FTIR': ['吸光度', 'ピーク波数'],
        'DSC': ['熱流', 'ピーク温度'],
        'エリプソメトリー': ['Ψ', 'Δ', '膜厚'],
        '疲労試験': ['疲労寿命', '応力振幅', 'き裂長さ', 'き裂数'],
        '接着試験': ['接着強度'],
        '接触角測定': ['接触角'],
        'MCシミュレーション': ['溶質濃度'],
        '画像観察': [],
    },
    'units': {
        '結晶粒径': ['μm', 'nm'], '粒子径': ['μm', 'nm'], '結晶粒面積': ['μm²', 'nm²'],
        '膜厚（断面）': ['μm', 'nm'], '膜厚': ['μm', 'nm', 'mm'],
        '結晶層膜厚': ['μm', 'nm'], '緻密層膜厚': ['μm', 'nm'],
        '表面粗さ Sa': ['nm', 'μm'], '表面粗さ Sq': ['nm', 'μm'], '段差': ['nm', 'μm'],
        '表面電位': ['V', 'mV'], '腐食電位': ['V', 'mV'],
        '腐食電流密度': ['A/cm²', 'μA/cm²'], '分極抵抗': ['Ω·cm²', 'kΩ·cm²'],
        'ビッカース硬さ': ['HV'], 'ロックウェル硬さ': ['HRC', 'HRB'],
        '回折ピーク位置': ['° (2θ)'], '半値幅': ['° (2θ)'], '結晶子サイズ': ['nm', 'μm'],
        '引張強さ': ['MPa', 'GPa'], '降伏応力': ['MPa', 'GPa'], '破断伸び': ['%'],
        '表面粗さ Rq': ['nm', 'μm'],
        '元素濃度': ['at%', 'mass%'], '結合エネルギー': ['eV'], 'ピーク強度': ['cps', 'a.u.'],
        '吸光度': ['a.u.'], 'ピーク波数': ['cm-1'],
        '熱流': ['mW/g', 'mW'], 'ピーク温度': ['°C', 'K'],
        'Ψ': ['°'], 'Δ': ['°'],
        '疲労寿命': ['サイクル'], '応力振幅': ['MPa'], 'き裂長さ': ['μm', 'mm'], 'き裂数': ['本'],
        '接着強度': ['MPa'], '接触角': ['°'],
        '溶質濃度': ['at%', 'mass%'],
    },
}


def choice_key(value, *, unit=False):
    # Scientific unit case carries meaning: mV and MV are different units.
    # NFKC is the collation key only: it flattens cm-1 superscripts and turns
    # U+02DA into a space plus a combining mark, so it never reaches the display.
    return unicodedata.normalize('NFKC', value).strip() if unit else normalized(value)


# Display spelling the papers use. NFKC already folds U+00B5 and U+2103 in the
# collation key, but the stored text keeps whatever was typed until this runs.
NOTATION = {'℃': '°C', 'µ': 'μ', '˚': '°',
            '（': '(', '）': ')', '／': '/', '−': '-'}
# 単位の積にだけ意味がある置き換え。日本語の中黒（引張・圧縮試験）を壊さないため分ける。
UNIT_NOTATION = {'・': '·'}


def canonical_notation(value, *, unit=False):
    """One spelling per character, so the same unit reads the same everywhere."""
    table = {**NOTATION, **UNIT_NOTATION} if unit else NOTATION
    for wrong, right in table.items():
        value = value.replace(wrong, right)
    return re.sub(r'[ 　]+', ' ', value).strip()


def checked_list(values, label, *, unit=False):
    if not isinstance(values, list) or len(values) > 1000:
        raise StoreError(f'{label}の候補一覧が不正です。')
    result, keys = [], set()
    for value in values:
        value = text(value, label, True, 200)
        key = choice_key(value, unit=unit)
        if key in keys:
            raise StoreError(f'{label}に表記違いの重複候補があります: {value}')
        keys.add(key)
        result.append(value)
    return result


class Catalog:
    def __init__(self, data=None):
        data = copy.deepcopy(DEFAULT_CATALOG if data is None else data)
        if not isinstance(data, dict) or data.get('version') != 1:
            raise StoreError('選択肢設定の形式が違います。')
        self.data = {'version': 1}
        for key in ['alloys', 'methods', 'units']:
            mapping = data.get(key)
            if not isinstance(mapping, dict) or len(mapping) > 1000:
                raise StoreError('選択肢の分類が不正です。')
            names = checked_list(list(mapping), key)
            self.data[key] = {name: checked_list(mapping[name], name, unit=key == 'units') for name in names}
        for key in ['polishing', 'treatments']:
            self.data[key] = checked_list(data.get(key), key)
        if not self.data['alloys'] or not self.data['methods']:
            raise StoreError('材料区分と測定方法が必要です。')

    def options(self, kind, key, data=None):
        data = data or {}
        if kind == 'sample':
            if key == 'material':
                return list(self.data['alloys'])
            if key == 'alloy':
                return self.data['alloys'].get(data.get('material'), [])
            if key == 'polishing':
                return self.data['polishing']
        if kind == 'measurement':
            if key == 'method':
                return list(self.data['methods'])
            if key == 'metric':
                return self.data['methods'].get(data.get('method'), [])
            if key == 'unit':
                return self.data['units'].get(data.get('metric'), [])
        if kind == 'treatment':
            if key == 'method':
                return self.data['treatments']
            if key == 'duration_unit':
                return ['h', 'ks']
        return None

    def validate(self, kind, data, previous=None):
        result = dict(data)
        if kind == 'treatment':
            result.setdefault('duration_unit', 'h')
        fields = {'sample': ['material', 'alloy', 'polishing'],
                  'measurement': ['method', 'metric', 'unit'],
                  'treatment': ['method', 'duration_unit']}[kind]
        required = {'sample': {'material', 'alloy'}, 'measurement': {'method'}, 'treatment': {'method', 'duration_unit'}}[kind]
        for key in fields:
            value = text(result.get(key, ''), key, key in required and previous is None, 200)
            # Old literals are retained only while their dependency stays unchanged.
            dependency = {'alloy': 'material', 'metric': 'method', 'unit': 'metric'}.get(key)
            if previous is not None and value == previous.get(key, '') and (
                    dependency is None or result.get(dependency, '') == previous.get(dependency, '')):
                result[key] = value
                continue
            if not value and key not in required:
                result[key] = ''
                continue
            options = self.options(kind, key, result)
            matches = [option for option in options if choice_key(option, unit=key == 'unit') == choice_key(value, unit=key == 'unit')]
            if len(matches) != 1:
                label = {'material': '材料区分', 'alloy': '合金種', 'polishing': '研磨条件',
                         'method': '方法', 'metric': '測定項目', 'unit': '単位', 'duration_unit': '時間単位'}[key]
                raise StoreError(f'{label}を候補から選んでください。候補がない場合は担当者に追加を依頼してください。')
            result[key] = matches[0]
        if kind == 'measurement' and data.get('values') and (not result['metric'] or not result['unit']):
            if previous is None or data.get('values') != previous.get('values'):
                raise StoreError('数値を登録する場合は測定項目と単位も選んでください。画像・ファイルだけなら省略できます。')
        return result

    def add(self, category, value, parent=''):
        value = canonical_notation(text(value, '新しい候補', True, 200), unit=category == 'units')
        data = copy.deepcopy(self.data)
        if category in {'alloys', 'methods', 'units'}:
            if parent not in data[category]:
                if category == 'units' and parent in {m for items in data['methods'].values() for m in items}:
                    data['units'][parent] = []
                else:
                    raise StoreError('分類を選んでください。')
            values = data[category][parent]
        elif category in {'material', 'method'}:
            mapping = data['alloys' if category == 'material' else 'methods']
            if any(normalized(key) == normalized(value) for key in mapping):
                raise StoreError('同じ表記の候補が既にあります。')
            mapping[value] = []
            return Catalog(data)
        else:
            values = data[category]
        if any(choice_key(item, unit=category == 'units') == choice_key(value, unit=category == 'units') for item in values):
            raise StoreError('大文字・全角などの違いを含め、同じ候補が既にあります。')
        values.append(value)
        return Catalog(data)
