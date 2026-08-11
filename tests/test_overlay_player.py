from __future__ import annotations

from pathlib import Path
from time import monotonic, sleep

import pytest

from party_player.audio.fake_backend import FakeAudioBackend
from party_player.overlay import OverlayDefinition, OverlayStatus
from party_player.overlay_player import OverlayAudioPlayer


class FailingPrepareBackend(FakeAudioBackend):
    def prepare(self, file_path: Path) -> object:
        raise RuntimeError(f"VLC konnte Medium nicht vorbereiten: {file_path}")


class TrackingCloseBackend(FakeAudioBackend):
    def __init__(self, duration: float = 180.0) -> None:
        super().__init__(duration)
        self.closed = False

    def close(self) -> None:
        self.closed = True
        super().close()


def audio_file(tmp_path: Path) -> Path:
    path = tmp_path / "jingle.mp3"
    path.write_bytes(b"ID3")
    return path


def wait_until(predicate: object, timeout: float = 1.0) -> None:
    check = predicate
    deadline = monotonic() + timeout
    while monotonic() < deadline:
        if callable(check) and check():
            return
        sleep(0.005)
    raise AssertionError("Erwarteter Overlayzustand wurde nicht erreicht")


def test_prepare_start_and_fade_use_independent_backend(tmp_path: Path) -> None:
    backend = FakeAudioBackend(duration=2.0)
    statuses: list[OverlayStatus] = []
    player = OverlayAudioPlayer(backend, on_status=lambda item: statuses.append(item.status))
    item = OverlayDefinition(
        1,
        "Tusch",
        str(audio_file(tmp_path)),
        fade_in_ms=10,
        fade_out_ms=10,
    )

    generation = player.prepare(item, duration_ms=2_000)
    assert player.start(generation)
    wait_until(lambda: player.runtime.status == OverlayStatus.PLAYING)
    assert player.fade_out()
    assert player.fade_out()
    wait_until(lambda: player.runtime.status == OverlayStatus.FINISHED)

    assert statuses[:3] == [
        OverlayStatus.PREPARING,
        OverlayStatus.READY,
        OverlayStatus.FADING_IN,
    ]
    assert not backend.is_playing()
    player.close()


def test_stop_during_prepare_state_finishes_safely(tmp_path: Path) -> None:
    backend = FakeAudioBackend(duration=2.0)
    player = OverlayAudioPlayer(backend)
    item = OverlayDefinition(1, "Tusch", str(audio_file(tmp_path)))
    generation = player.prepare(item, duration_ms=2_000)
    assert player.start(generation)

    assert player.stop()
    assert player.stop()
    wait_until(lambda: player.runtime.status == OverlayStatus.FINISHED)
    assert not backend.is_playing()
    player.close()


def test_cue_out_triggers_fade_without_waiting_for_file_end(tmp_path: Path) -> None:
    backend = FakeAudioBackend(duration=10.0)
    player = OverlayAudioPlayer(backend)
    item = OverlayDefinition(
        1,
        "Kurz",
        str(audio_file(tmp_path)),
        cue_out_ms=2_000,
        fade_in_ms=0,
        fade_out_ms=10,
    )
    generation = player.prepare(item, duration_ms=10_000)
    assert player.start(generation)
    backend.position = 1.995

    player.update_position()
    wait_until(lambda: player.runtime.status == OverlayStatus.FINISHED)

    assert not backend.is_playing()
    player.close()


def test_position_update_publishes_progress_before_cue_out(tmp_path: Path) -> None:
    backend = FakeAudioBackend(duration=10.0)
    snapshots = []
    player = OverlayAudioPlayer(backend, on_status=snapshots.append)
    item = OverlayDefinition(
        1,
        "Lang",
        str(audio_file(tmp_path)),
        fade_in_ms=0,
        fade_out_ms=100,
    )
    generation = player.prepare(item, duration_ms=10_000)
    assert player.start(generation)
    backend.position = 2.5

    player.update_position()

    assert player.runtime.position_ms == 2_500
    assert snapshots[-1].position_ms == 2_500
    assert player.runtime.status == OverlayStatus.PLAYING
    player.close()


