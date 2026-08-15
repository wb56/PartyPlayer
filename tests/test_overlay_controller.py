from __future__ import annotations

from pathlib import Path
from datetime import datetime, timedelta
import logging
from time import monotonic, sleep
from threading import Event

import pytest

from party_player.audio.fake_backend import FakeAudioBackend
from party_player.controllers.overlay_controller import OverlayController
from party_player.crossfader_service import CrossfaderService
from party_player.deck_controller import DeckController
from party_player.ducking import DuckingController
from party_player.enums import DeckState
from party_player.models import Track
from party_player.overlay import OverlayDefinition, OverlayRuntime, OverlayStatus
from party_player.overlay import OverlayPlayResult
from party_player.overlay_player import OverlayAudioPlayer
from party_player.performance_monitor import PerformanceMonitor


def wait_until(predicate: object, timeout: float = 1.0) -> None:
    deadline = monotonic() + timeout
    while monotonic() < deadline:
        if callable(predicate) and predicate():
            return
        sleep(0.005)
    raise AssertionError("Overlay-Controller erreichte den Zielzustand nicht")


def build_controller(
    tmp_path: Path,
    performance_monitor: PerformanceMonitor | None = None,
) -> tuple[OverlayController, list[OverlayRuntime], list[float]]:
    file_path = tmp_path / "jingle.mp3"
    file_path.write_bytes(b"ID3")
    backend = FakeAudioBackend(duration=2.0)
    snapshots: list[OverlayRuntime] = []
    duck_values: list[float] = []
    holder: dict[str, OverlayController] = {}
    player = OverlayAudioPlayer(
        backend,
        on_status=lambda runtime: holder["controller"].player_status_changed(runtime),
    )
    ducking = DuckingController(duck_values.append)
    controller = OverlayController(
        player,
        ducking,
        publish_status=snapshots.append,
        duration_resolver=lambda _path: 2_000,
        performance_monitor=performance_monitor,
    )
    holder["controller"] = controller
    return controller, snapshots, duck_values


def overlay(tmp_path: Path, overlay_id: int = 1, name: str = "Tusch") -> OverlayDefinition:
    return OverlayDefinition(
        overlay_id,
        name,
        str(tmp_path / "jingle.mp3"),
        fade_in_ms=10,
        fade_out_ms=10,
        ducking_db=-8.0,
        ducking_attack_ms=10,
        ducking_release_ms=10,
    )


def test_prepare_start_and_finish_publish_events_and_restore_ducking(tmp_path: Path) -> None:
    controller, snapshots, duck_values = build_controller(tmp_path)
    controller.start(overlay(tmp_path))
    wait_until(lambda: controller.runtime.status == OverlayStatus.PLAYING)
    wait_until(lambda: any(value < 1.0 for value in duck_values))

    assert controller.fade_out()
    wait_until(lambda: controller.runtime.status == OverlayStatus.FINISHED)
    wait_until(lambda: duck_values[-1] == 1.0)

    assert OverlayStatus.PREPARING in [item.status for item in snapshots]
    assert OverlayStatus.PLAYING in [item.status for item in snapshots]
    assert snapshots[-1].status == OverlayStatus.FINISHED
    controller.close()


def test_overlay_switch_waits_for_safe_stop_then_starts_pending_item(tmp_path: Path) -> None:
    controller, snapshots, _duck_values = build_controller(tmp_path)
    first = overlay(tmp_path, 1, "Tusch")
    second = overlay(tmp_path, 2, "Applaus")
    controller.start(first)
    wait_until(lambda: controller.runtime.status == OverlayStatus.PLAYING)

    switch_event_start = len(snapshots)
    controller.start(second)
    wait_until(
        lambda: controller.runtime.status == OverlayStatus.PLAYING
        and controller.runtime.definition == second
    )

    played_names = [
        item.definition.name
        for item in snapshots
        if item.status == OverlayStatus.PLAYING and item.definition is not None
    ]
    assert played_names == ["Tusch", "Applaus"]
    switch_events = [
        (item.definition.name, item.status)
        for item in snapshots[switch_event_start:]
        if item.definition is not None
    ]
    expected_order = [
        ("Tusch", OverlayStatus.STOPPING),
        ("Tusch", OverlayStatus.FINISHED),
        ("Applaus", OverlayStatus.PREPARING),
        ("Applaus", OverlayStatus.READY),
        ("Applaus", OverlayStatus.FADING_IN),
        ("Applaus", OverlayStatus.PLAYING),
    ]
    positions = [switch_events.index(event) for event in expected_order]
    assert positions == sorted(positions)
    controller.close()


