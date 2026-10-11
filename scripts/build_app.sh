#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")/.."
export PYINSTALLER_CONFIG_DIR="$PWD/build/pyinstaller-cache"
uv run --group app python - "$PWD" <<'PY'
import os
from pathlib import Path
import shutil
import subprocess
import sys

import PyInstaller.__main__
from PyInstaller.depend.bindepend import get_imports

root = Path(sys.argv[1])
args = ['--noconfirm', '--clean', '--windowed', '--onedir', '--name', 'LUT Lab',
        '--osx-bundle-identifier', 'local.lut-lab', '--codesign-identity', '-',
        '--icon', str(root / 'assets/icon.icns'),
        '--distpath', str(root / 'dist'), '--workpath', str(root / 'build/lut-lab'),
        '--specpath', str(root / 'build'), '--paths', str(root),
        '--hidden-import', 'webview.platforms.cocoa', '--collect-data', 'webview',
        '--collect-submodules', 'uvicorn',
        '--add-data', f'{root / "lutlab/static"}:lutlab/static',
        '--add-data', f'{root / "LICENSE"}:licenses',
        '--add-data', f'{root / "THIRD_PARTY.md"}:licenses']
seen = set()


def check_dependencies(binary):
    binary = binary.resolve()
    if binary in seen:
        return
    seen.add(binary)
    subprocess.run(['otool', '-L', str(binary)], check=True)
    for name, resolved in get_imports(str(binary)):
        if name.startswith(('/usr/lib/', '/System/Library/')):
            continue
        if not resolved:
            raise RuntimeError(f'Cannot resolve non-system dylib {name} for {binary}')
        if resolved.startswith(('/usr/lib/', '/System/Library/')):
            continue
        if Path(resolved).resolve() not in seen:
            args.extend(['--add-binary', f'{resolved}:.'])
            check_dependencies(Path(resolved))


# rr-lut is intentionally excluded: it is a separately installed AGPL engine.
source = os.environ.get('LIGHTCRAFT_CLI') or shutil.which('lightcraft-cli')
if source:
    source = Path(shutil.which(source) or source).expanduser().resolve()
    if not source.is_file():
        raise FileNotFoundError(source)
    check_dependencies(source)
    staged = root / 'build/engines/lightcraft-cli'
    staged.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, staged)
    args.extend(['--add-binary', f'{staged}:bin'])
    license_roots = [source.parent, *source.parents, Path.home() / 'vscode' / 'lightcraft']
    for name in ('LICENSE-MIT', 'LICENSE-APACHE'):
        license_file = next((p / name for p in license_roots if (p / name).is_file()), None)
        if license_file is None:
            raise RuntimeError(f'Cannot bundle LightCraft without {name}; place it beside the binary')
        args.extend(['--add-data', f'{license_file}:licenses/lightcraft'])
else:
    print('LightCraft not found; building with optional engines unbundled.', file=sys.stderr)

PyInstaller.__main__.run([*args, str(root / 'lutlab/app.py')])
PY
codesign --force --sign - --deep "dist/LUT Lab.app"
codesign --verify --deep --strict "dist/LUT Lab.app"
echo "Built: $PWD/dist/LUT Lab.app"
