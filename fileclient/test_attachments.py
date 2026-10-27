import base64
import tempfile
import unittest
from pathlib import Path

from .attachments import original_filename, original_extension, export_destination
from .store import FileStore
from .session import DesktopSession


class AttachmentTests(unittest.TestCase):
    def test_restore_names_and_supply_missing_extension_without_conversion(self):
        for name, extension in [('画像.PNG', '.PNG'), ('装置データ.tar.gz', '.tar.gz'), ('README', ''), ('.notes', '')]:
            with self.subTest(name=name):
                attachment = {'name': name}
                self.assertEqual(original_extension(attachment), extension)
                self.assertEqual(export_destination('renamed', attachment).name, 'renamed' + extension)
                self.assertEqual(export_destination(name, attachment).name, name)
                self.assertEqual(export_destination('explicit.txt', attachment).name, 'explicit.txt')
        self.assertEqual(original_filename({'name': r'C:\old\画像.png'}), '画像.png')

    def test_png_survives_offline_queue_sync_fresh_client_and_export(self):
        png = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aG1sAAAAASUVORK5CYII=')
        with tempfile.TemporaryDirectory(prefix='labnote-png-') as temporary:
            root = Path(temporary)
            shared = FileStore.create(root / 'shared')
            source = root / '日本語画像.PNG'
            source.write_bytes(png)
            client = DesktopSession(shared.root, shared.store_id, cache_home=root / 'pc-a')
            item = client.save('sample', {'label': 'PNG-TEST', 'creator': 'zz00001',
                              'material': 'アルミニウム合金', 'alloy': 'A1050'}, 'zz00001', files=[source])
            self.assertEqual(item['attachments'][0]['name'], source.name)
            self.assertEqual(client.sync(), (1, [], 0))
            other = DesktopSession(shared.root, shared.store_id, cache_home=root / 'pc-b')
            restored = other.load().current(item['id'])
            attachment = restored['attachments'][0]
            destination = export_destination(root / '取り出した画像', attachment)
            other.export_attachment(restored, attachment['id'], destination)
            self.assertEqual(destination.suffix, '.PNG')
            self.assertEqual(destination.read_bytes(), png)
            # Schema 1 already includes the full name: old revisions need no migration.
            self.assertNotIn('extension', attachment)


if __name__ == '__main__':
    unittest.main()
