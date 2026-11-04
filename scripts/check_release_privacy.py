#!/usr/bin/env python3
"""Fail-closed release check. No private dictionaries or application data required.

PyInstaller archives must be checked with the build's Python minor version and
PyInstaller installed. The guard never executes bundled code. All archive members
are inspected, including nested ZIP/OOXML, CArchive and PYZ code constants.
This generic gate complements the publisher's private pre-release review.
"""
import argparse
import io
import ipaddress
import marshal
import posixpath
import re
import stat
import sys
import tempfile
import types
import zipfile
from pathlib import Path, PurePosixPath


class Rejected(Exception):
    pass


CREDENTIAL = re.compile(
    r'-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----|'
    r'\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,}|'
    r'AKIA[A-Z0-9]{16}|ASIA[A-Z0-9]{16}|sk-[A-Za-z0-9_-]{24,}|'
    r'xox[baprs]-[A-Za-z0-9-]{20,})\b|'
    r'''(?i:["']?(?:api[_-]?key|access[_-]?token|client[_-]?secret|password)["']?\s*[:=]\s*["'][A-Za-z0-9+/=_-]{20,}["'])'''
)
HOME = re.compile(r'(?:/(?:Users|home)/|[A-Za-z]:[\\/]Users[\\/])[^\s\x00\'"<>]+')
CI_HOME = re.compile(r'(?:/(?:Users|home)/runner/work/|[A-Za-z]:[\\/]a[\\/])')
IP = re.compile(r'(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])')
RUNTIME = {'.db', '.sqlite', '.sqlite3', '.db3', '.db-wal', '.db-shm', '.bak',
           '.wav', '.mp3', '.m4a', '.aac', '.flac', '.ogg', '.mp4', '.mov',
           '.avi', '.dcm', '.dicom', '.xlsx', '.xls', '.csv', '.log', '.key', '.p12', '.pfx'}
IMAGES = {'.png', '.jpg', '.jpeg', '.webp', '.gif', '.heic', '.tiff', '.bmp', '.ico'}
PUBLIC_IMAGES = {'local_app/static/icon-180.png', 'local_app/static/icon.svg'}
TEXT = {'.py', '.js', '.css', '.html', '.svg', '.sql', '.json', '.xml', '.rels',
        '.md', '.txt', '.toml', '.ini', '.cfg', '.yaml', '.yml', '.sh', '.bat',
        '.ps1', '.rst', '.spec', '.pth', '.typed', '.pem'}
NATIVE = {'.dll', '.dylib', '.so', '.pyd'}
MAX_BYTES = 4 * 1024 ** 3
MAX_ENTRIES = 100000
MAX_DEPTH = 12


def safe_name(name):
    value = name.replace('\\', '/')
    path = PurePosixPath(value)
    if not value or '\x00' in value or path.is_absolute() or '..' in path.parts or re.match(r'^[A-Za-z]:', value):
        raise Rejected('unsafe archive path')
    return value


def first_party(name):
    normalized = name.replace('\\', '/')
    return (normalized.startswith(('local_app/', 'local_app.', 'test/', 'scripts/'))
            or normalized in {'desktop_launcher', 'desktop_launcher.py'})


