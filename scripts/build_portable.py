"""Package the model manager separately from the standalone Windows build."""
import argparse
from pathlib import Path
import re
import shutil
import subprocess
import tomllib


def main():
    project = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cli', type=Path, required=True)
    parser.add_argument('--work-dir', type=Path, required=True)
    parser.add_argument('--output-dir', type=Path, default=project / 'dist')
    options = parser.parse_args()
    package = options.work_dir.resolve() / 'portable-package'
    if package.exists():
        raise SystemExit('Use a fresh package directory.')
    package.mkdir(parents=True)
    for directory in ['src', 'ui', 'locales']:
        shutil.copytree(project / directory, package / directory,
                        ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    for name in ['README.md', 'PLATFORMS.md', 'LICENSE', 'locales.lock.json', 'icon.png',
                 'requirements.txt', 'requirements.lock', 'pyproject.toml', 'uv.toml']:
        shutil.copy2(project / name, package / name)
    manifest = (project / 'plugin.toml').read_text('utf-8')
    manifest = re.sub(r'(?m)^os = \[.*\]$', 'os = ["linux", "macos"]', manifest)
    (package / 'plugin.toml').write_text(manifest, encoding='utf-8')
    version = tomllib.loads(manifest)['plugin']['version']
    output = options.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    cli = str(options.cli.resolve(strict=True))
    subprocess.run([cli, 'check', str(package), '--strict'], check=True)
    subprocess.run([cli, 'test', str(package)], check=True)
    subprocess.run([cli, 'build', str(package), '--reproducible', '--output',
                    str(output / f'component-mirror-{version}-noarch.astraplugin')], check=True)


if __name__ == '__main__':
    main()
