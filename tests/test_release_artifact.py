from pathlib import Path

import pytest

from party_player.release_artifact import (
    forbidden_release_files,
    format_forbidden_files,
    is_forbidden_dependency_path,
)


@pytest.mark.parametrize(
    "path",
    [
        "vlc.exe",
        "_internal/libvlc.dll",
        "_internal/libvlccore.dll",
        "tools/ffmpeg.exe",
        "tools/ffprobe.exe",
        "_internal/plugins/audio_filter/libcompressor_plugin.dll",
    ],
)
def test_forbidden_dependency_paths_are_detected(path: str) -> None:
    assert is_forbidden_dependency_path(path)


@pytest.mark.parametrize(
    "path",
    ["vlc.py", "plugins/theme.json", "libavcodec.dll", "DeckRelay.exe"],
)
def test_binding_and_unrelated_runtime_files_remain_allowed(path: str) -> None:
    assert not is_forbidden_dependency_path(path)


def test_release_scan_is_recursive_stable_and_bounded(tmp_path: Path) -> None:
    allowed = tmp_path / "DeckRelay.exe"
    forbidden = tmp_path / "_internal" / "plugins" / "audio" / "liba_plugin.dll"
    allowed.write_bytes(b"app")
    forbidden.parent.mkdir(parents=True)
    forbidden.write_bytes(b"vlc")
    (tmp_path / "ffmpeg.exe").write_bytes(b"ffmpeg")

    matches = forbidden_release_files(tmp_path)

    assert matches == (
        Path("_internal/plugins/audio/liba_plugin.dll"),
        Path("ffmpeg.exe"),
    )
    formatted = format_forbidden_files(matches, maximum=1)
    assert "liba_plugin.dll" in formatted
    assert "weitere" in formatted


def test_release_scan_requires_existing_directory(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="Releaseverzeichnis fehlt"):
        forbidden_release_files(tmp_path / "missing")


def test_spec_and_build_script_enforce_release_policy() -> None:
    project = Path(__file__).resolve().parents[1]
    spec = (project / "DeckRelay.spec").read_text(encoding="utf-8")
    build_script = (project / "build_runtime.ps1").read_text(encoding="utf-8")

    assert "is_forbidden_dependency_path" in spec
    assert "a.binaries = TOC" in spec
    assert "a.datas = TOC" in spec
    assert "scripts\\check_release_artifact.py" in build_script
    assert "$LASTEXITCODE -ne 0" in build_script