class Scanner:
    def __init__(self):
        self.entries = 0
        self.total = 0
        self.onefiles = 0

    def text(self, value, strict, name):
        if CREDENTIAL.search(value):
            raise Rejected('credential or private key')
        if not strict:
            return
        for match in HOME.finditer(value):
            if not CI_HOME.match(match.group()):
                raise Rejected('personal home path')
        for match in IP.finditer(value):
            try:
                address = ipaddress.ip_address(match.group())
            except ValueError:
                continue
            private = any(address in ipaddress.ip_network(net) for net in ((0x0A000000, 8), (0xAC100000, 12), (0xC0A80000, 16)))
            if not private:
                continue
            # One documented synthetic fixture, restricted to its exact assignment.
            line = value[value.rfind('\n', 0, match.start()) + 1:value.find('\n', match.end()) if '\n' in value[match.end():] else len(value)]
            fixture = 'CURRENT_IP = "' + '.'.join(('10', '0', '0', '9')) + '"'
            if name == 'test/test_access_session_service.py' and line == fixture:
                continue
            raise Rejected('private network address')

    def code(self, code, strict, name):
        if not isinstance(code, types.CodeType):
            raise Rejected('invalid Python code archive')
        self.text(code.co_filename, strict, name)
        for identifier in code.co_names + code.co_varnames:
            self.text(identifier, strict, name)
        for constant in code.co_consts:
            self.constant(constant, strict, name)

    def constant(self, value, strict, name):
        if isinstance(value, types.CodeType):
            self.code(value, strict, name)
        elif isinstance(value, str):
            self.text(value, strict, name)
        elif isinstance(value, bytes):
            self.text(value.decode('latin1'), strict, name)
        elif isinstance(value, (tuple, frozenset)):
            for part in value:
                self.constant(part, strict, name)

    def path_rules(self, name, document):
        self.text(name, True, name)
        suffix = PurePosixPath(name).suffix.lower()
        if suffix in RUNTIME or name.lower().endswith(('-wal', '-shm')):
            raise Rejected('runtime data or credential file')
        parts = {part.lower() for part in PurePosixPath(name).parts}
        if parts & {'uploads', 'recordings', 'backups', 'patient_images', 'raw_audio'} or name.startswith('local_app/data/'):
            raise Rejected('runtime data directory')
        if suffix in IMAGES and name not in PUBLIC_IMAGES and not (document and name.startswith('word/media/')):
            raise Rejected('unapproved media')

    def scan(self, data, name, depth=0, strict=True, document=False, native=False):
        name = safe_name(name)
        self.entries += 1
        self.total += len(data)
        if depth > MAX_DEPTH or self.entries > MAX_ENTRIES or self.total > MAX_BYTES:
            raise Rejected('archive inspection limit exceeded')
        self.path_rules(name, document)
        self.text(data.decode('latin1'), False, name)
        suffix = PurePosixPath(name).suffix.lower()
        if data.startswith(b'SQLite format 3\x00'):
            raise Rejected('database content')
        if suffix in {'.zip', '.docx'} or data.startswith(b'PK\x03\x04'):
            self.zip(data, depth, suffix == '.docx', strict)
        elif suffix == '.pyz' or data.startswith(b'PYZ\x00'):
            self.pyz(data, depth)
        elif suffix in {'.exe', '.command'} or b'MEI\x0c\x0b\x0a\x0b\x0e' in data:
            self.carchive(data, depth)
        elif suffix == '.pyc':
            self.code(marshal.loads(data[16:]), strict, name)
        elif native or suffix in NATIVE or '.so.' in name:
            # Native dependencies are opaque machine code; scan their complete bytes
            # for credential shapes, without treating library authors as user data.
            self.text(data.decode('latin1'), strict, name)
        elif suffix in IMAGES:
            pass  # Only explicit public assets passed path_rules above.
        elif suffix in TEXT or not data or (b'\x00' not in data and self.utf8(data)):
            if b'\x00' in data:
                raise Rejected('binary content in text file')
            self.text(data.decode('utf-8-sig'), strict, name)
        elif name.startswith(('tzdata/zoneinfo/', 'zoneinfo/')) and data.startswith(b'TZif'):
            pass
        else:
            raise Rejected('unknown binary format')

    @staticmethod
    def utf8(data):
        try:
            data.decode('utf-8-sig')
            return True
        except UnicodeDecodeError:
            return False

    def zip(self, data, depth, document, strict):
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            names = set()
            members = archive.infolist()
            if not members:
                raise Rejected('empty archive')
            if document and not {'[Content_Types].xml', 'word/document.xml'} <= set(archive.namelist()):
                raise Rejected('invalid Word document')
            for info in members:
                name = safe_name(info.filename)
                if name in names or stat.S_ISLNK(info.external_attr >> 16) or info.flag_bits & 1:
                    raise Rejected('duplicate, symbolic or encrypted archive member')
                names.add(name)
                if info.is_dir():
                    continue
                if info.file_size > MAX_BYTES - self.total:
                    raise Rejected('archive inspection limit exceeded')
                self.scan(archive.read(info), name, depth + 1, strict, document)

    def pyz(self, data, depth):
        from PyInstaller.archive.readers import ZlibArchiveReader
        with tempfile.TemporaryDirectory(prefix='release-pyz-') as temporary:
            path = Path(temporary) / 'archive.pyz'
            path.write_bytes(data)
            archive = ZlibArchiveReader(str(path), check_pymagic=True)
            if not archive.toc:
                raise Rejected('empty Python archive')
            for name, entry in archive.toc.items():
                self.text(name, True, name)
                value = archive.extract(name)
                if value is None and entry[0] == 3:  # namespace package
                    continue
                if isinstance(value, types.CodeType):
                    self.code(value, first_party(name), name)
                else:
                    raise Rejected('unknown Python archive entry')

    def carchive(self, data, depth):
        from PyInstaller.archive.readers import CArchiveReader
        with tempfile.TemporaryDirectory(prefix='release-onefile-') as temporary:
            path = Path(temporary) / 'archive.pkg'
            path.write_bytes(data)
            archive = CArchiveReader(str(path))
            if not archive.toc:
                raise Rejected('empty executable archive')
            self.onefiles += 1
            normalized = {safe_name(name): name for name in archive.toc}
            if len(normalized) != len(archive.toc):
                raise Rejected('duplicate executable path')
            links = {safe_name(name): archive.extract(name).rstrip(b'\x00').decode('utf-8')
                     for name, entry in archive.toc.items() if entry[-1] == 'n'}
            for option in archive.options:
                self.text(option, True, 'archive-option')
            for original, entry in archive.toc.items():
                name = safe_name(original)
                value = archive.extract(original)
                if len(value) != entry[2]:
                    raise Rejected('truncated executable member')
                kind = entry[-1]
                if kind in {'s', 'm', 'M'}:
                    self.path_rules(name, False)
                    strict = first_party(name) or (kind == 's' and not name.startswith(('pyiboot', 'pyi_rth')))
                    self.code(marshal.loads(value), strict, name)
                elif kind == 'z':
                    self.scan(value, name, depth + 1, False)
                elif kind in {'x', 'b', 'Z'}:
                    self.scan(value, name, depth + 1, first_party(name), native=kind == 'b')
                elif kind == 'n':
                    self.path_rules(name, False)
                    target = safe_name(posixpath.normpath(str(PurePosixPath(name).parent / links[name])))
                    for _ in range(40):
                        parts = PurePosixPath(target).parts
                        for index in range(1, len(parts) + 1):
                            prefix = '/'.join(parts[:index])
                            if prefix in links:
                                target = safe_name(posixpath.normpath(posixpath.join(
                                    posixpath.dirname(prefix), links[prefix], *parts[index:])))
                                break
                        else:
                            if target not in normalized and not any(n.startswith(target + '/') for n in normalized):
                                raise Rejected('unresolved archive link')
                            break
                    else:
                        raise Rejected('cyclic archive link')
                else:
                    raise Rejected('unsupported executable archive entry')

    def paths(self, inputs):
        count = 0
        for path in inputs:
            path = Path(path)
            if path.is_symlink() or not path.exists():
                raise Rejected('missing or symbolic input')
            files = sorted(path.rglob('*')) if path.is_dir() else [path]
            for file in files:
                if file.is_symlink():
                    raise Rejected('symbolic input')
                if file.is_dir():
                    continue
                if not file.is_file():
                    raise Rejected('unreadable input')
                name = file.relative_to(path).as_posix() if path.is_dir() else file.name
                self.scan(file.read_bytes(), name)
                count += 1
        if not count:
            raise Rejected('no release files')


