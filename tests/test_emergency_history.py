from pathlib import Path

from party_player.database.connection import Database
from party_player.database.migrations import migrate
from party_player.emergency_history import (
    EmergencyHistoryEntry,
    EmergencyHistoryRepository,
    EmergencyHistoryService,
)


def test_confirmed_emergency_start_is_stored_with_separate_source(tmp_path: Path) -> None:
    database = Database(tmp_path / "emergency-history.db")
    migrate(database)
    service = EmergencyHistoryService(EmergencyHistoryRepository(database))

    assert service.record_started(
        EmergencyHistoryEntry(
            12,
            91,
            "B",
            "PRIMARY",
            "Local Emergency",
            str(tmp_path / "emergency.mp3"),
            2.5,
            -1.0,
            True,
        )
    )
    service.close()

    with database.connect() as connection:
        row = connection.execute("SELECT * FROM emergency_play_history").fetchone()
    assert row is not None
    assert row["source"] == "EMERGENCY"
    assert row["session_id"] == 12
    assert row["track_id"] == 91
    assert row["deck_id"] == "B"
    assert row["media_type"] == "PRIMARY"
    assert row["cue_in"] == 2.5
    assert row["clip_protection_enabled"] == 1


def test_restore_participant_blocks_and_resumes_history_writes(tmp_path: Path) -> None:
    database = Database(tmp_path / "emergency-history-lifecycle.db")
    migrate(database)
    service = EmergencyHistoryService(EmergencyHistoryRepository(database))
    participant = service.restore_participant()
    entry = EmergencyHistoryEntry(1, 2, "A", "PRIMARY", "Test", "local.mp3", 0, 0, True)

    assert participant.block_new_work()
    assert not service.record_started(entry)
    assert participant.drain(1.0)
    assert participant.close_connections()
    assert participant.resume()
    assert service.record_started(entry)
    service.close()

    with database.connect() as connection:
        count = connection.execute("SELECT COUNT(*) FROM emergency_play_history").fetchone()
    assert count is not None and count[0] == 1
