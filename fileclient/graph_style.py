"""Presentation options only; none of these edits change research records."""
import json
import math
import os
import re
import unicodedata
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

from .runtime import profile_directory
from .store import StoreError, identifier, read_json

ENGLISH_FONTS = ('Arial', 'Times New Roman')
JAPANESE_FONTS = ('MS 明朝', 'MS Pゴシック')
ERROR_MODES = ('なし', '標本SD', '標準誤差（SD/√n）', '最小–最大')
ERROR_DIRECTIONS = ('両側', '上側のみ', '下側のみ')
LEGEND_POSITIONS = ('上（枠外）', '下（枠外）', '右上（枠内）', '左上（枠内）', '右下（枠内）', '左下（枠内）', '自由配置', '非表示')
MARKERS = ('丸', '四角', '三角', 'ひし形', '十字', '×')
BLACK = '#000000'


def font_family(name):
    # Tk does not resolve the mixed ASCII/Japanese name "MS 明朝" correctly.
    return {'MS 明朝': 'MS Mincho', 'MS Pゴシック': 'MS PGothic'}.get(name, name)


def missing_fonts(style, available):
    def key(name):
        return unicodedata.normalize('NFKC', name).strip().casefold()
    available = {key(name) for name in available}
    return [name for name in (style.english_font, style.japanese_font)
            if not {key(name), key(font_family(name))} & available]


@dataclass(frozen=True)
class GraphStyle:
    english_font: str = 'Arial'
    japanese_font: str = 'MS Pゴシック'
    label_size: float = 14
    tick_size: float = 12
    legend_size: float = 12
    width_mm: float = 120
    height_mm: float = 76
    preset_name: str = '報告会'
    labels_bold: bool = True
    line_width: float = .9
    tick_length: float = 5.25
    x_label: str | None = None
    y_label: str | None = None
    x_italics: tuple = ()
    y_italics: tuple = ()
    x_supers: tuple = ()
    y_supers: tuple = ()
    x_subs: tuple = ()
    y_subs: tuple = ()
    x_symbols: tuple = ()
    y_symbols: tuple = ()
    error_mode: str = 'なし'
    error_direction: str = '両側'
    error_multiplier: float = 1
    error_cap: float = 3.75
    legend_position: str = '上（枠外）'
    legend_x: float = 65
    legend_y: float = 5
    series: dict = field(default_factory=dict)

    def validate(self):
        for value, choices in ((self.english_font, ENGLISH_FONTS), (self.japanese_font, JAPANESE_FONTS),
                (self.error_mode, ERROR_MODES), (self.error_direction, ERROR_DIRECTIONS),
                (self.legend_position, LEGEND_POSITIONS)):
            if value not in choices:
                raise StoreError('グラフの書式を候補から選んでください。')
        if not isinstance(self.labels_bold, bool):
            raise StoreError('軸名の太字設定が不正です。')
        for value in (self.label_size, self.tick_size, self.legend_size):
            if type(value) not in (int, float) or not math.isfinite(value) or not 6 <= value <= 72:
                raise StoreError('文字サイズは6〜72 ptで指定してください。')
        for value in (self.width_mm, self.height_mm):
            if type(value) not in (int, float) or not math.isfinite(value) or not 40 <= value <= 1000:
                raise StoreError('図の幅・高さは40〜1000 mmで指定してください。')
        if not isinstance(self.preset_name, str) or len(self.preset_name) > 100:
            raise StoreError('図のプリセット名が不正です。')
        for value, lower, upper in ((self.line_width, .3, 4), (self.tick_length, 1, 15),
                (self.error_multiplier, .01, 10), (self.error_cap, 0, 15),
                (self.legend_x, 0, 100), (self.legend_y, 0, 100)):
            if type(value) not in (int, float) or not math.isfinite(value) or not lower <= value <= upper:
                raise StoreError('線幅・目盛り・エラーバー・凡例位置の数値を確認してください。')
        for axis, value in (('x', self.x_label), ('y', self.y_label)):
            if value is not None and (not isinstance(value, str) or len(value) > 160 or '\n' in value or '\r' in value):
                raise StoreError('軸名は改行なし・160文字以内で入力してください。')
            for suffix in ('italics', 'supers', 'subs', 'symbols'):
                ranges = getattr(self, axis + '_' + suffix)
                if len(ranges) > 160:
                    raise StoreError('書式の範囲が多すぎます。')
                for span in ranges:
                    if len(span) != 2 or any(type(n) is not int for n in span) or not 0 <= span[0] < span[1] <= len(value or ''):
                        raise StoreError('軸名の書式の範囲を確認してください。')
        if not isinstance(self.series, dict) or len(self.series) > 1000:
            raise StoreError('系列の設定が不正です。')
        for key, item in self.series.items():
            if not isinstance(key, str) or not isinstance(item, dict):
                raise StoreError('系列の設定が不正です。')
            if not isinstance(item.get('label', key), str) or len(item.get('label', key)) > 60:
                raise StoreError('凡例の文字は60文字以内で入力してください。')
            if any(char in item.get('label', key) for char in ('\n', '\r')):
                raise StoreError('凡例の文字は改行なしで入力してください。')
            color = item.get('color', BLACK)
            if not isinstance(color, str) or not re.fullmatch(r'#[0-9a-fA-F]{6}', color):
                raise StoreError('系列色の指定が不正です。')
            if item.get('marker', MARKERS[0]) not in MARKERS:
                raise StoreError('マーカーの形を選んでください。')
        return self


