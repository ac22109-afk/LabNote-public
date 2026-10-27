"""Per-user desktop configuration; research cache is separate, no credentials."""
import json
import os
import sys
import uuid
from pathlib import Path, PureWindowsPath, PurePosixPath


def profile_directory():
    if sys.platform == 'win32':
        return Path(os.environ.get('LOCALAPPDATA') or Path.home() / 'AppData/Local') / 'LabNotePortfolio'
    if sys.platform == 'darwin':
        return Path.home() / 'Library/Application Support/LabNotePortfolio'
    return Path.home() / '.local/share/LabNotePortfolio'


def demo_directory():
    if getattr(sys, 'frozen', False):
        return profile_directory() / 'demo'
    return Path(__file__).resolve().parent.parent / 'var/file-client-demo'


def load_preferences():
    path = profile_directory() / 'settings.json'
    if not path.exists():
        return {}
    with path.open('rb') as stream:
        raw = stream.read(16385)
    if len(raw) > 16384:
        raise ValueError('保存先の設定が大きすぎます。')
    data = json.loads(raw.decode('utf-8'))
    if not isinstance(data, dict) or data.get('version') != 1:
        raise ValueError('保存先の設定の形式が違います。')
    for key in ['store_path', 'store_id', 'actor']:
        if not isinstance(data.get(key), str):
            raise ValueError('保存先の設定を読み取れません。')
    if not data['store_path'] or len(data['actor']) > 150:
        raise ValueError('保存先の設定を確認してください。')
    return {key: data[key] for key in ['store_path', 'store_id', 'actor']}


