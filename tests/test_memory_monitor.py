from party_player.memory_monitor import MemoryMonitor, process_rss, process_rss_bytes


def sample(monitor: MemoryMonitor, value: int) -> None:
    monitor.sample(
        gui_event_queue_size=value,
        active_worker_count=0,
        cover_cache_size=2,
        registered_widget_count=10,
        active_preview_count=0,
        active_vlc_player_count=2,
    )


def test_memory_samples_have_fixed_capacity_and_native_rss() -> None:
    monitor = MemoryMonitor(maximum_samples=3)
    for value in range(10):
        sample(monitor, value)

    assert monitor.sample_count() == 3
    assert monitor.latest() is not None
    assert monitor.latest().gui_event_queue_size == 9  # type: ignore[union-attr]
    rss, status = process_rss()
    assert rss is None or rss > 0
    rss_value = process_rss_bytes()
    assert rss_value is None or rss_value > 0
    assert status == "available" or status in {
        "unavailable",
        "psutil_not_available",
    }


def test_tracemalloc_snapshot_comparison_is_bounded_and_close_is_idempotent() -> None:
    monitor = MemoryMonitor(maximum_growth_entries=3)
    assert monitor.enable_tracemalloc()
    assert monitor.begin_snapshot()
    retained = [bytearray(1024) for _ in range(20)]

    growth = monitor.end_snapshot()
    monitor.close()
    monitor.close()

    assert retained
    assert len(growth) <= 3
    assert not monitor.enabled


def test_memory_stress_cycle_history_is_bounded_and_reports_widget_deltas() -> None:
    monitor = MemoryMonitor(maximum_samples=3)
    sample(monitor, 1)
    for cycle_number in range(12):
        monitor.record_stress_cycle(
            cycle_number,
            100,
            {
                "queue_row_views": 14,
                "tk_widget_count": 400,
                "tooltip_instances_current": 20,
                "widgets_created_total": 84,
                "widgets_destroyed_total": 0,
            },
        )

    cycles = monitor.stress_cycles()
    assert len(cycles) == 10
    assert cycles[-1].queue_row_views == 14
    assert cycles[-1].widgets_created_delta == 0
