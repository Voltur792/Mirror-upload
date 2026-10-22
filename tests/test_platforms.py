"""Portable models must never trigger the Windows setup or registry backend."""
import asyncio
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from src import platform_paths
from src.catalog import default_data_root
from src.plugin import ComponentMirror, state_root


class PlatformTests(unittest.TestCase):
    def test_linux_uses_xdg_data_and_configuration(self):
        with TemporaryDirectory() as tmp, patch.object(platform_paths.sys, 'platform', 'linux'), patch.object(Path, 'home', return_value=Path(tmp)):
            with patch.dict(os.environ, {'XDG_DATA_HOME': str(Path(tmp) / 'data'), 'XDG_CONFIG_HOME': str(Path(tmp) / 'cfg')}, clear=True):
                self.assertEqual(default_data_root(), Path(tmp) / 'data' / 'astra')
                self.assertEqual(state_root(), Path(tmp) / 'cfg' / 'astra' / 'component-mirror')
            with patch.dict(os.environ, {'XDG_DATA_HOME': 'relative'}, clear=True):
                self.assertEqual(default_data_root(), Path(tmp) / '.local' / 'share' / 'astra')

    def test_macos_application_support_and_explicit_portable_root(self):
        with TemporaryDirectory() as tmp, patch.object(platform_paths.sys, 'platform', 'darwin'), patch.object(Path, 'home', return_value=Path(tmp)):
            with patch.dict(os.environ, {}, clear=True):
                self.assertEqual(default_data_root(), Path(tmp) / 'Library' / 'Application Support' / 'com.astra.astra')
            with patch.dict(os.environ, {'COMPONENT_MIRROR_ASTRA_DATA': str(Path(tmp) / 'portable')}, clear=True):
                self.assertEqual(default_data_root(), Path(tmp) / 'portable')

    def test_portable_dashboard_and_methods_never_touch_windows_environment(self):
        with TemporaryDirectory() as tmp, patch.dict(os.environ, {'COMPONENT_MIRROR_STATE': str(Path(tmp) / 'state'), 'COMPONENT_MIRROR_ASTRA_DATA': str(Path(tmp) / 'models')}):
            plugin = ComponentMirror()
            with patch('src.plugin.supported_platform', return_value=False), patch('src.plugin.is_applied', side_effect=AssertionError('registry read')), patch('src.plugin.restore', side_effect=AssertionError('registry write')), patch('src.plugin.QuickSetup', side_effect=AssertionError('Windows installer')):
                dashboard = asyncio.run(plugin.get_dashboard())
                self.assertTrue(dashboard['components'])
                self.assertFalse(dashboard['quick_setup']['supported'])
                self.assertFalse(dashboard['quick_setup']['environment_saved'])
                self.assertFalse(asyncio.run(plugin.start_quick_setup())['ok'])
                self.assertFalse(asyncio.run(plugin.restore_user_environment())['ok'])
