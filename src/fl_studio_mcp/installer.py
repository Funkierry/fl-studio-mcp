"""Install the FL-side scripts into FL Studio's user data folder."""

import shutil

from . import paths


def install():
    """Copy the bridge and piano roll scripts into FL. Returns a list of human-readable actions."""
    settings = paths.fl_user_dir() / "Settings"
    if not settings.is_dir():
        raise FileNotFoundError(
            f"FL Studio user data folder not found at {settings.parent}. Start FL once, or set FLSTUDIO_USER_DIR "
            f"to the folder shown in FL under Options → File settings → User data folder.")
    actions = []

    bridge = paths.bridge_script_path()
    bridge.parent.mkdir(parents=True, exist_ok=True)
    bridge.write_text(paths.packaged_script(paths.BRIDGE_FILE), encoding="utf-8")
    actions.append(f"bridge script       -> {bridge}")

    pr = paths.piano_roll_script_path()
    pr.parent.mkdir(parents=True, exist_ok=True)
    pr.write_text(paths.packaged_script(f"{paths.PIANO_ROLL_SCRIPT}.pyscript"), encoding="utf-8")
    actions.append(f"piano roll script   -> {pr}")

    legacy = [paths.hardware_dir() / name for name in paths.LEGACY_HARDWARE_FOLDERS]
    legacy += [paths.piano_roll_dir() / name for name in paths.LEGACY_PIANO_ROLL_SCRIPTS]
    for old in legacy:
        if not old.exists():
            continue
        try:
            shutil.rmtree(old) if old.is_dir() else old.unlink()
            actions.append(f"removed old version {old}")
        except OSError as e:
            actions.append(f"could not remove old version {old} ({e.strerror}); close FL and run install again")
    return actions
