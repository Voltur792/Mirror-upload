"""Install with Astra's uv path, then import native dependencies and run tests."""
import os
from pathlib import Path
import subprocess
import sys
import venv

root = Path(__file__).resolve().parents[1]
target = root / '.tmp-platform-tests' / 'runtime'
if target.exists():
    raise SystemExit('Use a fresh .tmp-platform-tests/runtime directory.')
venv.EnvBuilder(with_pip=False).create(target)
python = target / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
subprocess.run([sys.executable, '-m', 'uv', 'pip', 'sync', 'requirements.lock', '--python', str(python)], cwd=root, check=True)
probe = "import sys,grpc; from grpc._cython import cygrpc; import astra_plugin_sdk; print(sys.version); print(cygrpc.__file__)"
if (root / 'src' / 'renderer.py').exists():
    probe += "; import numpy,onnxruntime; from src import renderer; print('Renderer and native dependencies imported')"
subprocess.run([str(python), '-c', probe], cwd=root, check=True)
subprocess.run([str(python), '-m', 'compileall', '-q', 'src'], cwd=root, check=True)
subprocess.run([str(python), '-m', 'unittest', 'discover', '-s', 'tests', '-v'], cwd=root, check=True)