def test_natural_backend_completion_finishes_overlay(tmp_path: Path) -> None:
    backend = FakeAudioBackend(duration=2.0)
    player = OverlayAudioPlayer(backend)
    item = OverlayDefinition(
        1,
        "Kurz",
        str(audio_file(tmp_path)),
        fade_in_ms=0,
        fade_out_ms=0,
    )
    generation = player.prepare(item, duration_ms=2_000)
    assert player.start(generation)
    backend.position = 1.0
    backend.playing = False
    backend.finished = True

    player.update_position()

    assert player.runtime.status == OverlayStatus.FINISHED
    assert not backend.is_playing()
    player.close()


def test_backend_prepare_failure_sets_only_overlay_failed(tmp_path: Path) -> None:
    backend = FailingPrepareBackend()
    snapshots = []
    player = OverlayAudioPlayer(backend, on_status=snapshots.append)
    item = OverlayDefinition(1, "Defekt", str(audio_file(tmp_path)))

    with pytest.raises(RuntimeError, match="VLC"):
        player.prepare(item, duration_ms=2_000)

    assert player.runtime.status == OverlayStatus.FAILED
    assert snapshots[-1].status == OverlayStatus.FAILED
    assert "VLC" in player.runtime.error
    assert not backend.is_playing()
    player.close()


def test_close_is_idempotent_and_rejects_new_work(tmp_path: Path) -> None:
    backend = FakeAudioBackend()
    player = OverlayAudioPlayer(backend)
    player.close()
    player.close()

    item = OverlayDefinition(1, "Tusch", str(audio_file(tmp_path)))
    try:
        player.prepare(item, duration_ms=1_000)
    except RuntimeError as exc:
        assert "geschlossen" in str(exc)
    else:
        raise AssertionError("Geschlossener Overlay-Player akzeptierte Prepare")


def test_close_during_active_playback_stops_and_releases_backend(tmp_path: Path) -> None:
    backend = TrackingCloseBackend(duration=2.0)
    player = OverlayAudioPlayer(backend)
    item = OverlayDefinition(
        1,
        "Tusch",
        str(audio_file(tmp_path)),
        fade_in_ms=0,
    )
    generation = player.prepare(item, duration_ms=2_000)
    assert player.start(generation)
    assert backend.is_playing()

    player.close()

    assert not backend.is_playing()
    assert backend.closed


def test_stop_and_wait_finishes_safety_fade_before_close(tmp_path: Path) -> None:
    backend = FakeAudioBackend(duration=2.0)
    player = OverlayAudioPlayer(backend)
    item = OverlayDefinition(1, "Tusch", str(audio_file(tmp_path)), fade_in_ms=0)
    generation = player.prepare(item, duration_ms=2_000)
    assert player.start(generation)

    assert player.stop_and_wait()

    assert player.runtime.status == OverlayStatus.FINISHED
    assert not backend.is_playing()
    assert backend.volume == 0.0
    player.close()


def test_master_mute_silences_and_restores_overlay_without_changing_state(
    tmp_path: Path,
) -> None:
    backend = FakeAudioBackend(duration=2.0)
    player = OverlayAudioPlayer(backend)
    item = OverlayDefinition(
        1,
        "Tusch",
        str(audio_file(tmp_path)),
        volume_percent=75,
        fade_in_ms=0,
    )
    generation = player.prepare(item, duration_ms=2_000)
    assert player.start(generation)
    assert backend.volume == 0.75

    player.set_master_muted(True)
    assert backend.volume == 0.0
    assert player.runtime.status == OverlayStatus.PLAYING

    player.set_master_muted(False)
    assert backend.volume == 0.75
    assert player.runtime.status == OverlayStatus.PLAYING
    player.close()
