import unittest
from check_public import inspect_file


class PublicPolicyTests(unittest.TestCase):
    def test_forced_ignored_data_and_local_configuration_are_rejected(self):
        for path in ['labnote-deployment.json', 'fileclient/labnote-deployment.json',
                     'var/records.json', 'fileclient/credentials.json', '.env', 'work/report.json']:
            with self.subTest(path=path):
                self.assertTrue(inspect_file(path, '{}'))

    def test_hidden_private_address_is_rejected(self):
        self.assertTrue(inspect_file('README.md', '172.' + '20.1.2'))

    def test_non_example_email_is_rejected_without_echoing_it(self):
        value = 'someone@' + 'private.invalid'
        errors = inspect_file('README.md', value)
        self.assertTrue(errors)
        self.assertNotIn(value, str(errors))

    def test_public_example_and_legacy_import_policy(self):
        self.assertEqual(inspect_file('labnote-deployment.example.json', '{}'), [])
        self.assertTrue(inspect_file('fileclient/legacy.py', 'from django import forms'))
