"""Pure presentation and download-boundary tests for first-run setup."""

from pathlib import Path

import pytest

from party_player.system_dependencies import (
    FFMPEG_DOWNLOAD_URL,
    VLC_DOWNLOAD_URL,
    DependencyInfo,
    DependencyStatus,
)
from party_player.ui.first_run_dialog import FirstRunSetupDialog, open_official_download


def test_dependency_description_contains_path_reason_and_action() -> None:
    info = DependencyInfo(
        "FFmpeg",
        DependencyStatus.NOT_FOUND,
        executable_path=Path(r"C:\Tools\ffmpeg.exe"),
        source="user",
        message="Programmdatei fehlt",
    )

    text = FirstRunSetupDialog._describe(info, "bin-Ordner auswählen.")

    assert "Quelle: user" in text
    assert r"Pfad: C:\Tools\ffmpeg.exe" in text
    assert "Hinweis: Programmdatei fehlt" in text
    assert "Aktion: bin-Ordner auswählen." in text


@pytest.mark.parametrize("url", [VLC_DOWNLOAD_URL, FFMPEG_DOWNLOAD_URL])
def test_only_configured_https_downloads_open_after_user_action(url: str) -> None:
    opened: list[str] = []

    open_official_download(url, lambda target: opened.append(target))

    assert opened == [url]
    assert url.startswith("https://")


def test_unconfigured_download_target_is_rejected() -> None:
    with pytest.raises(ValueError, match="Nicht erlaubtes"):
        open_official_download("https://example.invalid/tool", lambda _target: True)
