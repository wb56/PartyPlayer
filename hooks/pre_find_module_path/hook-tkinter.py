"""Keep stdlib tkinter discoverable when PyInstaller's Tcl probe is inconclusive."""

from pathlib import Path
import sys


def pre_find_module_path(hook_api):
    hook_api.search_dirs = [str(Path(sys.base_prefix) / "Lib")]
