"""File-availability decisions used before queue preparation."""

from pathlib import Path

from party_player.file_availability import FileAvailabilityService
from party_player.models import Track


def _track(path: Path) -> Track:
    return Track(1, str(path), "Titel", "Interpret", "", 60.0)


def test_available_supported_file_is_accepted(tmp_path: Path) -> None:
    path = tmp_path / "song.mp3"
    path.write_bytes(b"audio")

    decision = FileAvailabilityService().evaluate(_track(path))

    assert decision.accepted


def test_missing_file_has_stable_failure_code(tmp_path: Path) -> None:
    decision = FileAvailabilityService().evaluate(_track(tmp_path / "missing.flac"))

    assert not decision.accepted
    assert decision.code == "FILE_MISSING"


def test_unsupported_format_is_rejected_before_file_access(tmp_path: Path) -> None:
    decision = FileAvailabilityService().evaluate(_track(tmp_path / "song.wav"))

    assert not decision.accepted
    assert decision.code == "UNSUPPORTED_FORMAT"


def test_network_failure_is_retried_with_injected_delay() -> None:
    class FlakyNetworkAvailability(FileAvailabilityService):
        checks = 0

        @staticmethod
        def _check_readable(_path: Path) -> None:
            FlakyNetworkAvailability.checks += 1
            if FlakyNetworkAvailability.checks < 3:
                raise OSError("share unavailable")

    delays: list[float] = []
    service = FlakyNetworkAvailability(
        network_retry_attempts=2,
        network_retry_delay_seconds=3.0,
        sleeper=delays.append,
    )

    decision = service.evaluate(_track(Path("//server/share/song.mp3")))

    assert decision.accepted
    assert FlakyNetworkAvailability.checks == 3
    assert delays == [3.0, 3.0]


def test_unreachable_network_is_not_reported_as_permanently_missing() -> None:
    class UnavailableNetwork(FileAvailabilityService):
        @staticmethod
        def _check_readable(_path: Path) -> None:
            raise FileNotFoundError

    decision = UnavailableNetwork(
        network_retry_attempts=2,
        network_retry_delay_seconds=0,
    ).evaluate(_track(Path("//server/share/song.flac")))

    assert not decision.accepted
    assert decision.code == "NETWORK_UNAVAILABLE"


def test_cancelled_network_retry_does_not_run_a_stale_second_check() -> None:
    class UnavailableNetwork(FileAvailabilityService):
        checks = 0

        @staticmethod
        def _check_readable(_path: Path) -> None:
            UnavailableNetwork.checks += 1
            raise OSError("share unavailable")

    cancelled = [False]

    def cancel_during_retry(_delay: float) -> None:
        cancelled[0] = True

    decision = UnavailableNetwork(
        network_retry_attempts=3,
        network_retry_delay_seconds=3,
        sleeper=cancel_during_retry,
    ).evaluate(
        _track(Path("//server/share/song.mp3")),
        cancelled=lambda: cancelled[0],
    )

    assert not decision.accepted
    assert decision.code == "CANDIDATE_CANCELLED"
    assert UnavailableNetwork.checks == 1