def test_same_overlay_can_be_retriggered_repeatedly(tmp_path: Path) -> None:
    controller, snapshots, _duck_values = build_controller(tmp_path)
    item = overlay(tmp_path)

    for expected_starts in range(1, 6):
        controller.start(item)
        wait_until(
            lambda: len(
                [snapshot for snapshot in snapshots if snapshot.status == OverlayStatus.PLAYING]
            )
            == expected_starts
        )

    played = [snapshot for snapshot in snapshots if snapshot.status == OverlayStatus.PLAYING]
    assert len(played) == 5
    assert all(snapshot.definition == item for snapshot in played)
    assert len({snapshot.generation for snapshot in played}) == 5
    assert controller.diagnostics()["pending_switch"] == ""
    controller.close()


def test_rapid_overlay_switches_start_only_the_last_selected_item(tmp_path: Path) -> None:
    controller, snapshots, _duck_values = build_controller(tmp_path)
    first = overlay(tmp_path, 1, "Tusch")
    second = overlay(tmp_path, 2, "Applaus")
    third = overlay(tmp_path, 3, "Hinweis")
    controller.start(first)
    wait_until(lambda: controller.runtime.status == OverlayStatus.PLAYING)

    controller.start(second)
    controller.start(third)
    assert controller.diagnostics()["pending_switch"] == "Hinweis"
    wait_until(
        lambda: controller.runtime.status == OverlayStatus.PLAYING
        and controller.runtime.definition == third
    )

    played_names = [
        item.definition.name
        for item in snapshots
        if item.status == OverlayStatus.PLAYING and item.definition is not None
    ]
    assert played_names == ["Tusch", "Hinweis"]
    assert controller.diagnostics()["pending_switch"] == ""
    controller.close()


