"""Synthetic release fixtures; no real application records are read."""
import io
import subprocess
import sys
import tempfile
import unittest
import warnings
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "check_release_privacy.py"


class ReleasePrivacyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def run_scan(self, path, good=True):
        result = subprocess.run([sys.executable, str(SCRIPT), str(path)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0 if good else 1, result.stdout + result.stderr)
        return result

    def write(self, name, data):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data.encode() if isinstance(data, str) else data)
        return path

    def archive(self, members):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as archive:
            for name, data in members.items():
                archive.writestr(name, data)
        return buf.getvalue()

    def bundle(self, module_text='VALUE = "public"', data=None, links=()):
        from PyInstaller.archive.writers import CArchiveWriter, ZlibArchiveWriter
        source = self.write('module.py', module_text)
        pyz = self.root / 'bundle.pyz'
        ZlibArchiveWriter(str(pyz), [('local_app.fixture', str(source), 'PYMODULE')],
                          {'local_app.fixture': compile(module_text, 'local_app/fixture.py', 'exec')})
        entries = [('PYZ.pyz', str(pyz), True, 'z')]
        if data:
            target, value = data
            resource = self.write('resource.txt', value)
            entries.append((target, str(resource), True, 'x'))
        entries.extend((name, target, False, 'n') for name, target in links)
        archive = self.root / 'program.command'
        CArchiveWriter(str(archive), entries, 'libpython.so')
        return archive

    def test_selftest(self):
        result = subprocess.run([sys.executable, str(SCRIPT), '--selftest'], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('selftest passed', result.stdout)

    def test_public_source_and_nested_docx_are_allowed(self):
        document = self.archive({'[Content_Types].xml': '<Types/>', 'word/document.xml': '<p>Public manual</p>'})
        source = self.archive({'local_app/schema.sql': 'CREATE TABLE example (id INTEGER);',
                               'manual.docx': document})
        self.run_scan(self.write('release.zip', source))

    def test_nested_zip_cannot_hide_runtime_data(self):
        nested = self.archive({'local_app/data/record.db': b'SQLite format 3\x00'})
        self.run_scan(self.write('release.zip', self.archive({'inside.zip': nested})), good=False)

    def test_rejects_media_unknown_binary_and_broken_archive(self):
        for name, data in [('recording.wav', b'RIFF'), ('patient.jpg', b'\xff\xd8\xff'),
                           ('unknown.bin', b'\x00\x01\xff'), ('broken.zip', b'not a zip')]:
            with self.subTest(name=name):
                self.run_scan(self.write(name, data), good=False)

    def test_rejects_zip_traversal_symlinks_and_encryption(self):
        self.run_scan(self.write('bad.zip', self.archive({'../outside.txt': 'x'})), good=False)
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, 'w') as archive:
            entry = zipfile.ZipInfo('link.txt')
            entry.create_system = 3
            entry.external_attr = 0o120777 << 16
            archive.writestr(entry, 'target.txt')
        self.run_scan(self.write('symbolic.zip', buf.getvalue()), good=False)
        encrypted = bytearray(self.archive({'public.txt': 'public'}))
        central = encrypted.index(b'PK\x01\x02')
        encrypted[central + 8] |= 1
        self.run_scan(self.write('encrypted.zip', encrypted), good=False)
        target = self.write('target.txt', 'public')
        link = self.root / 'link.txt'
        link.symlink_to(target)
        self.run_scan(link, good=False)

    def test_sensitive_paths_and_credentials_fail_without_echoing_values(self):
        secret = 'ghp_' + 'x' * 36
        home = '/' + 'Users/sample-user/private'
        for value in [secret, home, '-----BEGIN ' + 'PRIVATE KEY-----', '"api_key": "' + 'x' * 30 + '"']:
            with self.subTest(kind=value[:8]):
                result = self.run_scan(self.write('private.txt', value), good=False)
                self.assertNotIn(value, result.stdout + result.stderr)

    def test_only_exact_synthetic_ip_fixture_is_allowed(self):
        ip = '.'.join(['10', '0', '0', '9'])
        self.write('test/test_access_session_service.py', 'CURRENT_IP = "' + ip + '"\n')
        self.run_scan(self.root)
        self.run_scan(self.write('local_app/settings.py', 'HOST = "' + ip + '"\n'), good=False)
        self.run_scan(self.write('test/test_access_session_service.py', 'OTHER = "' + ip + '"\n'), good=False)

    def test_pyz_code_constants_and_carchive_data_are_inspected(self):
        secret = 'ghp_' + 'x' * 36
        self.run_scan(self.bundle(), good=True)
        self.run_scan(self.bundle('def nested():\n    return "' + secret + '"'), good=False)
        self.run_scan(self.bundle(data=('local_app/config.json', '{"token":"' + secret + '"}')), good=False)

    def test_onefile_inside_zip_is_inspected(self):
        path = self.bundle(data=('local_app/data/fixture.db', 'synthetic'))
        self.run_scan(self.write('release.zip', self.archive({'program.command': path.read_bytes()})), good=False)

    def test_broken_onefile_and_pyz_fail_closed(self):
        self.run_scan(self.write('program.exe', b'MZ' + b'\0' * 100), good=False)
        self.run_scan(self.write('broken.pyz', b'PYZ\0broken'), good=False)

    def test_third_party_author_and_ci_build_path_are_allowed_but_secrets_are_not(self):
        path = self.bundle(data=('vendor/metadata.txt', 'Author: public@example.org\n' + '/'.join(('', 'home', 'runner', 'work', 'repo', 'repo', 'file.py'))))
        self.run_scan(path)
        path = self.bundle(data=('vendor/metadata.txt', 'ghp_' + 'x' * 36))
        self.run_scan(path, good=False)

    def test_public_static_icon_allowed_but_private_media_not(self):
        self.run_scan(self.bundle(data=('local_app/static/icon-180.png', b'\x89PNG\r\n\x1a\npublic')))
        self.run_scan(self.bundle(data=('local_app/static/patient.png', b'\x89PNG\r\n\x1a\nsynthetic')), good=False)

    def test_windows_archive_paths_and_safe_framework_links(self):
        self.run_scan(self.bundle(data=('local_app\\schema.sql', 'CREATE TABLE sample (id INT);')))
        self.run_scan(self.bundle(data=('Framework/Versions/3.12/public.txt', 'public'), links=(
            ('Framework/Versions/Current', '3.12'), ('Framework/public.txt', 'Versions/Current/public.txt'))))
        self.run_scan(self.bundle(links=(('bad', '../../outside'),)), good=False)

    def test_disguised_database_and_binary_text_are_rejected(self):
        self.run_scan(self.write('readme.txt', b'SQLite format 3\x00synthetic'), good=False)
        self.run_scan(self.write('data.txt', b'public\x00binary'), good=False)

    def test_empty_directory_missing_input_and_zip_duplicate_fail_closed(self):
        self.run_scan(self.root, good=False)
        self.run_scan(self.root / 'missing.zip', good=False)
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, 'w') as archive:
            archive.writestr('same.txt', 'first')
            with warnings.catch_warnings():
                warnings.simplefilter('ignore', UserWarning)
                archive.writestr('same.txt', 'second')
        self.run_scan(self.write('duplicate.zip', buf.getvalue()), good=False)


if __name__ == '__main__':
    unittest.main()
