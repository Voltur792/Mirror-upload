"""Entry point for the Windows build with its own embedded interpreter."""
import json
import sys
from src.plugin import ComponentMirror, ICON
from src.quick_setup import load_seed, supported_platform

if __name__ == '__main__':
    if sys.argv[1:] == ['--self-check']:
        seed = load_seed()
        print(json.dumps({'name': 'Mirror-upload', 'embedded_python': sys.version.split()[0],
                          'windows_x64': supported_platform(), 'registry_plugins': len({p['id'] for p in seed['registry_plugins']}),
                          'nav_icon_bytes': len(ICON.encode()), 'seed_loaded': True}))
    else:
        ComponentMirror().run()
