from pathlib import Path
import sys

from PyInstaller.utils.hooks import collect_data_files


project_dir = Path(SPECPATH)
sys.path.insert(0, str(project_dir / "src"))

from party_player.release_artifact import is_forbidden_dependency_path

datas = collect_data_files("customtkinter")

a = Analysis(
    [str(project_dir / "src" / "party_player" / "__main__.py")],
    pathex=[str(project_dir / "src")],
    binaries=[],
    datas=datas,
    hiddenimports=["vlc"],
)
# The python-vlc PyInstaller hook may discover a locally installed VLC runtime.
# PartyPlayer intentionally ships only the Python binding and requires an external
# user-installed VLC/FFmpeg runtime.
a.binaries = TOC(
    entry for entry in a.binaries if not is_forbidden_dependency_path(entry[0])
)
a.datas = TOC(entry for entry in a.datas if not is_forbidden_dependency_path(entry[0]))
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="PartyPlayer",
    console=False,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    name="PartyPlayer",
    contents_directory=".",
)