def selftest():
    # Deliberately dirty compressed content must fail before a clean case passes.
    bad = io.BytesIO()
    with zipfile.ZipFile(bad, 'w', zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('local_app/config.json', ''.join(('ghp_', 'x' * 36)))
    try:
        Scanner().scan(bad.getvalue(), 'release.zip')
    except Rejected:
        pass
    else:
        raise Rejected('selftest failed to block dirty fixture')
    clean = io.BytesIO()
    with zipfile.ZipFile(clean, 'w') as archive:
        archive.writestr('local_app/schema.sql', 'CREATE TABLE sample (id INTEGER);')
    Scanner().scan(clean.getvalue(), 'release.zip')
    print('selftest passed (dirty blocked, clean accepted)')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--selftest', action='store_true')
    parser.add_argument('paths', nargs='*')
    args = parser.parse_args()
    try:
        if args.selftest:
            selftest()
        if args.paths:
            scanner = Scanner()
            scanner.paths(args.paths)
            print(f'release privacy passed: {scanner.entries} files/members; {scanner.onefiles} executable archives')
        elif not args.selftest:
            raise Rejected('no release inputs')
    except Exception as error:
        # Do not echo file names, matched values or archive exception payloads.
        reason = str(error) if isinstance(error, Rejected) else type(error).__name__
        print('release privacy FAILED: ' + reason, file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