SYMBOL_FONT = 'Symbol'
# Symbol.ttf carries no Unicode cmap: its only table maps U+F020-U+F0FE, which is
# U+F000 plus the ASCII position of the letter. Greek stays real Unicode in the
# stored text so it remains searchable, and moves to those code points only when
# drawing. The published figures use the same font for Greek.
SYMBOL_ASCII = {
    'µ': 'm',
    'α': 'a', 'β': 'b', 'γ': 'g', 'δ': 'd', 'ε': 'e',
    'ζ': 'z', 'η': 'h', 'θ': 'q', 'ι': 'i', 'κ': 'k',
    'λ': 'l', 'μ': 'm', 'ν': 'n', 'ξ': 'x', 'ο': 'o',
    'π': 'p', 'ρ': 'r', 'ς': 'V', 'σ': 's', 'τ': 't',
    'υ': 'u', 'φ': 'f', 'χ': 'c', 'ψ': 'y', 'ω': 'w',
    'Α': 'A', 'Β': 'B', 'Γ': 'G', 'Δ': 'D', 'Ε': 'E',
    'Ζ': 'Z', 'Η': 'H', 'Θ': 'Q', 'Ι': 'I', 'Κ': 'K',
    'Λ': 'L', 'Μ': 'M', 'Ν': 'N', 'Ξ': 'X', 'Ο': 'O',
    'Π': 'P', 'Ρ': 'R', 'Σ': 'S', 'Τ': 'T', 'Υ': 'U',
    'Φ': 'F', 'Χ': 'C', 'Ψ': 'Y', 'Ω': 'W',
}
# Smaller face raised or lowered from the baseline, as the published figures set them.
SCRIPT_SIZE = .68
SCRIPT_RISE = .34
SCRIPT_DROP = .18


def symbol_text(value):
    """Greek moved to the Symbol font's own code points. For drawing only."""
    return ''.join(chr(0xF000 + ord(SYMBOL_ASCII[char])) if char in SYMBOL_ASCII else char
                   for char in value)


# 軸名そのものと、軸名の文字位置を指す書式範囲。標準書式には保存しない。
# 範囲だけが残ると、軸名が無い状態で validate() が必ず失敗する。
PER_GRAPH_FIELDS = ('x_label', 'y_label', 'series',
                    'x_italics', 'y_italics', 'x_supers', 'y_supers',
                    'x_subs', 'y_subs', 'x_symbols', 'y_symbols')


