"""Bounded, read-only Excel/CSV input shared by import and curve tools."""
import csv
import hashlib
import io
import zipfile
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from .store import StoreError, normalized


@dataclass(frozen=True)
class Table:
    headers: tuple
    rows: tuple
    sha256: str
    sheet: str = ''
    encoding: str = ''


def cell_text(value):
    if value is None:
        return ''
    if isinstance(value, (datetime, date)):
        return value.date().isoformat() if isinstance(value, datetime) else value.isoformat()
    return str(value).strip()


def read_table(path, *, sheet=None, max_rows=10000):
    path = Path(path)
    with path.open('rb') as stream:
        raw = stream.read(32 * 1024**2 + 1)
    if len(raw) > 32 * 1024**2:
        raise StoreError('入力ファイルは32MiBまでです。分割してください。')
    encoding, sheet_name = '', ''
    if path.suffix.lower() == '.xlsx':
        import openpyxl
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            if sum(item.file_size for item in archive.infolist()) > 128 * 1024**2:
                raise StoreError('Excelの展開後サイズが大きすぎます。必要な表だけ別ファイルにしてください。')
        workbook = openpyxl.load_workbook(io.BytesIO(raw), read_only=True, data_only=False, keep_links=False)
        try:
            worksheet = workbook[sheet] if sheet else workbook.worksheets[0]
            sheet_name = worksheet.title
            worksheet.reset_dimensions()
            rows = []
            for cells in worksheet.iter_rows():
                if len(cells) > 128:
                    raise StoreError('列数は128列までです。不要な列を除いてください。')
                if any(cell.data_type == 'f' for cell in cells):
                    raise StoreError('数式セルがあります。Excelで値として貼り付けたファイルを使ってください。')
                rows.append(tuple(cell_text(cell.value) for cell in cells))
                if len(rows) > max_rows + 1:
                    raise StoreError(f'データは{max_rows}行までです。')
        finally:
            workbook.close()
    elif path.suffix.lower() in {'.csv', '.tsv', '.txt'}:
        for candidate in ('utf-8-sig', 'cp932', 'utf-16'):
            try:
                content = raw.decode(candidate)
                encoding = candidate
                break
            except UnicodeError:
                continue
        else:
            raise StoreError('文字コードを読み取れません。UTF-8のCSVとして保存してください。')
        try:
            dialect = csv.Sniffer().sniff(content[:65536], delimiters=',\t;')
        except csv.Error:
            dialect = csv.excel_tab if path.suffix.lower() == '.tsv' else csv.excel
        rows = []
        for row in csv.reader(io.StringIO(content, newline=''), dialect):
            if len(row) > 128:
                raise StoreError('列数は128列までです。')
            rows.append(tuple(value.strip() for value in row))
            if len(rows) > max_rows + 1:
                raise StoreError(f'データは{max_rows}行までです。')
    else:
        raise StoreError('対応形式は.xlsx、.csv、.tsv、.txtです。.xlsは.xlsxとして保存してください。')
    while rows and not any(rows[-1]):
        rows.pop()
    if not rows or not any(rows[0]):
        raise StoreError('先頭行に列名を入れてください。')
    width = max((len(row) for row in rows), default=0)
    if any(len(value) > 10000 for row in rows for value in row):
        raise StoreError('1セルは10000文字までです。')
    header = list(rows[0]) + [''] * (width - len(rows[0]))
    header = tuple(value or f'列{i+1}' for i, value in enumerate(header))
    if len({normalized(value) for value in header}) != len(header):
        raise StoreError('重複する列名があります。異なる列名にしてください。')
    padded = tuple(tuple(row) + ('',) * (width - len(row)) for row in rows[1:])
    return Table(header, padded, hashlib.sha256(raw).hexdigest(), sheet_name, encoding)


def sheet_names(path):
    if Path(path).suffix.lower() != '.xlsx':
        return ()
    with Path(path).open('rb') as stream:
        raw = stream.read(32 * 1024**2 + 1)
    if len(raw) > 32 * 1024**2:
        raise StoreError('入力ファイルは32MiBまでです。')
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        if sum(item.file_size for item in archive.infolist()) > 128 * 1024**2:
            raise StoreError('Excelの展開後サイズが大きすぎます。')
    import openpyxl
    workbook = openpyxl.load_workbook(io.BytesIO(raw), read_only=True, data_only=False, keep_links=False)
    try:
        return tuple(workbook.sheetnames)
    finally:
        workbook.close()