def save_preferences(store, actor):
    home = profile_directory()
    home.mkdir(parents=True, exist_ok=True)
    temporary = home / f'settings-{uuid.uuid4()}.pending'
    data = {'version': 1, 'store_path': str(store.root), 'store_id': store.store_id,
            'actor': str(actor).strip()[:150]}
    with temporary.open('x', encoding='utf-8') as stream:
        json.dump(data, stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    # This is the application's own local preference file, not a record file.
    os.replace(temporary, home / 'settings.json')


def install_error_dialog(root, reporter):
    def report(exception_type, value, traceback):
        reporter('操作を完了できませんでした',
                 '画面の処理で問題が発生しました。保存中だった場合は、保存状態を確認してから再操作してください。'
                 f'\n\n詳細: {value}', parent=root)
    root.report_callback_exception = report


def deployment_directory():
    if not getattr(sys, 'frozen', False):
        return Path(__file__).resolve().parent.parent
    executable = Path(sys.executable).resolve()
    if sys.platform == 'darwin':
        for parent in executable.parents:
            if parent.suffix == '.app':
                return parent.parent
    return executable.parent


def bundled_deployment_file():
    return Path(__file__).with_name('labnote-deployment.json')


def validate_deployment(data, bound=None):
    from .catalog import Catalog
    from .store import StoreError, uid
    if not isinstance(data, dict) or data.get('version') != 1:
        raise StoreError('配布設定の形式が違います。担当者に確認してください。')
    if data.get('mode', 'development') not in {'development', 'production'}:
        raise StoreError('配布設定の運用モードを確認してください。')
    uid(data.get('store_id'))
    data = dict(data)
    if bound is not None:
        if not isinstance(bound, dict) or bound.get('mode') != 'production':
            raise StoreError('アプリに組み込まれた本番設定が不正です。')
        uid(bound.get('store_id'))
        if data.get('mode') != 'production' or data['store_id'] != bound['store_id']:
            raise StoreError('配布設定のデータID・運用モードがアプリの本番設定と一致しません。接続しません。')
    for key in ['windows_path', 'mac_path']:
        if not isinstance(data.get(key), str):
            raise StoreError('配布設定の保存先を確認してください。')
    if (data['windows_path'] and not PureWindowsPath(data['windows_path']).is_absolute()) or (
            data['mac_path'] and not PurePosixPath(data['mac_path']).is_absolute()):
        raise StoreError('配布設定にはOSごとの絶対パスが必要です。')
    if not data['windows_path'] and not data['mac_path']:
        raise StoreError('配布設定に共有先がありません。')
    data['catalog'] = Catalog(data.get('catalog')).data
    return data


def save_deployment(data, *, bound=None, profile=None, initial=False):
    """Persist validated settings only; no research files or credentials."""
    data = validate_deployment(data, bound)
    home = Path(profile) if profile is not None else profile_directory()
    home.mkdir(parents=True, exist_ok=True)
    path = home / 'labnote-deployment.json'
    temporary = home / f'deployment-{uuid.uuid4()}.pending'
    with temporary.open('x', encoding='utf-8') as stream:
        json.dump(data, stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    if initial:
        # Another first launch must not overwrite settings imported concurrently.
        try:
            os.link(temporary, path)
        except FileExistsError:
            from .store import read_json
            existing, _ = read_json(path)
            return validate_deployment(existing, bound)
        finally:
            temporary.unlink()  # Own staging file only.
    else:
        os.replace(temporary, path)
    return data


def load_deployment(*, persist=True, profile=None):
    from .store import StoreError, read_json
    external = deployment_directory() / 'labnote-deployment.json'
    embedded = bundled_deployment_file() if getattr(sys, 'frozen', False) else None
    bound = None
    if embedded is not None and embedded.exists():
        bound, _ = read_json(embedded)
        validate_deployment(bound, bound)
    local = (Path(profile) if profile is not None else profile_directory()) / 'labnote-deployment.json'
    # A copied or older sidecar never silently replaces a maintainer's local edits.
    path = local if persist and local.exists() else external if external.exists() else embedded
    if path is None or not path.exists():
        if getattr(sys, 'frozen', False):
            raise StoreError('本番の配布設定が見つかりません。担当者から完全な配布物を取得してください。')
        return {}
    data, _ = read_json(path)
    data = validate_deployment(data, bound)
    if persist and path != local:
        return save_deployment(data, bound=bound, profile=profile, initial=True)
    return data


def startup_target(deployment, preferences, *, platform=None, demo=False, explicit_store=None, demo_path=None):
    """Production binding wins over old preferences; never silently open a demo."""
    if demo:
        return {'path': None, 'store_id': None, 'production': False, 'select_required': False}
    platform = platform or sys.platform
    configured = deployment.get('mac_path' if platform == 'darwin' else 'windows_path', '')
    production = deployment.get('mode') == 'production'
    if production:
        expected = deployment['store_id']
        remembered = preferences.get('store_path') if preferences.get('store_id') == expected else None
        if remembered and not (PurePosixPath(remembered).is_absolute() if platform == 'darwin' else PureWindowsPath(remembered).is_absolute()):
            remembered = None
        path = (explicit_store or ((remembered or configured) if platform == 'darwin' else (configured or remembered))) or None
        return {'path': path, 'store_id': expected, 'production': True, 'select_required': not path}
    use_deployment = bool(configured and (not preferences.get('store_path') or Path(preferences['store_path']) == (demo_path or demo_directory())))
    return {'path': explicit_store or (configured if use_deployment else preferences.get('store_path')),
            'store_id': None if explicit_store else deployment.get('store_id') if use_deployment else preferences.get('store_id'),
            'production': False, 'select_required': False}


def load_catalog(deployment=None):
    from .catalog import Catalog
    from .store import read_json
    path = profile_directory() / 'catalog.json'
    if path.exists():
        data, _ = read_json(path)
        return Catalog(data)
    return Catalog((deployment or {}).get('catalog'))


def save_catalog(catalog):
    home = profile_directory()
    home.mkdir(parents=True, exist_ok=True)
    temporary = home / f'catalog-{uuid.uuid4()}.pending'
    with temporary.open('x', encoding='utf-8') as stream:
        json.dump(catalog.data, stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, home / 'catalog.json')
