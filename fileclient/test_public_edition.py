import sys
import unittest
from unittest.mock import patch
from . import runtime


class PublicEditionTests(unittest.TestCase):
    def test_profile_is_separate_from_original_application(self):
        for platform in ('win32', 'darwin', 'linux'):
            with self.subTest(platform=platform), patch.object(sys, 'platform', platform):
                self.assertEqual(runtime.profile_directory().name, 'LabNotePortfolio')

    def test_default_entry_selects_demo_before_loading_deployment(self):
        from . import app
        # Stop after routing is established, before any GUI or store mutation.
        class StopRouting(Exception):
            pass
        with patch.object(sys, 'argv', ['labnote']), patch.object(app.tk, 'Tk'), \
                patch.object(app, 'load_preferences', return_value={}), \
                patch.object(app, 'load_deployment') as load_deployment, \
                patch.object(app, 'load_catalog', side_effect=StopRouting) as catalog:
            with self.assertRaises(StopRouting):
                app.main()
            load_deployment.assert_not_called()
            catalog.assert_called_once_with({})
