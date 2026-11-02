"""Where FL Studio keeps user scripts, and where this package keeps the FL-side scripts."""

import ctypes
import os
import uuid
from importlib import resources
from pathlib import Path

BRIDGE_FOLDER = "FL Studio MCP"
BRIDGE_FILE = "device_FLStudioMCP.py"
BRIDGE_NAME = "FL Studio MCP Bridge"
PIANO_ROLL_SCRIPT = "MCP - Apply notes"

# Files installed by earlier development versions; removed on install so FL doesn't list duplicates.
LEGACY_HARDWARE_FOLDERS = ["FL Agent Bridge"]
LEGACY_PIANO_ROLL_SCRIPTS = ["FL Agent - Apply notes.pyscript"]

_FOLDERID_DOCUMENTS = "{FDD39AD0-238F-46AF-ADB4-6C85480369C7}"


def documents_dir():
    """The user's Documents folder, honoring OneDrive/folder redirection."""

    class GUID(ctypes.Structure):
        _fields_ = [("d1", ctypes.c_ulong), ("d2", ctypes.c_ushort), ("d3", ctypes.c_ushort),
                    ("d4", ctypes.c_ubyte * 8)]

    try:
        guid = GUID.from_buffer_copy(uuid.UUID(_FOLDERID_DOCUMENTS).bytes_le)
        out = ctypes.c_wchar_p()
        if ctypes.windll.shell32.SHGetKnownFolderPath(ctypes.byref(guid), 0, None, ctypes.byref(out)) == 0:
            path = out.value
            ctypes.windll.ole32.CoTaskMemFree(out)
            return Path(path)
    except (AttributeError, OSError):
        pass
    return Path.home() / "Documents"


def fl_user_dir():
    """FL Studio's user data folder (File settings → User data folder). Override with FLSTUDIO_USER_DIR."""
    env = os.environ.get("FLSTUDIO_USER_DIR")
    return Path(env) if env else documents_dir() / "Image-Line" / "FL Studio"


def hardware_dir():
    return fl_user_dir() / "Settings" / "Hardware"


def piano_roll_dir():
    return fl_user_dir() / "Settings" / "Piano roll scripts"


def piano_roll_script_path():
    return piano_roll_dir() / f"{PIANO_ROLL_SCRIPT}.pyscript"


def bridge_script_path():
    return hardware_dir() / BRIDGE_FOLDER / BRIDGE_FILE


def packaged_script(name):
    return resources.files("fl_studio_mcp").joinpath("fl", name).read_text(encoding="utf-8")
