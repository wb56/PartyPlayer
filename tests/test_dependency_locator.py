from pathlib import Path

from party_player.dependency_locator import (
    DependencyCandidateSource,
    DependencyLocator,
)


def test_vlc_user_path_has_priority_and_is_retained_without_io(tmp_path: Path) -> None:
    user = tmp_path / "missing custom vlc"
    standard = tmp_path / "standard-vlc"
    locator = DependencyLocator(
        which=lambda _name: None,
        vlc_standard_directories=[standard],
        ffmpeg_standard_directories=[],
    )

    candidates = locator.locate_vlc(user)

    assert [candidate.installation_directory for candidate in candidates] == [user, standard]
    assert candidates[0].source == DependencyCandidateSource.USER
    assert candidates[0].user_selected
    assert candidates[0].expected_files == ("vlc.exe", "libvlc.dll", "plugins")


def test_vlc_path_candidate_follows_standard_installation(tmp_path: Path) -> None:
    standard = tmp_path / "standard"
    path_dir = tmp_path / "path tools"

    def which(name: str) -> str | None:
        return str(path_dir / "vlc.exe") if name == "vlc.exe" else None

    candidates = DependencyLocator(
        which=which,
        vlc_standard_directories=[standard],
        ffmpeg_standard_directories=[],
    ).locate_vlc()

    assert [candidate.source for candidate in candidates] == [
        DependencyCandidateSource.STANDARD,
        DependencyCandidateSource.PATH,
    ]
    assert candidates[1].installation_directory == path_dir


def test_duplicate_candidate_uses_highest_priority_source(tmp_path: Path) -> None:
    shared = tmp_path / "VLC"
    locator = DependencyLocator(
        which=lambda name: str(shared / name) if name == "vlc.exe" else None,
        vlc_standard_directories=[shared],
        ffmpeg_standard_directories=[],
    )

    candidates = locator.locate_vlc(shared, detected_directories=[shared])

    assert len(candidates) == 1
    assert candidates[0].source == DependencyCandidateSource.USER


def test_ffmpeg_and_ffprobe_path_results_become_directory_candidates(
    tmp_path: Path,
) -> None:
    first = tmp_path / "ffmpeg-one" / "bin"
    second = tmp_path / "ffmpeg-two" / "bin"

    def which(name: str) -> str | None:
        if name == "ffmpeg.exe":
            return str(first / name)
        if name == "ffprobe.exe":
            return str(second / name)
        return None

    candidates = DependencyLocator(
        which=which,
        vlc_standard_directories=[],
        ffmpeg_standard_directories=[],
    ).locate_ffmpeg()

    assert [candidate.installation_directory for candidate in candidates] == [first, second]
    assert all(
        candidate.expected_files == ("ffmpeg.exe", "ffprobe.exe") for candidate in candidates
    )


def test_environment_variables_and_spaces_are_normalized(tmp_path: Path) -> None:
    tools = tmp_path / "External Tools"
    locator = DependencyLocator(
        environment={"PARTY_TOOLS": str(tools)},
        which=lambda _name: None,
        vlc_standard_directories=[],
        ffmpeg_standard_directories=[],
    )

    candidate = locator.locate_ffmpeg("%PARTY_TOOLS%/ffmpeg bin")[0]

    assert candidate.installation_directory == tools / "ffmpeg bin"
