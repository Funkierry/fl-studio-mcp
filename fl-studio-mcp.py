"""Run fl-studio-mcp from a source checkout without installing it: python fl-studio-mcp.py [command]"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from fl_studio_mcp.cli import main  # noqa: E402

main()
