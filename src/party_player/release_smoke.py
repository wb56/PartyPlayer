"""Time-bounded smoke test for the frozen Windows release."""

from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
import subprocess
from time import monotonic, sleep
from typing import Protocol


class ReleaseSmokeCode(str, Enum):
    SUCCESS = "RELEASE_SMOKE_SUCCESS"
    RELEASE_MISSING = "RELEASE_MISSING"
    EXECUTABLE_MISSING = "RELEASE_EXECUTABLE_MISSING"
    ARTIFACT_CHECK_FAILED = "RELEASE_ARTIFACT_CHECK_FAILED"
    PROCESS_START_FAILED = "RELEASE_PROCESS_START_FAILED"
    PROCESS_EXITED_EARLY = "RELEASE_PROCESS_EXITED_EARLY"
    LOG_NOT_CREATED = "RELEASE_LOG_NOT_CREATED"
    CLEANUP_FAILED = "RELEASE_CLEANUP_FAILED"


@dataclass(frozen=True, slots=True)
class ReleaseSmokeResult:
    success: bool
    code: ReleaseSmokeCode
    message: str
    pid: int | None = None
    exit_code: int | None = None


class SmokeProcess(Protocol):
    pid: int

    def poll(self) -> int | None: ...

    def terminate(self) -> None: ...

    def kill(self) -> None: ...

    def wait(self, timeout: float | None = None) -> int: ...


ProcessStarter = Callable[[Path, Path], SmokeProcess]
ArtifactChecker = Callable[[Path], bool]


def _start_process(executable: Path, working_directory: Path) -> SmokeProcess:
    return subprocess.Popen(  # noqa: S603 - executable is the fixed release binary
        [str(executable)],
        cwd=working_directory,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _log_signature(path: Path) -> tuple[int, int] | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    return stat.st_mtime_ns, stat.st_size


def run_release_smoke_test(
    release_directory: Path,
    *,
    artifact_checker: ArtifactChecker,
    process_starter: ProcessStarter = _start_process,
    timeout_seconds: float = 8.0,
    poll_interval_seconds: float = 0.1,
    cleanup_timeout_seconds: float = 3.0,
    clock: Callable[[], float] = monotonic,
    sleeper: Callable[[float], None] = sleep,
) -> ReleaseSmokeResult:
    """Check one release without ever terminating an unrelated process."""
    release_directory = release_directory.resolve()
    if not release_directory.is_dir():
        return ReleaseSmokeResult(
            False, ReleaseSmokeCode.RELEASE_MISSING, "Releaseverzeichnis fehlt."
        )
    executable = release_directory / "DeckRelay.exe"
    if not executable.is_file():
        return ReleaseSmokeResult(
            False, ReleaseSmokeCode.EXECUTABLE_MISSING, "DeckRelay.exe fehlt."
        )
    if not artifact_checker(release_directory):
        return ReleaseSmokeResult(
            False,
            ReleaseSmokeCode.ARTIFACT_CHECK_FAILED,
            "Die Release-Artefaktprüfung ist fehlgeschlagen.",
        )

    log_file = release_directory / "logs" / "party_player.log"
    initial_log = _log_signature(log_file)
    try:
        process = process_starter(executable, release_directory)
    except OSError as exc:
        return ReleaseSmokeResult(
            False,
            ReleaseSmokeCode.PROCESS_START_FAILED,
            f"Prozessstart fehlgeschlagen ({type(exc).__name__}).",
        )

    result: ReleaseSmokeResult
    deadline = clock() + max(0.0, timeout_seconds)
    try:
        while clock() < deadline:
            exit_code = process.poll()
            if exit_code is not None:
                result = ReleaseSmokeResult(
                    False,
                    ReleaseSmokeCode.PROCESS_EXITED_EARLY,
                    f"DeckRelay wurde vor Ablauf des Zeitlimits beendet ({exit_code}).",
                    process.pid,
                    exit_code,
                )
                break
            sleeper(max(0.001, poll_interval_seconds))
        else:
            current_log = _log_signature(log_file)
            if current_log is None or current_log == initial_log:
                result = ReleaseSmokeResult(
                    False,
                    ReleaseSmokeCode.LOG_NOT_CREATED,
                    "Das Release-Log wurde nicht neu erstellt oder aktualisiert.",
                    process.pid,
                )
            else:
                result = ReleaseSmokeResult(
                    True,
                    ReleaseSmokeCode.SUCCESS,
                    "Release gestartet, Log aktualisiert und Zeitlimit erreicht.",
                    process.pid,
                )
    finally:
        if process.poll() is None:
            try:
                process.terminate()
                process.wait(timeout=max(0.1, cleanup_timeout_seconds))
            except (OSError, subprocess.TimeoutExpired):
                try:
                    process.kill()
                    process.wait(timeout=max(0.1, cleanup_timeout_seconds))
                except (OSError, subprocess.TimeoutExpired):
                    result = ReleaseSmokeResult(
                        False,
                        ReleaseSmokeCode.CLEANUP_FAILED,
                        "Der gestartete Testprozess konnte nicht beendet werden.",
                        process.pid,
                    )
    return result
