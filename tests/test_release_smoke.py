from pathlib import Path
import subprocess

from party_player.release_smoke import ReleaseSmokeCode, run_release_smoke_test


class FakeProcess:
    def __init__(self, polls: list[int | None], *, cleanup_fails: bool = False) -> None:
        self.pid = 1234
        self._polls = polls
        self._last: int | None = None
        self.cleanup_fails = cleanup_fails
        self.terminated = False
        self.killed = False

    def poll(self) -> int | None:
        if self._polls:
            self._last = self._polls.pop(0)
        return self._last

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.killed = True

    def wait(self, timeout: float | None = None) -> int:
        if self.cleanup_fails:
            raise subprocess.TimeoutExpired("PartyPlayer.exe", timeout)
        self._last = 0
        return 0


def release(tmp_path: Path) -> Path:
    root = tmp_path / "PartyPlayer"
    (root / "logs").mkdir(parents=True)
    (root / "PartyPlayer.exe").write_bytes(b"exe")
    return root


def test_artifact_failure_prevents_process_start(tmp_path: Path) -> None:
    root = release(tmp_path)
    started = False

    def starter(_executable: Path, _cwd: Path) -> FakeProcess:
        nonlocal started
        started = True
        return FakeProcess([])

    result = run_release_smoke_test(
        root, artifact_checker=lambda _root: False, process_starter=starter
    )

    assert result.code is ReleaseSmokeCode.ARTIFACT_CHECK_FAILED
    assert not started


def test_early_exit_is_reported_without_cleanup(tmp_path: Path) -> None:
    process = FakeProcess([7])
    result = run_release_smoke_test(
        release(tmp_path),
        artifact_checker=lambda _root: True,
        process_starter=lambda _exe, _cwd: process,
        timeout_seconds=1,
    )

    assert result.code is ReleaseSmokeCode.PROCESS_EXITED_EARLY
    assert result.exit_code == 7
    assert not process.terminated


def test_missing_log_is_reported_and_only_started_process_is_stopped(tmp_path: Path) -> None:
    process = FakeProcess([None])
    ticks = iter((0.0, 2.0))
    result = run_release_smoke_test(
        release(tmp_path),
        artifact_checker=lambda _root: True,
        process_starter=lambda _exe, _cwd: process,
        timeout_seconds=1,
        clock=lambda: next(ticks),
        sleeper=lambda _delay: None,
    )

    assert result.code is ReleaseSmokeCode.LOG_NOT_CREATED
    assert result.pid == 1234
    assert process.terminated


def test_success_requires_fresh_log_and_cleans_up(tmp_path: Path) -> None:
    root = release(tmp_path)
    process = FakeProcess([None])
    ticks = iter((0.0, 2.0))

    def starter(_exe: Path, _cwd: Path) -> FakeProcess:
        (root / "logs" / "party_player.log").write_text("started", encoding="utf-8")
        return process

    result = run_release_smoke_test(
        root,
        artifact_checker=lambda _root: True,
        process_starter=starter,
        timeout_seconds=1,
        clock=lambda: next(ticks),
        sleeper=lambda _delay: None,
    )

    assert result.code is ReleaseSmokeCode.SUCCESS
    assert result.success
    assert process.terminated


def test_cleanup_failure_overrides_success(tmp_path: Path) -> None:
    root = release(tmp_path)
    process = FakeProcess([None], cleanup_fails=True)
    ticks = iter((0.0, 2.0))

    def starter(_exe: Path, _cwd: Path) -> FakeProcess:
        (root / "logs" / "party_player.log").write_text("started", encoding="utf-8")
        return process

    result = run_release_smoke_test(
        root,
        artifact_checker=lambda _root: True,
        process_starter=starter,
        timeout_seconds=1,
        clock=lambda: next(ticks),
        sleeper=lambda _delay: None,
    )

    assert result.code is ReleaseSmokeCode.CLEANUP_FAILED
    assert process.terminated and process.killed
