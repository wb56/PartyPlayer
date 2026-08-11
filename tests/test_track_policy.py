"""Persistent track-policy selection tests."""

from pathlib import Path

from party_player.database.connection import Database
from party_player.database.migrations import migrate
from party_player.enums import QueueStatus
from party_player.models import QueueEntry
from party_player.repositories.track_repository import TrackRepository
from party_player.track_policy import (
    PersistentTrackBlockService,
    TrackPolicyRepository,
    TrackPolicyStatus,
)


def _database(path: Path) -> Database:
    database = Database(path)
    migrate(database)
    with database.connect() as connection:
        connection.execute(
            """INSERT INTO tracks (file_path, title, artist)
               VALUES ('song.mp3', 'Song', 'Artist')"""
        )
    return database


def test_track_policy_survives_repository_restart(tmp_path: Path) -> None:
    database = _database(tmp_path / "policy.db")
    repository = TrackPolicyRepository(database)

    repository.set(1, TrackPolicyStatus.BLOCKED, "Nicht für diese Veranstaltung")
    restored = TrackPolicyRepository(database).get(1)

    assert restored.status is TrackPolicyStatus.BLOCKED
    assert restored.reason == "Nicht für diese Veranstaltung"


def test_block_and_restriction_require_explicit_queue_override(tmp_path: Path) -> None:
    database = _database(tmp_path / "override.db")
    tracks = TrackRepository(database)
    track = tracks.get(1)
    assert track is not None
    entry = QueueEntry(7, track.id, 1, QueueStatus.WAITING)
    service = PersistentTrackBlockService(TrackPolicyRepository(database))
    service.set_policy(track.id, TrackPolicyStatus.RESTRICTED, "Nur auf Nachfrage")

    rejected = service.evaluate(entry, track)
    assert rejected is not None
    assert rejected.code == "RESTRICTED_TRACK"

    service.allow_queue_entry(entry.queue_id)
    assert service.evaluate(entry, track) is None