def test_prepare_failure_affects_only_overlay_and_restores_ducking(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    controller, snapshots, duck_values = build_controller(tmp_path)
    missing = OverlayDefinition(1, "Fehlt", str(tmp_path / "missing.mp3"))

    with caplog.at_level(logging.ERROR):
        controller.start(missing)
    wait_until(lambda: controller.runtime.status == OverlayStatus.FAILED)

    assert snapshots[-1].status == OverlayStatus.FAILED
    assert not duck_values or duck_values[-1] == 1.0
    message = caplog.messages[-1]
    assert "overlay.failed" in message
    assert "overlay_id=1" in message
    assert "error_type=FileNotFoundError" in message
    assert str(tmp_path / "missing.mp3") in message
    controller.close()


def test_overlay_lifecycle_and_crossfade_progress_are_independent(
    tmp_path: Path,
) -> None:
    file_path = tmp_path / "jingle.mp3"
    file_path.write_bytes(b"ID3")
    deck_a = DeckController("A", FakeAudioBackend())
    deck_b = DeckController("B", FakeAudioBackend())
    deck_a.load(Track(1, "a.mp3", "A", "", "", 180.0), validate_file=False)
    deck_b.load(Track(2, "b.mp3", "B", "", "", 180.0), validate_file=False)
    deck_a.play()
    deck_b.play()
    mixer = CrossfaderService(deck_a, deck_b, position=0.4, master_volume=0.8)
    holder: dict[str, OverlayController] = {}
    player = OverlayAudioPlayer(
        FakeAudioBackend(duration=2.0),
        on_status=lambda runtime: holder["controller"].player_status_changed(runtime),
    )
    controller = OverlayController(
        player,
        DuckingController(mixer.set_ducking_factor),
        publish_status=lambda _runtime: None,
        duration_resolver=lambda _path: 2_000,
    )
    holder["controller"] = controller
    unchanged = (
        mixer.master_volume,
        deck_a.model.loaded_track,
        deck_b.model.loaded_track,
        deck_a.model.state,
        deck_b.model.state,
    )

    controller.start(
        OverlayDefinition(
            1,
            "Tusch",
            str(file_path),
            fade_in_ms=0,
            fade_out_ms=10,
            ducking_attack_ms=0,
            ducking_release_ms=0,
        )
    )
    wait_until(lambda: controller.runtime.status == OverlayStatus.PLAYING)
    assert deck_a.model.state == DeckState.PLAYING
    assert deck_b.model.state == DeckState.PLAYING

    for position in (0.55, 0.7, 0.85, 1.0):
        mixer.set_position(position)
        assert controller.runtime.status == OverlayStatus.PLAYING
        assert deck_a.model.state == DeckState.PLAYING
        assert deck_b.model.state == DeckState.PLAYING

    assert controller.fade_out()
    wait_until(lambda: controller.runtime.status == OverlayStatus.FINISHED)

    assert (
        mixer.master_volume,
        deck_a.model.loaded_track,
        deck_b.model.loaded_track,
        deck_a.model.state,
        deck_b.model.state,
    ) == unchanged
    assert mixer.position == 1.0
    controller.close()


def test_dispatcher_receives_status_callbacks(tmp_path: Path) -> None:
    callbacks: list[object] = []
    backend = FakeAudioBackend()
    ducking = DuckingController(lambda _factor: None)
    holder: dict[str, OverlayController] = {}
    player = OverlayAudioPlayer(
        backend,
        on_status=lambda runtime: holder["controller"].player_status_changed(runtime),
    )
    controller = OverlayController(
        player,
        ducking,
        publish_status=lambda _runtime: None,
        dispatch=callbacks.append,
        duration_resolver=lambda _path: 1_000,
    )
    holder["controller"] = controller
    file_path = tmp_path / "jingle.mp3"
    file_path.write_bytes(b"ID3")

    controller.start(OverlayDefinition(1, "Tusch", str(file_path), fade_in_ms=0))
    wait_until(lambda: len(callbacks) >= 3)

    assert all(callable(callback) for callback in callbacks)
    controller.close()


def test_manual_fade_is_recorded_once_outside_audio_completion(tmp_path: Path) -> None:
    file_path = tmp_path / "jingle.mp3"
    file_path.write_bytes(b"ID3")
    backend = FakeAudioBackend(duration=2.0)
    ducking = DuckingController(lambda _factor: None)
    holder: dict[str, OverlayController] = {}
    history: list[tuple[OverlayPlayResult, datetime, datetime]] = []
    now = datetime(2026, 7, 29, 20, 0)
    ticks = iter((now, now + timedelta(seconds=2)))
    player = OverlayAudioPlayer(
        backend,
        on_status=lambda runtime: holder["controller"].player_status_changed(runtime),
    )

    def record_history(
        _definition: OverlayDefinition,
        started_at: datetime,
        completed_at: datetime,
        result: OverlayPlayResult,
        _error: str,
    ) -> None:
        history.append((result, started_at, completed_at))

    controller = OverlayController(
        player,
        ducking,
        publish_status=lambda _runtime: None,
        duration_resolver=lambda _path: 2_000,
        record_history=record_history,
        wall_clock=lambda: next(ticks),
    )
    holder["controller"] = controller
    controller.start(OverlayDefinition(1, "Tusch", str(file_path), fade_in_ms=0, fade_out_ms=10))
    wait_until(lambda: controller.runtime.status == OverlayStatus.PLAYING)
    assert controller.fade_out()
    wait_until(lambda: len(history) == 1)

    assert history == [(OverlayPlayResult.FADED_OUT, now, now + timedelta(seconds=2))]
    controller.close()


def test_overlay_operations_publish_named_performance_diagnostics(tmp_path: Path) -> None:
    performance = PerformanceMonitor()
    controller, _snapshots, _duck_values = build_controller(tmp_path, performance)

    controller.start(overlay(tmp_path))
    wait_until(lambda: controller.runtime.status == OverlayStatus.PLAYING)
    assert controller.fade_out()
    wait_until(lambda: controller.runtime.status == OverlayStatus.FINISHED)

    statistics = performance.statistics()
    assert {
        "overlay.prepare",
        "overlay.start",
        "overlay.fade_out",
        "overlay.finish",
        "ducking.attack",
        "ducking.release",
    } <= statistics.keys()
    controller.close()


def test_prepare_duration_cache_hits_and_invalidates_on_file_change(tmp_path: Path) -> None:
    file_path = tmp_path / "jingle.mp3"
    file_path.write_bytes(b"ID3")
    backend = FakeAudioBackend(duration=2.0)
    ducking = DuckingController(lambda _factor: None)
    holder: dict[str, OverlayController] = {}
    resolutions = 0
    player = OverlayAudioPlayer(
        backend,
        on_status=lambda runtime: holder["controller"].player_status_changed(runtime),
    )

    def resolve(_path: Path) -> int:
        nonlocal resolutions
        resolutions += 1
        return 2_000

    controller = OverlayController(
        player,
        ducking,
        publish_status=lambda _runtime: None,
        duration_resolver=resolve,
    )
    holder["controller"] = controller
    item = OverlayDefinition(1, "Tusch", str(file_path), fade_in_ms=0, fade_out_ms=0)
    controller.start(item)
    wait_until(lambda: controller.runtime.status == OverlayStatus.PLAYING)
    controller.stop()
    wait_until(lambda: controller.runtime.status == OverlayStatus.FINISHED)
    controller.start(item)
    wait_until(lambda: controller.runtime.status == OverlayStatus.PLAYING)
    assert resolutions == 1
    assert controller.diagnostics()["prepare_cache_hits"] == 1

    controller.stop()
    wait_until(lambda: controller.runtime.status == OverlayStatus.FINISHED)
    file_path.write_bytes(b"ID3 changed")
    controller.start(item)
    wait_until(lambda: controller.runtime.status == OverlayStatus.PLAYING)
    assert resolutions == 2
    controller.close()


def test_stop_cancels_metadata_prepare_before_backend_adoption(tmp_path: Path) -> None:
    file_path = tmp_path / "jingle.mp3"
    file_path.write_bytes(b"ID3")
    backend = FakeAudioBackend(duration=2.0)
    ducking = DuckingController(lambda _factor: None)
    entered = Event()
    release = Event()
    holder: dict[str, OverlayController] = {}
    player = OverlayAudioPlayer(
        backend,
        on_status=lambda runtime: holder["controller"].player_status_changed(runtime),
    )

    def slow_duration(_path: Path) -> int:
        entered.set()
        release.wait(1.0)
        return 2_000

    controller = OverlayController(
        player,
        ducking,
        publish_status=lambda _runtime: None,
        duration_resolver=slow_duration,
    )
    holder["controller"] = controller
    controller.start(OverlayDefinition(1, "Langsam", str(file_path)))
    assert entered.wait(1.0)
    assert controller.runtime.status == OverlayStatus.PREPARING

    assert controller.stop()
    release.set()
    wait_until(lambda: controller.diagnostics()["prepare_aborted_count"] == 1)

    assert not backend.is_playing()
    controller.close()


def test_second_overlay_supersedes_stale_slow_prepare_callback(tmp_path: Path) -> None:
    first_path = tmp_path / "slow.mp3"
    second_path = tmp_path / "next.mp3"
    first_path.write_bytes(b"ID3")
    second_path.write_bytes(b"ID3")
    backend = FakeAudioBackend(duration=2.0)
    duck_values: list[float] = []
    ducking = DuckingController(duck_values.append)
    entered = Event()
    release = Event()
    holder: dict[str, OverlayController] = {}
    snapshots: list[OverlayRuntime] = []
    player = OverlayAudioPlayer(
        backend,
        on_status=lambda runtime: holder["controller"].player_status_changed(runtime),
    )

    def duration(path: Path) -> int:
        if path == first_path:
            entered.set()
            release.wait(1.0)
        return 2_000

    controller = OverlayController(
        player,
        ducking,
        publish_status=snapshots.append,
        duration_resolver=duration,
    )
    holder["controller"] = controller
    first = OverlayDefinition(1, "Langsam", str(first_path), ducking_db=-8.0, ducking_attack_ms=0)
    second = OverlayDefinition(2, "Danach", str(second_path), ducking_db=-8.0, ducking_attack_ms=0)

    controller.start(first)
    assert entered.wait(1.0)
    controller.start(second)
    release.set()
    wait_until(
        lambda: controller.runtime.status == OverlayStatus.PLAYING
        and controller.runtime.definition == second
    )

    assert not any(
        item.status == OverlayStatus.PLAYING and item.definition == first for item in snapshots
    )
    assert controller.diagnostics()["prepare_aborted_count"] == 1
    assert ducking.factor < 1.0
    controller.close()
    assert duck_values[-1] == 1.0


def test_close_records_stop_and_restores_ducking(tmp_path: Path) -> None:
    file_path = tmp_path / "jingle.mp3"
    file_path.write_bytes(b"ID3")
    backend = FakeAudioBackend(duration=2.0)
    duck_values: list[float] = []
    ducking = DuckingController(duck_values.append)
    holder: dict[str, OverlayController] = {}
    results: list[OverlayPlayResult] = []
    player = OverlayAudioPlayer(
        backend,
        on_status=lambda runtime: holder["controller"].player_status_changed(runtime),
    )

    def record(
        _definition: OverlayDefinition,
        _started_at: datetime,
        _completed_at: datetime,
        result: OverlayPlayResult,
        _error: str,
    ) -> None:
        results.append(result)

    controller = OverlayController(
        player,
        ducking,
        publish_status=lambda _runtime: None,
        duration_resolver=lambda _path: 2_000,
        record_history=record,
    )
    holder["controller"] = controller
    controller.start(
        OverlayDefinition(
            1,
            "Tusch",
            str(file_path),
            fade_in_ms=0,
            ducking_db=-8.0,
            ducking_attack_ms=0,
        )
    )
    wait_until(lambda: controller.runtime.status == OverlayStatus.PLAYING)
    wait_until(lambda: ducking.factor < 1.0)

    controller.close()

    assert results == [OverlayPlayResult.STOPPED]
    assert duck_values[-1] == 1.0
    assert not backend.is_playing()
