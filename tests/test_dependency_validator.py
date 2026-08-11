from pathlib import Path
import sys

from party_player.dependency_locator import (
    DependencyCandidate,
    DependencyCandidateSource,
)
from party_player.dependency_validator import DependencyValidator, VlcProbeResult
from party_player.external_process import ExternalProcessResult
from party_player.system_dependencies import DependencyStatus, VersionStatus


def candidate(name: str, directory: Path) -> DependencyCandidate:
    return DependencyCandidate(
        name,
        directory,
        DependencyCandidateSource.USER,
        0,
        (),
        True,
    )


def write_pe(path: Path, bitness: int = 64) -> None:
    payload = bytearray(256)
    payload[0:2] = b"MZ"
    payload[0x3C:0x40] = (128).to_bytes(4, "little")
    payload[128:132] = b"PE\0\0"
    payload[132:134] = (0x8664 if bitness == 64 else 0x014C).to_bytes(2, "little")
    path.write_bytes(payload)


def complete_vlc(directory: Path, *, bitness: int = 64) -> None:
    directory.mkdir(parents=True)
    (directory / "vlc.exe").write_bytes(b"exe")
    write_pe(directory / "libvlc.dll", bitness)
    (directory / "plugins").mkdir()


class FakeRunner:
    def __init__(self, results: dict[str, ExternalProcessResult]) -> None:
        self.results = results
        self.commands: list[tuple[str, ...]] = []

    def run(self, arguments, *, timeout_seconds=None) -> ExternalProcessResult:
        del timeout_seconds
        command = tuple(str(argument) for argument in arguments)
        self.commands.append(command)
        return self.results[Path(command[0]).name]


def process_result(
    executable: str, stdout: str = "", *, timed_out: bool = False, return_code: int = 0
) -> ExternalProcessResult:
    return ExternalProcessResult(
        (executable, "-version"),
        return_code,
        stdout,
        "",
        0.01,
        timed_out=timed_out,
    )


def test_vlc_requires_executable_libvlc_and_plugins(tmp_path: Path) -> None:
    directory = tmp_path / "vlc"
    directory.mkdir()
    validator = DependencyValidator(vlc_probe=lambda _path: VlcProbeResult(True, "3.0"))

    missing_executable = validator.validate_vlc(candidate("VLC", directory))
    (directory / "vlc.exe").write_bytes(b"exe")
    missing_library = validator.validate_vlc(candidate("VLC", directory))
    write_pe(directory / "libvlc.dll")
    missing_plugins = validator.validate_vlc(candidate("VLC", directory))

    assert missing_executable.error_code == "DEP_VLC_NOT_FOUND"
    assert missing_library.error_code == "DEP_VLC_LIBVLC_MISSING"
    assert missing_plugins.error_code == "DEP_VLC_PLUGINS_MISSING"


def test_vlc_rejects_architecture_mismatch_before_probe(tmp_path: Path) -> None:
    directory = tmp_path / "vlc"
    complete_vlc(directory, bitness=32)
    probes = 0

    def probe(_path: Path) -> VlcProbeResult:
        nonlocal probes
        probes += 1
        return VlcProbeResult(True, "3.0")

    result = DependencyValidator(vlc_probe=probe, process_bitness=64).validate_vlc(
        candidate("VLC", directory)
    )

    assert result.status == DependencyStatus.INCOMPATIBLE
    assert result.error_code == "DEP_VLC_ARCHITECTURE_MISMATCH"
    assert probes == 0


def test_vlc_requires_successful_probe_and_supported_version(tmp_path: Path) -> None:
    directory = tmp_path / "vlc"
    complete_vlc(directory)

    failed = DependencyValidator(
        vlc_probe=lambda _path: VlcProbeResult(False, message="DLL load failed"),
        process_bitness=64,
    ).validate_vlc(candidate("VLC", directory))
    old = DependencyValidator(
        vlc_probe=lambda _path: VlcProbeResult(True, "2.2.8"),
        process_bitness=64,
    ).validate_vlc(candidate("VLC", directory))

    assert failed.error_code == "DEP_VLC_LOAD_FAILED"
    assert not failed.libvlc_loaded
    assert old.error_code == "DEP_VLC_VERSION_UNSUPPORTED"
    assert old.version_status == VersionStatus.UNSUPPORTED


def test_unknown_vlc_version_remains_available_after_functional_probe(tmp_path: Path) -> None:
    directory = tmp_path / "vlc"
    complete_vlc(directory)

    result = DependencyValidator(
        vlc_probe=lambda _path: VlcProbeResult(True, "nightly"),
        process_bitness=64,
    ).validate_vlc(candidate("VLC", directory))

    assert result.status == DependencyStatus.AVAILABLE
    assert result.version_status == VersionStatus.UNKNOWN
    assert result.libvlc_loaded


def test_vlc_version_falls_back_to_executable_when_libvlc_reports_none(
    tmp_path: Path,
) -> None:
    directory = tmp_path / "vlc"
    complete_vlc(directory)
    runner = FakeRunner({"vlc.exe": process_result("vlc.exe", "VLC media player 3.0.21 Vetinari")})

    result = DependencyValidator(
        process_runner=runner,
        vlc_probe=lambda _path: VlcProbeResult(True),
        process_bitness=64,
    ).validate_vlc(candidate("VLC", directory))

    assert result.status == DependencyStatus.AVAILABLE
    assert result.version == "VLC media player 3.0.21 Vetinari"
    assert runner.commands == [(str(directory / "vlc.exe"), "--version")]


