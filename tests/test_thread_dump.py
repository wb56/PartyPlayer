from pathlib import Path

from party_player.thread_dump import ThreadDumpWriter


def test_critical_delay_writes_main_thread_stack(tmp_path: Path) -> None:
    now = [100.0]
    writer = ThreadDumpWriter(tmp_path, clock=lambda: now[0])

    target = writer.write(1250.0, "queue_stress", "Deck A=playing", "pending=0")

    assert target is not None
    report = target.read_text(encoding="utf-8")
    assert "Heartbeat delay: 1250.0 ms" in report
    assert "Test context: queue_stress" in report
    assert "Thread MainThread" in report
    assert "test_critical_delay_writes_main_thread_stack" in report


def test_thread_dump_is_rate_limited_to_one_per_minute(tmp_path: Path) -> None:
    now = [100.0]
    writer = ThreadDumpWriter(tmp_path, clock=lambda: now[0])

    assert writer.write(900.0, "idle", "idle", "pending=0") is not None
    now[0] += 59.0
    assert writer.write(950.0, "idle", "idle", "pending=0") is None
    now[0] += 1.0
    assert writer.write(1000.0, "idle", "idle", "pending=0") is not None
