"""
Build dist/fl-studio-mcp.exe: a single file that needs no Python installation (numpy and scipy are bundled).

    python -m pip install pyinstaller
    python scripts/build_exe.py
"""

import subprocess
import sys
import argparse
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
parser = argparse.ArgumentParser()
parser.add_argument('--dist-dir', type=Path, default=ROOT/'dist')
parser.add_argument('--work-dir', type=Path, default=ROOT/'build')
args = parser.parse_args()

cmd = [
    sys.executable, "-m", "PyInstaller",
    "--noconfirm", "--clean", "--onefile", "--console",
    "--name", "fl-studio-mcp",
    "--paths", str(SRC),
    "--collect-submodules", "fl_studio_mcp",          # tools import their listening modules lazily
    "--add-data", f"{SRC / 'fl_studio_mcp' / 'fl'};fl_studio_mcp/fl",
    "--exclude-module", "tkinter",
    "--exclude-module", "matplotlib",
    "--exclude-module", "PyInstaller",
    "--distpath", str(args.dist_dir),
    "--workpath", str(args.work_dir),
    "--specpath", str(args.work_dir),
    str(ROOT / "fl-studio-mcp.py"),
]
subprocess.run(cmd, check=True)
exe = args.dist_dir / "fl-studio-mcp.exe"
print(f"\nbuilt {exe} ({exe.stat().st_size / 1e6:.1f} MB)")
