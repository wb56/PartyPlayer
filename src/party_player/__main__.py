"""Application entry point."""

import ctypes
import json
import os
from pathlib import Path
import sys


def _run_internal_vlc_probe(directory: Path, output_path: Path) -> None:
    """Load and initialize external libVLC for the parent process's probe."""
    os.environ["VLC_PLUGIN_PATH"] = str(directory / "plugins")
    dll_directory = (
        os.add_dll_directory(str(directory)) if hasattr(os, "add_dll_directory") else None
    )
    try:
        try:
            libvlc = ctypes.CDLL(str(directory / "libvlc.dll"))
            libvlc.libvlc_get_version.restype = ctypes.c_char_p
            libvlc.libvlc_new.restype = ctypes.c_void_p
            instance = libvlc.libvlc_new(0, None)
            if not instance:
                raise RuntimeError("libvlc_new failed")
            try:
                raw_version = libvlc.libvlc_get_version()
                version = raw_version.decode(errors="replace") if raw_version else ""
            finally:
                libvlc.libvlc_release(ctypes.c_void_p(instance))
            payload = {"version": version}
        except Exception as exc:
            payload = {"error": f"{type(exc).__name__}: libVLC konnte nicht initialisiert werden"}
        output_path.write_text(json.dumps(payload), encoding="utf-8")
    finally:
        if dll_directory is not None:
            dll_directory.close()


def main() -> None:
    """Start the Party Player desktop application."""
    if len(sys.argv) == 4 and sys.argv[1] == "--internal-vlc-probe":
        _run_internal_vlc_probe(Path(sys.argv[2]), Path(sys.argv[3]))
        return

    # Keep GUI imports out of the private dependency-probe subprocess.
    from party_player.app import PartyPlayerApplication

    PartyPlayerApplication().run()


if __name__ == "__main__":
    main()