def font_runs(text, italics=(), supers=(), subs=(), symbols=()):
    """Japanese uses its selected face; Greek automatically uses Symbol.

    Yields (text, language, italic, script, symbol) runs, where script is '',
    'super' or 'sub'. Scripts are ranges rather than Unicode superscript
    characters: Arial has no U+207B, so cm-1 with a range renders in every
    selectable font while cm⁻¹ does not.
    """
    runs = []
    for i, char in enumerate(text):
        language = 'jp' if '\u2e80' <= char <= '\uffef' else 'en'
        italic = any(start <= i < end for start, end in italics)
        symbol = char in SYMBOL_ASCII or any(start <= i < end for start, end in symbols)
        script = ('super' if any(start <= i < end for start, end in supers)
                  else 'sub' if any(start <= i < end for start, end in subs) else '')
        shape = (language, italic, script, symbol)
        if runs and runs[-1][1:] == shape:
            runs[-1] = (runs[-1][0] + char,) + shape
        else:
            runs.append((char,) + shape)
    return runs


PT_PER_MM = 72 / 25.4


def text_width_pt(text, size, *, italics=(), supers=(), subs=(), symbols=()):
    """Conservative layout estimate, not a substitute for installed-font metrics."""
    total = 0
    for value, language, italic, script, symbol in font_runs(text, italics, supers, subs, symbols):
        scale = SCRIPT_SIZE if script else 1
        for char in value:
            em = (1.05 if language == 'jp' or char in 'MWmw@%Ω' or unicodedata.east_asian_width(char) in ('W', 'F') else
                  .8 if char.isupper() or symbol else .62 if not char.isspace() else .35)
            total += size * scale * em
    return total * 1.05


def error_extent(row, style):
    if style.error_mode == 'なし':
        return None
    if style.error_mode == '最小–最大':
        lower, upper = row.mean - min(row.values), max(row.values) - row.mean
    else:
        if row.sd is None:
            return None
        deviation = row.sd / math.sqrt(len(row.values)) if style.error_mode == '標準誤差（SD/√n）' else row.sd
        lower = upper = deviation
    lower, upper = lower * style.error_multiplier, upper * style.error_multiplier
    if style.error_direction == '上側のみ':
        lower = 0
    elif style.error_direction == '下側のみ':
        upper = 0
    return lower, upper


def style_path():
    return profile_directory() / 'graph-style.json'


def load_style(path=None):
    path = Path(path) if path is not None else style_path()
    if not path.exists():
        return GraphStyle()
    data, _ = read_json(path)
    if not isinstance(data, dict) or data.get('version') not in (1, 2) or not isinstance(data.get('style'), dict):
        raise StoreError('グラフの標準書式を読み取れません。')
    allowed = {item.name for item in fields(GraphStyle)} - set(PER_GRAPH_FIELDS)
    if set(data['style']) - allowed:
        raise StoreError('グラフの標準書式の項目が不正です。')
    values = dict(data['style'])
    if data['version'] == 1:
        # Old files used CSS pixels; do not rewrite the user's original on load.
        for key, default in dict(label_size=20, tick_size=16, legend_size=16,
                                 line_width=1.2, tick_length=7, error_cap=5).items():
            value = values.get(key, default)
            if type(value) not in (int, float) or not math.isfinite(value):
                raise StoreError('旧グラフ書式の数値が不正です。')
            values[key] = value * .75
        values['preset_name'] = '移行した書式'
    return GraphStyle(**values).validate()


def save_style(style, path=None):
    style.validate()
    path = Path(path) if path is not None else style_path()
    data = asdict(style)
    # Axis names and aliases are graph-specific; never reuse them on another unit.
    for name in PER_GRAPH_FIELDS:
        data.pop(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(identifier() + '.pending')
    with temporary.open('x', encoding='utf-8') as stream:
        json.dump({'version': 2, 'style': data}, stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
