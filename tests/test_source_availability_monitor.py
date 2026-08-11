from pathlib import Path
from threading import Event
from time import monotonic

from party_player.source_availability_monitor import (
    SourceAvailabilityMonitor,
    SourceAvailabilityState,
)


def test_readable_source_is_reported_available(tmp_path: Path) -> None:
    audio = tmp_path / "song.mp3"
    audio.write_bytes(b"audio")

    result = SourceAvailabilityMonitor().check(str(audio))

    assert result.state == SourceAvailabilityState.AVAILABLE
    assert result.checked_at


def test_missing_source_is_reported_unavailable(tmp_path: Path) -> None:
    result = SourceAvailabilityMonitor().check(str(tmp_path / "missing.mp3"))

    assert result.state == SourceAvailabilityState.UNAVAILABLE
    assert result.reason


def test_hanging_network_probe_is_bounded_by_timeout() -> None:
    release = Event()

    def blocking_probe(_path: Path) -> None:
        release.wait(timeout=2)

    monitor = SourceAvailabilityMonitor(timeout_seconds=0.05, probe=blocking_probe)
    started = monotonic()

    result = monitor.check(r"\\nas\party\song.mp3")

    assert monotonic() - started < 0.5
    assert result.state == SourceAvailabilityState.TIMEOUT
    release.set()
