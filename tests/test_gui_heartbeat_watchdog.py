from pathlib import Path
from threading import Thread
from time import monotonic, sleep

from party_player.gui_callback import measured_gui_callback
from party_player.gui_heartbeat_watchdog import GuiCallbackState, GuiHeartbeatWatchdog
from party_player.performance_monitor import PerformanceMonitor
from party_player.thread_dump import ThreadDumpWriter


def wait_for_files(directory: Path, expected: int, timeout: float = 1.0) -> list[Path]:
    deadline = monotonic() + timeout
    while monotonic() < deadline:
        files = list(directory.glob("deckrelay-thread-dump-*.txt"))
        if len(files) >= expected:
            return files
        sleep(0.01)
    return list(directory.glob("deckrelay-thread-dump-*.txt"))


def test_watchdog_captures_blocking_main_thread_function(tmp_path: Path) -> None:
    state = GuiCallbackState()
    watchdog = GuiHeartbeatWatchdog(
        state,
        diagnostics_directory=tmp_path,
        test_context=lambda: "queue_stress",
        playback_state=lambda: "Deck A=playing",
        dispatcher_state=lambda: "pending=0",
        interval_seconds=0.01,
        warning_threshold_ms=20.0,
        critical_threshold_ms=50.0,
    )

    def artificially_blocking_callback() -> None:
        state.mark_started("test.artificially_blocking_callback")
        try:
            sleep(0.15)
        finally:
            state.mark_completed("test.artificially_blocking_callback")

    watchdog.start()
    try:
        artificially_blocking_callback()
        files = wait_for_files(tmp_path, 1)
    finally:
        watchdog.stop()

    assert len(files) == 1
    report = files[0].read_text(encoding="utf-8")
    assert "artificially_blocking_callback" in report
    assert "active_gui_callback: test.artificially_blocking_callback" in report
    assert "_heartbeat_tick" not in report
    assert "Thread MainThread" in report


def test_watchdog_captures_later_block_after_recovery(tmp_path: Path) -> None:
    state = GuiCallbackState()
    writer = ThreadDumpWriter(tmp_path, rate_limit_seconds=0.05)
    watchdog = GuiHeartbeatWatchdog(
        state,
        test_context=lambda: "idle",
        playback_state=lambda: "idle",
        dispatcher_state=lambda: "pending=0",
        interval_seconds=0.01,
        warning_threshold_ms=15.0,
        critical_threshold_ms=30.0,
        writer=writer,
    )
    watchdog.start()
    try:
        sleep(0.06)
        assert len(wait_for_files(tmp_path, 1)) == 1
        for _ in range(6):
            state.heartbeat()
            sleep(0.01)
        sleep(0.08)
        files = wait_for_files(tmp_path, 2)
    finally:
        watchdog.stop()

    assert len(files) == 2


def test_watchdog_stops_cleanly_and_uses_no_tk_dependency(tmp_path: Path) -> None:
    state = GuiCallbackState()
    calls: list[str] = []
    watchdog = GuiHeartbeatWatchdog(
        state,
        diagnostics_directory=tmp_path,
        test_context=lambda: calls.append("context") or "idle",
        playback_state=lambda: calls.append("playback") or "idle",
        dispatcher_state=lambda: calls.append("dispatcher") or "pending=0",
        interval_seconds=0.01,
        warning_threshold_ms=10.0,
        critical_threshold_ms=20.0,
    )
    watchdog.start()
    sleep(0.04)
    watchdog.stop()

    assert not watchdog.is_running
    assert calls == ["context", "playback", "dispatcher"]


def test_measured_callback_publishes_active_and_completed_state() -> None:
    state = GuiCallbackState()
    observed: list[str | None] = []

    def callback() -> None:
        observed.append(state.snapshot().active_gui_callback)

    wrapped = measured_gui_callback(
        PerformanceMonitor(),
        "command.test",
        callback,
        callback_state=state,
    )
    wrapped()

    assert observed == ["gui_callback.command.test"]
    snapshot = state.snapshot()
    assert snapshot.active_gui_callback is None
    assert snapshot.last_started_gui_callback == "gui_callback.command.test"
    assert snapshot.last_completed_gui_callback == "gui_callback.command.test"


def test_callback_state_locking_does_not_deadlock_under_concurrent_snapshots() -> None:
    state = GuiCallbackState()

    def publish_many() -> None:
        for _ in range(2000):
            state.mark_started("test.callback")
            state.snapshot()
            state.mark_completed("test.callback")
            state.heartbeat()

    worker = Thread(target=publish_many)
    worker.start()
    for _ in range(2000):
        state.snapshot()
    worker.join(1.0)

    assert not worker.is_alive()


def test_callback_state_publishes_layout_context_without_tk_access() -> None:
    state = GuiCallbackState(clock=lambda: 20.0)
    state.mark_started("catalog_render_chunk")
    state.mark_completed("catalog_render_chunk")
    state.update_layout_state(
        pending_layout_refreshes=1,
        pending_focus_request=True,
        pending_catalog_chunks=4,
        pending_queue_chunks=2,
        catalog_rows_created=18,
        queue_rows_created=12,
    )

    snapshot = state.snapshot()

    assert snapshot.last_completed_gui_callback_at == 20.0
    assert snapshot.pending_layout_refreshes == 1
    assert snapshot.pending_focus_request
    assert snapshot.pending_catalog_chunks == 4
    assert snapshot.pending_queue_chunks == 2
    assert snapshot.catalog_rows_created == 18
    assert snapshot.queue_rows_created == 12
