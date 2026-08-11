from pathlib import Path

from party_player.system_dependencies import (
    DependencyInfo,
    DependencyStatus,
    RuntimeCapabilities,
    VersionStatus,
    VlcDependencyInfo,
    assess_version,
    parse_version,
)


def test_version_parser_extracts_numeric_version_from_tool_output() -> None:
    assert parse_version("VLC media player 3.0.21 Vetinari").parts == (3, 0, 21)  # type: ignore[union-attr]
    assert parse_version("ffmpeg version 8.0-full_build").parts == (8, 0)  # type: ignore[union-attr]
    assert parse_version("keine erkennbare Version") is None


def test_version_comparison_is_numeric_and_normalizes_missing_parts() -> None:
    assert assess_version("10.0", "3.0").status == VersionStatus.SUPPORTED
    assert assess_version("3.0", "3.0.0").status == VersionStatus.SUPPORTED
    assert assess_version("2.10.9", "3.0").status == VersionStatus.UNSUPPORTED


def test_unknown_version_does_not_reject_a_functional_dependency() -> None:
    result = assess_version("nightly build", "3.0")

    assert result.status == VersionStatus.UNKNOWN
    assert result.detected is None


def test_runtime_capabilities_distinguish_playback_from_analysis() -> None:
    vlc = VlcDependencyInfo(
        DependencyStatus.AVAILABLE,
        installation_directory=Path("C:/VLC"),
        libvlc_loaded=True,
    )
    missing_ffmpeg = DependencyInfo("FFmpeg", DependencyStatus.NOT_FOUND)
    ffprobe = DependencyInfo("FFprobe", DependencyStatus.AVAILABLE)

    capabilities = RuntimeCapabilities.from_dependencies(vlc, missing_ffmpeg, ffprobe)

    assert capabilities.playback_available
    assert not capabilities.cue_analysis_available
    assert not capabilities.loudness_analysis_available
    assert capabilities.ffprobe_available


def test_vlc_without_successful_libvlc_probe_cannot_enable_playback() -> None:
    vlc = VlcDependencyInfo(DependencyStatus.AVAILABLE, libvlc_loaded=False)
    available = DependencyInfo("tool", DependencyStatus.AVAILABLE)

    capabilities = RuntimeCapabilities.from_dependencies(vlc, available, available)

    assert not capabilities.playback_available
    assert capabilities.cue_analysis_available
