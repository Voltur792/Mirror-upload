import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from src.catalog import default_data_root
from src.quick_setup import DEFAULT_MIRROR, QuickSetup
from src.user_environment import is_applied, persist, restore


class MemoryEnvironment:
    def __init__(self):
        self.values = {"Path": {"value": r"%LOCALAPPDATA%\Existing;D:\User Tools", "kind": 2},
                       "PIP_NO_INDEX": {"value": "0", "kind": 1}}
        self.notifications = 0

    def read(self, name):
        return self.values.get(name)

    def write(self, name, record):
        if record is None:
            self.values.pop(name, None)
        else:
            self.values[name] = dict(record)

    def notify(self):
        self.notifications += 1


class EnvironmentTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "Пользователь с пробелом 'Тест'"
        self.root.mkdir()
        self.store = MemoryEnvironment()
        self.original = dict(self.store.values)
        self.backup = self.root / "backup.json"
        self.paths = [self.root / "Python" / "Scripts", self.root / "Node"]

    def tearDown(self):
        self.temporary.cleanup()

    def test_unicode_paths_persist_repeat_and_restore_without_duplicates(self):
        for _ in range(2):
            persist(self.paths, self.root / "Библиотеки", self.backup, self.store)
        self.assertTrue(is_applied(self.backup, self.store))
        self.assertEqual(self.store.values['Path']['value'].count(str(self.paths[0])), 1)
        self.assertEqual(self.store.values['Path']['kind'], 2)
        self.assertIn('%LOCALAPPDATA%\\Existing', self.store.values['Path']['value'])
        self.assertIn('%D0', self.store.values['PIP_FIND_LINKS']['value'])
        self.assertEqual(restore(self.backup, self.store), [])
        self.assertEqual(self.store.values, self.original)
        self.assertFalse(self.backup.exists())

    def test_restore_preserves_later_user_changes(self):
        persist(self.paths, self.root, self.backup, self.store)
        self.store.values['Path'] = {'value': r'D:\Changed by user', 'kind': 1}
        self.assertEqual(restore(self.backup, self.store), ['Path'])
        self.assertEqual(self.store.values['Path']['value'], r'D:\Changed by user')

    def test_failed_registry_write_rolls_back_and_keeps_existing_backup(self):
        persist(self.paths, self.root, self.backup, self.store)
        before = dict(self.store.values)
        backup = self.backup.read_bytes()
        original_write = self.store.write
        failed = False
        def write(name, record):
            nonlocal failed
            if name == 'UV_FIND_LINKS' and not failed:
                failed = True
                raise OSError('registry write failed')
            original_write(name, record)
        with patch.object(self.store, 'write', side_effect=write), self.assertRaises(OSError):
            persist([self.root / 'new'], self.root, self.backup, self.store)
        self.assertEqual(before, self.store.values)
        self.assertEqual(backup, self.backup.read_bytes())

    def test_default_data_root_uses_current_unicode_user(self):
        with patch.dict(os.environ, {'APPDATA': str(self.root)}, clear=True):
            self.assertEqual(default_data_root(), (self.root / 'astra' / 'astra' / 'data').resolve())

    def test_launcher_targets_redirected_desktop_and_quotes_unicode_paths(self):
        setup = QuickSetup(self.root / 'state', self.root / 'astra/data', DEFAULT_MIRROR, [], threading.Event(), lambda **_: None)
        setup.root.mkdir(parents=True)
        setup.python = self.root / 'Python/python.exe'
        desktop = self.root / 'Перенесённый рабочий стол'
        with patch.object(setup, 'desktop_directory', return_value=desktop), patch.object(setup, 'run_command') as command:
            launcher, shortcut = setup.write_launcher(self.root / "Программы 'Тест'/Astra.exe")
        self.assertEqual(Path(shortcut).parent, desktop)
        self.assertEqual(command.call_args.kwargs['environment']['MIRROR_SHORTCUT_FILE'], shortcut)
        script = (setup.root / 'start-astra.ps1').read_text('utf-8-sig')
        self.assertIn("Программы ''Тест''", script)
        self.assertIn('Remove-Item -LiteralPath $mirrorShortcut', script)
        self.assertLess(script.index('Start-Process'), script.index('Remove-Item'))
        self.assertIn('$mirrorLink.TargetPath -eq $mirrorLauncher', script)
        self.assertEqual(Path(launcher).read_text('ascii').count('%~dp0'), 1)

    def test_cleanup_removes_only_pinned_completed_archives(self):
        import hashlib
        setup = QuickSetup(self.root / 'state', self.root / 'astra/data', DEFAULT_MIRROR, [], threading.Event(), lambda **_: None,
                           seed={'runtimes': [], 'models': [], 'providers': [], 'libraries': {'path': 'libraries.zip', 'sha256': hashlib.sha256(b'archive').hexdigest()}})
        folder = setup.root / 'downloads'
        folder.mkdir(parents=True)
        archive = folder / 'libraries.zip'
        archive.write_bytes(b'user replacement')
        keep = folder / 'unknown.zip'
        keep.write_bytes(b'user file')
        self.assertEqual(setup.clean_downloads(), 0)
        archive.write_bytes(b'archive')
        self.assertEqual(setup.clean_downloads(), 7)
        self.assertFalse(archive.exists())
        self.assertTrue(keep.exists())