def test_frozen_vlc_probe_uses_private_entrypoint_instead_of_python_c(
    tmp_path: Path, monkeypatch
) -> None:
    directory = tmp_path / "VLC install"
    runner = FakeRunner(
        {"PartyPlayer.exe": process_result("PartyPlayer.exe", '{"version": "3.0.21 Vetinari"}')}
    )
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", r"C:\Portable\PartyPlayer.exe")

    result = DependencyValidator(process_runner=runner)._probe_vlc_isolated(directory)

    assert result == VlcProbeResult(True, "3.0.21 Vetinari")
    assert len(runner.commands) == 1
    assert runner.commands[0][:3] == (
        r"C:\Portable\PartyPlayer.exe",
        "--internal-vlc-probe",
        str(directory),
    )
    assert runner.commands[0][3].endswith(".json")


def test_ffmpeg_requires_both_programs_from_candidate_directory(tmp_path: Path) -> None:
    directory = tmp_path / "ffmpeg" / "bin"
    directory.mkdir(parents=True)
    (directory / "ffmpeg.exe").write_bytes(b"exe")
    runner = FakeRunner({"ffmpeg.exe": process_result("ffmpeg.exe", "ffmpeg version 8.0")})

    result = DependencyValidator(process_runner=runner).validate_ffmpeg(
        candidate("FFmpeg", directory)
    )

    assert result.ffmpeg.status == DependencyStatus.AVAILABLE
    assert result.ffprobe.error_code == "DEP_FFPROBE_NOT_FOUND"
    assert not result.available
    assert runner.commands == [(str(directory / "ffmpeg.exe"), "-version")]


def test_ffmpeg_pair_is_probed_with_bounded_runner(tmp_path: Path) -> None:
    directory = tmp_path / "Tools with spaces" / "bin"
    directory.mkdir(parents=True)
    for name in ("ffmpeg.exe", "ffprobe.exe"):
        (directory / name).write_bytes(b"exe")
    runner = FakeRunner(
        {
            "ffmpeg.exe": process_result("ffmpeg.exe", "ffmpeg version 8.0-full"),
            "ffprobe.exe": process_result("ffprobe.exe", "ffprobe version 8.0-full"),
        }
    )

    result = DependencyValidator(process_runner=runner).validate_ffmpeg(
        candidate("FFmpeg", directory)
    )

    assert result.available
    assert result.ffmpeg.version_status == VersionStatus.SUPPORTED
    assert result.ffprobe.version_status == VersionStatus.SUPPORTED
    assert all(command[1] == "-version" for command in runner.commands)


def test_ffmpeg_timeout_has_stable_execution_error(tmp_path: Path) -> None:
    directory = tmp_path / "bin"
    directory.mkdir()
    for name in ("ffmpeg.exe", "ffprobe.exe"):
        (directory / name).write_bytes(b"exe")
    runner = FakeRunner(
        {
            "ffmpeg.exe": process_result("ffmpeg.exe", timed_out=True, return_code=-1),
            "ffprobe.exe": process_result("ffprobe.exe", "ffprobe version 8.0"),
        }
    )

    result = DependencyValidator(process_runner=runner).validate_ffmpeg(
        candidate("FFmpeg", directory)
    )

    assert result.ffmpeg.status == DependencyStatus.ERROR
    assert result.ffmpeg.error_code == "DEP_FFMPEG_EXEC_FAILED"
    assert "Zeitlimit" in (result.ffmpeg.message or "")


def test_ffmpeg_failure_message_uses_bounded_relevant_lines(tmp_path: Path) -> None:
    directory = tmp_path / "bin"
    directory.mkdir()
    for name in ("ffmpeg.exe", "ffprobe.exe"):
        (directory / name).write_bytes(b"exe")
    noisy_error = "first\nsecond\nthird\nfourth private detail"
    runner = FakeRunner(
        {
            "ffmpeg.exe": process_result("ffmpeg.exe", "ffmpeg version 8.0"),
            "ffprobe.exe": ExternalProcessResult(("ffprobe.exe",), 1, "", noisy_error, 0.01),
        }
    )

    result = DependencyValidator(process_runner=runner).validate_ffmpeg(
        candidate("FFmpeg", directory)
    )

    assert result.ffprobe.message == "first | second | third"
    assert "private" not in (result.ffprobe.message or "")


def test_quick_checks_probe_vlc_but_spawn_no_version_programs(tmp_path: Path) -> None:
    vlc_directory = tmp_path / "vlc"
    complete_vlc(vlc_directory)
    ffmpeg_directory = tmp_path / "ffmpeg" / "bin"
    ffmpeg_directory.mkdir(parents=True)
    for name in ("ffmpeg.exe", "ffprobe.exe"):
        (ffmpeg_directory / name).write_bytes(b"exe")
    runner = FakeRunner({})
    validator = DependencyValidator(
        process_runner=runner,
        vlc_probe=lambda _path: VlcProbeResult(True, "3.0.21"),
        process_bitness=64,
    )

    vlc_result = validator.validate_vlc_quick(candidate("VLC", vlc_directory))
    ffmpeg_result = validator.validate_ffmpeg_quick(candidate("FFmpeg", ffmpeg_directory))

    assert vlc_result.status == DependencyStatus.AVAILABLE
    assert ffmpeg_result.available
    assert runner.commands == []
