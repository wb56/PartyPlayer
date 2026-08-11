import logging

import pytest

from party_player.performance_monitor import GuiHeartbeat, PerformanceMonitor, PerformanceSettings


class Clock:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value


def test_monitor_aggregates_and_logs_only_slow_operations(caplog: pytest.LogCaptureFixture) -> None:
    clock = Clock()
    monitor = PerformanceMonitor(clock=clock, warning_rate_limit_seconds=5.0)
    with caplog.at_level(logging.WARNING):
        with monitor.measure("fast", warning_threshold_ms=50):
            clock.value += 0.010
        with monitor.measure("slow", warning_threshold_ms=50):
            clock.value += 0.080

    statistics = monitor.statistics()
    assert statistics["fast"].average_duration_ms == pytest.approx(10)
    assert statistics["slow"].maximum_duration_ms == pytest.approx(80)
    assert statistics["slow"].slow_operation_count == 1
    assert "slow" in caplog.text
    assert "fast" not in caplog.text


def test_monitor_preserves_exception_from_measured_block() -> None:
    clock = Clock()
    monitor = PerformanceMonitor(clock=clock)
    with pytest.raises(ValueError, match="original"):
        with monitor.measure("failure", warning_threshold_ms=50):
            clock.value += 0.1
            raise ValueError("original")
    assert monitor.statistics()["failure"].count == 1


def test_disabled_monitor_collects_and_logs_nothing(caplog: pytest.LogCaptureFixture) -> None:
    clock = Clock()
    monitor = PerformanceMonitor(clock=clock, enabled=False)
    with caplog.at_level(logging.WARNING):
        with monitor.measure("disabled", warning_threshold_ms=1):
            clock.value += 10

    assert monitor.statistics() == {}
    assert caplog.text == ""


def test_statistics_can_be_reset_between_scenarios() -> None:
    monitor = PerformanceMonitor()
    monitor.record("before", 10.0, 50.0)

    monitor.reset_statistics()
    monitor.record("during", 5.0, 50.0)

    assert set(monitor.statistics()) == {"during"}


def test_signed_measurements_preserve_negative_maximum_and_absolute_peak() -> None:
    monitor = PerformanceMonitor()
    monitor.record("crossfade.duration_deviation_ms", -90.0, 100.0)
    monitor.record("crossfade.duration_deviation_ms", -25.0, 100.0)

    stats = monitor.statistics()["crossfade.duration_deviation_ms"]
    assert stats.minimum_value_ms == -90.0
    assert stats.maximum_value_ms == -25.0
    assert stats.maximum_duration_ms == -25.0
    assert stats.average_value_ms == -57.5
    assert stats.maximum_absolute_value_ms == 90.0


def test_samples_exceeding_scenario_duration_are_rejected() -> None:
    clock = Clock()
    monitor = PerformanceMonitor(clock=clock)
    monitor.begin_scenario()

    timing = monitor.record("gui_callback.after.test", 500.0, 50.0)

    assert monitor.statistics() == {}
    assert timing.context["measurement_status"] == "invalid"
    assert timing.context["measurement_reason"] == "exceeds_scenario_duration"
    assert monitor.validation_counters()["invalid_timing_sample_count"] == 1


def test_nested_measurements_keep_independent_local_start_values() -> None:
    clock = Clock()
    monitor = PerformanceMonitor(clock=clock)
    with monitor.measure("outer", warning_threshold_ms=100):
        clock.value += 0.010
        with monitor.measure("inner", warning_threshold_ms=100):
            clock.value += 0.020
        clock.value += 0.005

    assert monitor.statistics()["inner"].maximum_duration_ms == pytest.approx(20)
    assert monitor.statistics()["outer"].maximum_duration_ms == pytest.approx(35)


def test_gui_heartbeat_detects_delay_rate_limits_and_reports_recovery(
    caplog: pytest.LogCaptureFixture,
) -> None:
    clock = Clock()
    settings = PerformanceSettings(
        gui_heartbeat_warning_ms=250,
        gui_heartbeat_critical_ms=750,
        slow_warning_rate_limit_seconds=5,
    )
    heartbeat = GuiHeartbeat(settings, clock=clock)
    heartbeat.start()
    with caplog.at_level(logging.INFO):
        clock.value = 0.5
        heartbeat.beat()
        clock.value = 1.5
        heartbeat.beat()
        clock.value = 1.6
        heartbeat.beat()

    stats = heartbeat.statistics()
    assert stats.warning_count == 2
    assert stats.critical_count == 1
    assert stats.maximum_delay_ms == pytest.approx(900)
    assert caplog.text.count("GUI-Heartbeat verzögert") == 1
    assert "GUI-Heartbeat erholt" in caplog.text


def test_monitor_exposes_counters_and_gauges() -> None:
    monitor = PerformanceMonitor()

    assert monitor.increment_counter("checks.success") == 1
    assert monitor.increment_counter("checks.success", 2) == 3
    monitor.set_gauge("capability.playback", True)

    assert monitor.counters() == {"checks.success": 3}
    assert monitor.gauges() == {"capability.playback": 1.0}


def test_disabled_monitor_does_not_collect_counters_or_gauges() -> None:
    monitor = PerformanceMonitor(enabled=False)

    assert monitor.increment_counter("checks.success") == 0
    monitor.set_gauge("capability.playback", True)

    assert monitor.counters() == {}
    assert monitor.gauges() == {}
