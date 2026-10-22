"""Astra's user directories; executable quick setup remains Windows-only."""
import os
from pathlib import Path
import sys


def _xdg(variable, fallback):
    value = Path(os.environ.get(variable, ''))
    return value if value.is_absolute() else fallback


def astra_config_dir():
    if sys.platform == 'win32':
        roaming = Path(os.environ['APPDATA']) if os.environ.get('APPDATA') else Path.home() / 'AppData' / 'Roaming'
        return roaming / 'astra' / 'astra' / 'config'
    home = Path.home()
    if sys.platform == 'darwin':
        return home / 'Library' / 'Application Support' / 'com.astra.astra'
    return _xdg('XDG_CONFIG_HOME', home / '.config') / 'astra'


def astra_data_dir():
    if sys.platform == 'win32':
        return astra_config_dir().parent / 'data'
    if sys.platform == 'darwin':
        return astra_config_dir()
    return _xdg('XDG_DATA_HOME', Path.home() / '.local' / 'share') / 'astra'
