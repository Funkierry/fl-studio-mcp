"""Collect license notices from the environment used to build the release EXE."""
import argparse
from importlib import metadata
import json
from pathlib import Path
import shutil
import sys

PACKAGES = ('numpy', 'scipy', 'mido', 'packaging', 'pyinstaller')


def collect(destination):
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    manifest = {'python': sys.version.split()[0], 'packages': {}}
    python_license = Path(sys.base_prefix)/'LICENSE.txt'
    if not python_license.is_file():
        raise FileNotFoundError('Python LICENSE.txt is missing; include the interpreter license before release')
    shutil.copy2(python_license, destination/'Python-LICENSE.txt')
    for name in PACKAGES:
        dist = metadata.distribution(name)
        paths = []
        for entry in dist.files or []:
            if not any(word in Path(entry).name.lower() for word in ('license', 'copying', 'notice')):
                continue
            if '..' in entry.parts or entry.is_absolute():
                continue
            source = Path(dist.locate_file(entry))
            if not source.is_file():
                continue
            target = destination/name/entry
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            paths.append(target.relative_to(destination).as_posix())
        if not paths:
            raise RuntimeError(f'No license files found for {name}')
        manifest['packages'][name] = {'version': dist.version, 'license_files': paths}
    (destination/'manifest.json').write_text(json.dumps(manifest, indent=2)+'\n', encoding='utf-8')
    print(f'Collected Python and {len(PACKAGES)} package license notices into {destination}')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    collect(parser.parse_args().output)
