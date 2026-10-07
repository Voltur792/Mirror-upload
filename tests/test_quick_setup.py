import hashlib
import json
import os
import stat
import tarfile
import tempfile
import threading
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from astra_plugin_sdk.testing import Harness
from src.plugin import ComponentMirror
from src.quick_setup import DEFAULT_MIRROR, QuickSetup, child_environment, safe_member, unpack_verified


class QuickSetupTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.cancel = threading.Event()

    def tearDown(self):
        self.temporary.cleanup()

    def package(self, files):
        archive = self.root / 'dependencies.zip'
        with zipfile.ZipFile(archive, 'w') as target:
            for name, value in files.items():
                target.writestr(name, value)
        return archive

    def test_archive_preserves_conflict_before_any_new_file(self):
        archive = self.package({'providers/new.txt': b'new', 'providers/user.txt': b'other'})
        destination = self.root / 'target'
        (destination / 'providers').mkdir(parents=True)
        (destination / 'providers/user.txt').write_bytes(b'user')
        with self.assertRaisesRegex(ValueError, 'не заменён'):
            unpack_verified(archive, destination, self.cancel, {'providers'})
        self.assertFalse((destination / 'providers/new.txt').exists())
        self.assertEqual((destination / 'providers/user.txt').read_bytes(), b'user')

    def test_identical_install_can_repeat(self):
        archive = self.package({'wheels/test.whl': b'wheel'})
        for _ in range(2):
            unpack_verified(archive, self.root / 'target', self.cancel, {'wheels'})
        self.assertEqual((self.root / 'target/wheels/test.whl').read_bytes(), b'wheel')

    def test_zip_traversal_symlink_and_extra_root_rejected(self):
        for name in ['../escape', 'C:/escape', 'wheels/file:stream', 'providers/file.exe']:
            archive = self.package({name: b'bad'})
            with self.subTest(name=name), self.assertRaises(ValueError):
                unpack_verified(archive, self.root / 'target', self.cancel, {'wheels'})
        archive = self.root / 'symlink.zip'
        member = zipfile.ZipInfo('wheels/link')
        member.create_system = 3
        member.external_attr = (stat.S_IFLNK | 0o777) << 16
        with zipfile.ZipFile(archive, 'w') as package:
            package.writestr(member, b'elsewhere')
        with self.assertRaises(ValueError):
            unpack_verified(archive, self.root / 'target', self.cancel)

    def test_tar_links_are_rejected(self):
        archive = self.root / 'claude.tgz'
        with tarfile.open(archive, 'w:gz') as package:
            member = tarfile.TarInfo('package/claude.exe')
            member.type = tarfile.SYMTYPE
            member.linkname = '../../outside'
            package.addfile(member)
        with self.assertRaises(ValueError):
            unpack_verified(archive, self.root / 'target', self.cancel)

    def test_cancelled_extraction_leaves_no_payload(self):
        archive = self.package({'wheels/test.whl': b'wheel'})
        self.cancel.set()
        with self.assertRaises(InterruptedError):
            unpack_verified(archive, self.root / 'target', self.cancel)
        self.assertFalse((self.root / 'target/wheels/test.whl').exists())

    def test_executable_download_uses_build_pinned_hash(self):
        setup = QuickSetup(self.root / 'state', self.root / 'astra/data', DEFAULT_MIRROR, [], self.cancel, lambda **_: None)
        good = b'official-package'
        record = {'path': 'runtimes/runtime.zip', 'size': len(good), 'sha256': hashlib.sha256(good).hexdigest()}
        def corrupt(relative, target, size, cancel, progress):
            target.write_bytes(b'bad-source-data!')
        with patch.object(setup.folder, 'download', side_effect=corrupt):
            with self.assertRaisesRegex(ValueError, 'SHA-256'):
                setup.asset(record)
        self.assertFalse((setup.root / 'downloads/runtimes/runtime.zip').exists())
        self.assertEqual(list(setup.root.rglob('*.part-*')), [])

    def test_missing_astra_aborts_before_installer_or_download(self):
        setup = QuickSetup(self.root / 'state', self.root / 'astra/data', DEFAULT_MIRROR, [], self.cancel, lambda **_: None)
        with patch('src.quick_setup.supported_platform', return_value=True), patch.object(setup, 'find_astra', side_effect=ValueError('Astra missing')), patch.object(setup, 'prepare_python') as prepare:
            with self.assertRaises(ValueError):
                setup.run()
            prepare.assert_not_called()

    def test_existing_webview2_is_preserved_without_installer(self):
        setup = QuickSetup(self.root / 'state', self.root / 'astra/data', DEFAULT_MIRROR, [], self.cancel, lambda **_: None)
        with patch.object(setup, 'webview2_installed', return_value=True), patch.object(setup, 'asset') as asset:
            setup.prepare_webview2()
            asset.assert_not_called()

    def test_vosk_also_uses_discord_model_folder_without_account_config(self):
        setup = QuickSetup(self.root / 'state', self.root / 'astra/data', DEFAULT_MIRROR, [], self.cancel, lambda **_: None)
        with patch.dict(os.environ, {'APPDATA': str(self.root / 'Пользователь'), 'DVOICE_DATA_DIR': str(self.root / 'Discord')}, clear=True):
            destinations = setup.speech_model_destinations({'id': 'vosk-model-small-ru-0.22'})
            self.assertEqual(destinations, [self.root / 'Пользователь/voice-text-input/models', self.root / 'Discord/models'])
            self.assertEqual(len(setup.speech_model_destinations({'id': 'faster-whisper-tiny'})), 1)

    def test_missing_webview2_requires_signature_and_checks_install_result(self):
        setup = QuickSetup(self.root / 'state', self.root / 'astra/data', DEFAULT_MIRROR, [], self.cancel, lambda **_: None)
        installer = self.root / 'MicrosoftEdgeWebView2RuntimeInstallerX64.exe'
        with patch.object(setup, 'webview2_installed', side_effect=[False, True]), patch.object(setup, 'asset', return_value=installer), patch.object(setup, 'run_command') as command:
            setup.prepare_webview2()
        self.assertIn('Microsoft Corporation', command.call_args_list[0].args[0][-1])
        self.assertEqual(command.call_args_list[1].args[0], [str(installer), '/silent', '/install'])
        self.assertFalse(command.call_args_list[1].kwargs['cancellable'])

    def test_child_powershell_does_not_inherit_foreign_module_path(self):
        with patch.dict(os.environ, {'PSMODULEPATH': 'PowerShell7-only', 'PYTHONPATH': 'foreign'}, clear=True):
            environment = child_environment()
        self.assertNotIn('PSMODULEPATH', environment)
        self.assertNotIn('PYTHONPATH', environment)

    def test_default_mirror_tab_label_and_exclusive_worker_via_sdk(self):
        with patch.dict(os.environ, {'COMPONENT_MIRROR_STATE': str(self.root / 'state'), 'COMPONENT_MIRROR_ASTRA_DATA': str(self.root / 'astra/data')}):
            plugin = ComponentMirror()
            started = threading.Event()
            release = threading.Event()
            def work(*args):
                started.set()
                release.wait(5)
                plugin._update(state='cancelled', message='test')
            with patch('src.plugin.supported_platform', return_value=True), patch.object(plugin, '_quick_setup', side_effect=work), Harness(plugin) as harness:
                dashboard = harness.ui_call('get_dashboard').json
                self.assertNotIn('source_url', dashboard)
                self.assertTrue(dashboard['mirror_configured'])
                self.assertEqual(plugin._source_url, DEFAULT_MIRROR)
                self.assertIn('quick_setup', dashboard)
                self.assertTrue(harness.ui_call('start_quick_setup').json['ok'])
                self.assertTrue(started.wait(2))
                self.assertFalse(harness.ui_call('start_quick_setup').json['ok'])
                self.assertFalse(hasattr(plugin, 'save_source'))
                self.assertTrue(harness.ui_call('cancel_install').json['ok'])
                self.assertTrue(plugin._cancel.is_set())
                release.set()
                plugin._worker.join(5)

    def test_windows_unsafe_names_rejected(self):
        for name in ['../bad', '/absolute', 'file.', 'file ', 'NUL.txt', 'a\\b', 'a/../b', 'a:ads', 'a/*']:
            with self.subTest(name=name), self.assertRaises(ValueError):
                safe_member(name)


if __name__ == '__main__':
    unittest.main()
