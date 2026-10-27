import unittest
from types import SimpleNamespace

from .app import App


class SearchDuringSaveTests(unittest.TestCase):
    def test_pending_search_waits_for_save_and_refresh_before_redrawing(self):
        calls, scheduled = [], []
        app = App.__new__(App)
        app.job = object()
        app.focus_sample_id = 'saved-sample'
        app.focus_measurement_id = 'saved-measurement'
        app.root = SimpleNamespace(after=lambda delay, callback: scheduled.append(callback) or 'timer')
        app.render_samples = lambda: calls.append((app.focus_sample_id, app.focus_measurement_id))
        app.run_search()
        self.assertFalse(calls)
        self.assertEqual(app.focus_measurement_id, 'saved-measurement')
        app.job = None
        scheduled.pop()()
        self.assertEqual(calls, [('saved-sample', 'saved-measurement')])
