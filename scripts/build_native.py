"""Build the native bootstrap, then package it with the official Astra CLI."""
import argparse
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

project = Path(__file__).resolve().parent.parent
version = tomllib.loads((project / 'plugin.toml').read_text('utf-8'))['plugin']['version']
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--cli', type=Path, required=True)
parser.add_argument('--output-dir', type=Path, default=project / 'dist')
parser.add_argument('--work-dir', type=Path, default=project / '.native-build')
options = parser.parse_args()
output = options.output_dir.resolve()
work = options.work_dir.resolve()
package = work / 'package'
cli = options.cli.resolve(strict=True)
if package.exists():
    raise SystemExit('Use a fresh work directory to avoid packaging stale files.')
work.mkdir(parents=True, exist_ok=True)
output.mkdir(parents=True, exist_ok=True)
command = [sys.executable, '-m', 'PyInstaller', '--onefile', '--console', '--noupx', '--noconfirm',
           '--name', 'mirror-upload', '--distpath', str(work / 'dist'), '--workpath', str(work / 'build'),
           '--specpath', str(work), '--paths', str(project),
           '--add-data', str(project / 'src/catalog.seed.json') + ':src',
           '--add-data', str(project / 'src/dependencies.seed.json') + ':src',
           '--add-data', str(project / 'ui/web/assets') + ':ui/web/assets',
           '--copy-metadata', 'astra-plugin-sdk', str(project / 'scripts/native_entry.py')]
subprocess.run(command, check=True, cwd=project)
package.mkdir(exist_ok=True)
for directory in ['ui', 'locales']:
    shutil.copytree(project / directory, package / directory, dirs_exist_ok=True)
for name in ['README.md', 'LICENSE', 'locales.lock.json', 'icon.png']:
    shutil.copy2(project / name, package / name)
manifest = (project / 'plugin.toml').read_text('utf-8')
manifest = manifest.replace('command = "python"', 'command = "./mirror-upload.exe"')
manifest = manifest.replace('args = ["-m", "src.plugin"]', 'args = []')
manifest = manifest.replace('runtimes = ["python"]', 'runtimes = []')
manifest += '\n[bundle]\nexecutables = ["mirror-upload.exe"]\n'
(package / 'plugin.toml').write_text(manifest, encoding='utf-8')
shutil.copy2(work / 'dist/mirror-upload.exe', package / 'mirror-upload.exe')
subprocess.run([str(cli), 'check', str(package), '--strict'], check=True)
subprocess.run([str(cli), 'build', str(package), '--target', 'windows-x64', '--reproducible',
                '--output', str(output / f'component-mirror-{version}-windows-x64.astraplugin')], check=True)
print('Native Windows artifact built; no separately installed Python is required for Mirror-upload.')
