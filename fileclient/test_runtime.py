import json
import gc
import threading
import time
from concurrent.futures import ThreadPoolExecutor
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from . import runtime
from .store import FileStore, StoreError, identifier


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.original_profile_directory = runtime.profile_directory
        temporary = tempfile.TemporaryDirectory(prefix='labnote-profile-test-')
        self.addCleanup(temporary.cleanup)
        self.profile = Path(temporary.name) / 'profile'
        patcher = patch.object(runtime, 'profile_directory', return_value=self.profile)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_import_survives_move_and_stale_sidecar_and_embedded_defaults(self):
        root = self.profile.parent
        configuration = {'version': 1, 'mode': 'production', 'store_id': identifier(),
                         'windows_path': r'\\server\share\data', 'mac_path': ''}
        embedded = root / 'embedded.json'
        embedded.write_text(json.dumps(configuration), encoding='utf-8')
        with patch.object(runtime.sys, 'frozen', True, create=True), patch.object(runtime, 'bundled_deployment_file', return_value=embedded), patch.object(runtime, 'deployment_directory', return_value=root):
            initial = runtime.load_deployment()
            self.assertTrue((self.profile / 'labnote-deployment.json').is_file())
            changed = runtime.save_deployment({**initial, 'windows_path': r'\\server\newshare\data'}, bound=initial)
            (root / 'labnote-deployment.json').write_text(json.dumps(configuration), encoding='utf-8')
            self.assertEqual(runtime.load_deployment(), changed)
            with patch.object(runtime, 'deployment_directory', return_value=root / 'desktop'):
                self.assertEqual(runtime.load_deployment(), changed)

    def test_broken_or_wrong_local_config_is_not_silently_reset(self):
        root = self.profile.parent
        config = {'version': 1, 'mode': 'production', 'store_id': identifier(),
                  'windows_path': r'\\server\share\data', 'mac_path': ''}
        embedded = root / 'embedded.json'
        embedded.write_text(json.dumps(config), encoding='utf-8')
        self.profile.mkdir()
        local = self.profile / 'labnote-deployment.json'
        with patch.object(runtime.sys, 'frozen', True, create=True), patch.object(runtime, 'bundled_deployment_file', return_value=embedded):
            for raw in ('{broken', json.dumps({**config, 'store_id': identifier()})):
                local.write_text(raw, encoding='utf-8')
                with self.assertRaises((ValueError, StoreError)):
                    runtime.load_deployment()
                self.assertEqual(local.read_text(encoding='utf-8'), raw)

    def test_initial_import_does_not_replace_concurrent_existing_settings(self):
        config = {'version': 1, 'mode': 'production', 'store_id': identifier(),
                  'windows_path': r'\\server\share\data', 'mac_path': ''}
        changed = runtime.save_deployment({**config, 'mac_path': '/Volumes/Verified/data'})
        self.assertEqual(runtime.save_deployment(config, bound=config, initial=True), changed)
        self.assertEqual(list(self.profile.glob('*.pending')), [])

    def test_frozen_single_executable_uses_bundled_production_config(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = {'version': 1, 'mode': 'production', 'store_id': identifier(), 'windows_path': r'\\server\share\data', 'mac_path': ''}
            embedded = root / 'embedded.json'
            embedded.write_text(json.dumps(config), encoding='utf-8')
            with patch.object(runtime.sys, 'frozen', True, create=True), patch.object(runtime, 'bundled_deployment_file', return_value=embedded), patch.object(runtime, 'deployment_directory', return_value=root / 'no-sidecar'):
                self.assertEqual(runtime.load_deployment()['store_id'], config['store_id'])

    def test_frozen_config_mismatch_or_mode_downgrade_is_refused(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = {'version': 1, 'mode': 'production', 'store_id': identifier(), 'windows_path': r'\\server\share\data', 'mac_path': ''}
            embedded = root / 'embedded.json'
            embedded.write_text(json.dumps(config), encoding='utf-8')
            with patch.object(runtime.sys, 'frozen', True, create=True), patch.object(runtime, 'bundled_deployment_file', return_value=embedded), patch.object(runtime, 'deployment_directory', return_value=root):
                for change in ({'store_id': identifier()}, {'mode': 'development'}):
                    (root / 'labnote-deployment.json').write_text(json.dumps({**config, **change}), encoding='utf-8')
                    with self.assertRaises(StoreError):
                        runtime.load_deployment()

    def test_frozen_missing_all_config_cannot_silently_start_demo(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with patch.object(runtime.sys, 'frozen', True, create=True), patch.object(runtime, 'bundled_deployment_file', return_value=root / 'missing'), patch.object(runtime, 'deployment_directory', return_value=root):
                with self.assertRaises(StoreError):
                    runtime.load_deployment()

    def test_same_production_id_allows_updated_sidecar_paths(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = {'version': 1, 'mode': 'production', 'store_id': identifier(), 'windows_path': r'\\server\share\data', 'mac_path': ''}
            embedded = root / 'embedded.json'
            embedded.write_text(json.dumps(config), encoding='utf-8')
            (root / 'labnote-deployment.json').write_text(json.dumps({**config, 'mac_path': '/Volumes/Verified/data'}), encoding='utf-8')
            with patch.object(runtime.sys, 'frozen', True, create=True), patch.object(runtime, 'bundled_deployment_file', return_value=embedded), patch.object(runtime, 'deployment_directory', return_value=root):
                self.assertEqual(runtime.load_deployment()['mac_path'], '/Volumes/Verified/data')

    def test_production_deployment_wins_over_other_remembered_store(self):
        expected = identifier()
        result = runtime.startup_target({'mode': 'production', 'store_id': expected, 'windows_path': r'\\server\share\data'},
            {'store_id': identifier(), 'store_path': r'C:\old-store'}, platform='win32')
        self.assertEqual(result['path'], r'\\server\share\data')
        self.assertEqual(result['store_id'], expected)
        self.assertTrue(result['production'])

    def test_production_mac_without_path_requests_selection_and_never_demo(self):
        expected = identifier()
        deployment = {'mode': 'production', 'store_id': expected, 'windows_path': r'\\server\share\data', 'mac_path': ''}
        result = runtime.startup_target(deployment, {'store_id': identifier(), 'store_path': '/old/demo'}, platform='darwin')
        self.assertTrue(result['select_required'])
        self.assertIsNone(result['path'])
        remembered = runtime.startup_target(deployment, {'store_id': expected, 'store_path': '/Volumes/Actual/data'}, platform='darwin')
        self.assertEqual(remembered['path'], '/Volumes/Actual/data')
        self.assertFalse(remembered['select_required'])

    def test_explicit_path_cannot_override_production_database_identity(self):
        expected = identifier()
        result = runtime.startup_target({'mode': 'production', 'store_id': expected}, {}, explicit_store='other')
        self.assertEqual(result['store_id'], expected)

    def test_explicit_demo_is_separate_and_unbound(self):
        result = runtime.startup_target({'mode': 'production', 'store_id': identifier()}, {}, demo=True)
        self.assertFalse(result['production'])
        self.assertIsNone(result['store_id'])

    def test_relative_remembered_production_mac_path_is_not_used(self):
        expected = identifier()
        result = runtime.startup_target({'mode': 'production', 'store_id': expected},
            {'store_id': expected, 'store_path': 'relative-folder'}, platform='darwin')
        self.assertTrue(result['select_required'])

    def test_mac_can_remember_verified_mount_name_for_same_database(self):
        expected = identifier()
        result = runtime.startup_target({'mode': 'production', 'store_id': expected, 'mac_path': '/Volumes/Public/data'},
            {'store_id': expected, 'store_path': '/Volumes/Public-1/data'}, platform='darwin')
        self.assertEqual(result['path'], '/Volumes/Public-1/data')

    def test_windows_uses_per_user_data_directory(self):
        with patch.object(runtime.sys, 'platform', 'win32'), patch.dict(runtime.os.environ, {'LOCALAPPDATA': 'C:/Profile/Local'}):
            self.assertEqual(self.original_profile_directory(), Path('C:/Profile/Local/LabNotePortfolio'))

    def test_mac_uses_application_support(self):
        with patch.object(runtime.sys, 'platform', 'darwin'), patch.object(runtime.Path, 'home', return_value=Path('/Users/demo')):
            self.assertEqual(self.original_profile_directory(), Path('/Users/demo/Library/Application Support/LabNotePortfolio'))

    def test_bundled_demo_is_outside_application_bundle(self):
        with patch.object(runtime.sys, 'frozen', True, create=True), patch.object(runtime, 'profile_directory', return_value=Path('/user-data/LabNote')):
            self.assertEqual(runtime.demo_directory(), Path('/user-data/LabNote/demo'))

    def test_preferences_remember_only_path_store_id_and_display_name(self):
        with tempfile.TemporaryDirectory(prefix='labnote-preferences-test-') as temporary:
            root = Path(temporary)
            store = FileStore.create(root / 'store')
            with patch.object(runtime, 'profile_directory', return_value=root / 'profile'):
                runtime.save_preferences(store, '仮担当')
                result = runtime.load_preferences()
                self.assertEqual(result['store_id'], store.store_id)
                self.assertEqual(result['actor'], '仮担当')
                document = json.loads((root / 'profile/settings.json').read_text(encoding='utf-8'))
                self.assertEqual(set(document), {'version', 'store_path', 'store_id', 'actor'})
                runtime.save_preferences(store, '次の表示名')
                self.assertEqual(runtime.load_preferences()['actor'], '次の表示名')

    def test_missing_preferences_do_not_create_files(self):
        with tempfile.TemporaryDirectory(prefix='labnote-preferences-test-') as temporary:
            path = Path(temporary) / 'not-created'
            with patch.object(runtime, 'profile_directory', return_value=path):
                self.assertEqual(runtime.load_preferences(), {})
                self.assertFalse(path.exists())

    def test_malformed_preferences_are_reported(self):
        with tempfile.TemporaryDirectory(prefix='labnote-preferences-test-') as temporary:
            path = Path(temporary)
            (path / 'settings.json').write_text('{bad')
            with patch.object(runtime, 'profile_directory', return_value=path):
                with self.assertRaises(ValueError):
                    runtime.load_preferences()

    def test_callback_exception_uses_dialog_without_standard_streams(self):
        root, reporter = SimpleNamespace(), Mock()
        runtime.install_error_dialog(root, reporter)
        with patch.object(runtime.sys, 'stdout', None), patch.object(runtime.sys, 'stderr', None):
            root.report_callback_exception(ValueError, ValueError('仮のエラー'), None)
        reporter.assert_called_once()
        self.assertIn('仮のエラー', reporter.call_args.args[1])

    def test_resetting_the_same_registrant_id_does_not_schedule_a_redraw(self):
        from .app import App
        # 保存のたびに同じIDが入れ直される。再描画を予約すると、遅れて走って
        # 保存直後の選択を消してしまう。
        calls = []
        app = SimpleNamespace(actor=SimpleNamespace(get=lambda: ' zz00001 '),
                              filtered_actor='zz00001',
                              schedule_search=lambda *a: calls.append(1))
        App.actor_changed(app)
        self.assertEqual(calls, [])
        app.actor = SimpleNamespace(get=lambda: 'zz00002')
        App.actor_changed(app)
        self.assertEqual(calls, [1])
        self.assertEqual(app.filtered_actor, 'zz00002')

    def test_sample_list_scope_matches_creator_or_registrant(self):
        from .app import App
        item = {'registrant': 'zz00001', 'data': {'creator': 'ZZ00002'}}
        self.assertTrue(App.belongs_to(item, 'zz00001'))
        self.assertTrue(App.belongs_to(item, 'zz00002'))
        self.assertFalse(App.belongs_to(item, 'nb23102'))
        self.assertTrue(App.belongs_to({'registrant': 'zz00001', 'data': {}}, 'zz00001'))
        self.assertFalse(App.belongs_to({'registrant': 'zz00001', 'data': {}}, ''))

    def test_sample_list_explains_why_it_is_empty_or_unfiltered(self):
        from .app import App, SCOPE_ALL, SCOPE_MINE
        app = SimpleNamespace(scope=SimpleNamespace(get=lambda: SCOPE_MINE), sample_items={})
        self.assertIn('登録者IDが未入力', App.sample_list_hint(app, 0, ''))
        hint = App.sample_list_hint(app, 3, 'zz00001')
        self.assertNotIn('検索条件', hint)
        searching = App.sample_list_hint(app, 3, 'zz00001', True)
        self.assertIn('検索条件に一致する', searching)
        self.assertIn('3件', searching)
        self.assertIn('3件', hint)
        self.assertIn(SCOPE_ALL, hint)
        app.sample_items = {'key': 'value'}
        self.assertIn('試料を選択してください', App.sample_list_hint(app, 3, 'zz00001'))
        everyone = SimpleNamespace(scope=SimpleNamespace(get=lambda: SCOPE_ALL), sample_items={})
        self.assertIn('試料を選択してください', App.sample_list_hint(everyone, 0, ''))

    def test_worker_cycles_are_finalized_on_ui_thread_and_gc_restored(self):
        from .app import App
        main_thread = threading.get_ident()
        finalized, callbacks = [], []
        class Cycle:
            def __init__(self):
                self.self = self
            def __del__(self):
                finalized.append(threading.get_ident())
        app = SimpleNamespace(job=None, status=Mock(), root=SimpleNamespace(after=lambda delay, callback: callbacks.append(callback)), show_error=Mock())
        enabled = gc.isenabled()
        with ThreadPoolExecutor(max_workers=1) as executor:
            app.executor = executor
            def work():
                cycle = Cycle()
                return threading.get_ident()
            completed = []
            App.run_job(app, 'test', work, completed.append)
            deadline = time.monotonic()+5
            while app.job is not None and time.monotonic() < deadline:
                callbacks.pop(0)()
                time.sleep(.001)
            self.assertIsNone(app.job)
            self.assertNotEqual(completed, [main_thread])
            self.assertEqual(finalized, [main_thread])
            self.assertEqual(gc.isenabled(), enabled)
