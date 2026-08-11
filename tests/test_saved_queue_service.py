"""Named queue persistence and reload tests."""

from pathlib import Path

import pytest
from pytest import MonkeyPatch

from party_player.database.connection import Database
from party_player.database.migrations import migrate
from party_player.cue_points import CuePointRepository, CuePointService
from party_player.enums import QueueStatus
from party_player.queue_service import QueueService
from party_player.repositories.saved_queue_repository import SavedQueueRepository
from party_player.repositories.track_repository import TrackRepository
from party_player.repository import PartyPlayerRepository
from party_player.saved_queue_service import SavedQueueService
from party_player.models import SavedQueueEntry


def build_services(
    tmp_path: Path,
) -> tuple[Database, PartyPlayerRepository, TrackRepository, SavedQueueRepository]:
    database = Database(tmp_path / "test.db")
    migrate(database)
    with database.connect() as connection:
        connection.executemany(
            "INSERT INTO tracks (file_path, title) VALUES (?, ?)",
            [("one.mp3", "One"), ("two.mp3", "Two"), ("three.mp3", "Three")],
        )
    return (
        database,
        PartyPlayerRepository(database),
        TrackRepository(database),
        SavedQueueRepository(database),
    )


def test_named_queue_saves_planned_order_and_loads_as_waiting(tmp_path: Path) -> None:
    _database, party_repository, tracks, saved_repository = build_services(tmp_path)
    first_session = party_repository.create_session("First")
    first_queue = QueueService(party_repository, tracks, first_session.session_id)
    first = first_queue.add(2)
    first_queue.add(1)
    first_queue.mark_loaded(first.queue_id, "A")
    saved_service = SavedQueueService(saved_repository, first_queue)

    saved = saved_service.save_current("Abend")

    second_session = party_repository.create_session("Second")
    second_queue = QueueService(party_repository, tracks, second_session.session_id)
    loader = SavedQueueService(saved_repository, second_queue)
    assert loader.load(saved.saved_queue_id, replace_waiting=False) == (2, 0)
    assert [entry.track_id for entry in second_queue.entries()] == [2, 1]
    assert all(entry.status == QueueStatus.WAITING for entry in second_queue.entries())


def test_loading_named_queue_can_replace_only_waiting_entries(tmp_path: Path) -> None:
    _database, party_repository, tracks, saved_repository = build_services(tmp_path)
    source_session = party_repository.create_session("Source")
    source_queue = QueueService(party_repository, tracks, source_session.session_id)
    source_queue.add(1)
    saved = SavedQueueService(saved_repository, source_queue).save_current("Vorlage")

    target_session = party_repository.create_session("Target")
    target_queue = QueueService(party_repository, tracks, target_session.session_id)
    loaded = target_queue.add(2)
    target_queue.mark_loaded(loaded.queue_id, "B")
    target_queue.add(3)

    SavedQueueService(saved_repository, target_queue).load(
        saved.saved_queue_id, replace_waiting=True
    )

    entries = target_queue.entries()
    assert [entry.track_id for entry in entries] == [2, 1]
    assert entries[0].status == QueueStatus.LOADED


def test_loading_playlist_can_shuffle_and_respects_duplicate_policy(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    _database, party_repository, tracks, saved_repository = build_services(tmp_path)
    source_session = party_repository.create_session("Source")
    source_queue = QueueService(party_repository, tracks, source_session.session_id)
    source_queue.add(1)
    source_queue.add(2)
    source_queue.add(3)
    saved = SavedQueueService(saved_repository, source_queue).save_current("Tanzabend")

    target_session = party_repository.create_session("Target")
    target_queue = QueueService(
        party_repository, tracks, target_session.session_id, allow_duplicates=False
    )
    target_queue.add(2)
    monkeypatch.setattr("party_player.saved_queue_service.shuffle", lambda values: values.reverse())

    result = SavedQueueService(saved_repository, target_queue).load(
        saved.saved_queue_id, replace_waiting=False, shuffle_tracks=True
    )

    assert result == (2, 1)
    assert [entry.track_id for entry in target_queue.entries()] == [2, 3, 1]


def test_saved_queue_preserves_cue_snapshot_when_loaded_later(tmp_path: Path) -> None:
    _database, party_repository, tracks, saved_repository = build_services(tmp_path)
    source_session = party_repository.create_session("Source")
    source_queue = QueueService(party_repository, tracks, source_session.session_id)
    entry = source_queue.add(1)
    source_queue.set_cue_overrides(entry.queue_id, 1.8, 110.0, 7.0)

    saved = SavedQueueService(saved_repository, source_queue).save_current("Party")
    restored_saved = saved_repository.get(saved.saved_queue_id)
    assert restored_saved is not None
    assert restored_saved.entries[0].cue_source == "snapshot"

    target_session = party_repository.create_session("Target")
    target_queue = QueueService(party_repository, tracks, target_session.session_id)
    SavedQueueService(saved_repository, target_queue).load(
        saved.saved_queue_id, replace_waiting=False
    )

    loaded = target_queue.entries()[0]
    assert (loaded.cue_in_override, loaded.cue_out_override) == (1.8, 110.0)
    assert loaded.fade_duration_override == 7.0
    assert loaded.cue_override_source == "snapshot"


def test_two_saved_queues_keep_distinct_snapshots_for_same_track(tmp_path: Path) -> None:
    _database, party_repository, tracks, saved_repository = build_services(tmp_path)
    session = party_repository.create_session("Source")
    queue = QueueService(party_repository, tracks, session.session_id)
    entry = queue.add(1)
    service = SavedQueueService(saved_repository, queue)
    queue.set_cue_overrides(entry.queue_id, 1.0, 100.0, 5.0)
    first = service.save_current("Kurz")
    queue.set_cue_overrides(entry.queue_id, 4.0, 115.0, 9.0)
    second = service.save_current("Lang")

    first_restored = saved_repository.get(first.saved_queue_id)
    second_restored = saved_repository.get(second.saved_queue_id)

    assert first_restored is not None and second_restored is not None
    assert first_restored.entries[0].cue_in == 1.0
    assert first_restored.entries[0].fade_duration == 5.0
    assert second_restored.entries[0].cue_in == 4.0
    assert second_restored.entries[0].fade_duration == 9.0


def test_save_choice_freezes_effective_title_cues_or_keeps_inheritance(tmp_path: Path) -> None:
    database, party_repository, tracks, saved_repository = build_services(tmp_path)
    session = party_repository.create_session("Source")
    queue = QueueService(party_repository, tracks, session.session_id)
    queue.add(1)
    cue_points = CuePointService(CuePointRepository(database), 7.0)
    track = tracks.get(1)
    assert track is not None
    with database.connect() as connection:
        connection.execute("UPDATE tracks SET duration_seconds = 120 WHERE id = 1")
    track = tracks.get(1)
    assert track is not None
    cue_points.save_manual(track, 2.0, 110.0, 6.0)
    service = SavedQueueService(saved_repository, queue, cue_points)

    frozen = service.save_current("Eingefroren", snapshot_cues=True)
    inherited = service.save_current("Dynamisch", snapshot_cues=False)

    assert frozen.entries[0].cue_source == "snapshot"
    assert (frozen.entries[0].cue_in, frozen.entries[0].cue_out) == (2.0, 110.0)
    assert frozen.entries[0].fade_duration == 6.0
    assert inherited.entries[0].cue_source == "inherited"
    assert inherited.entries[0].cue_in is None
    assert inherited.entries[0].cue_out is None


def test_load_choice_can_ignore_saved_snapshot_and_use_current_title_values(
    tmp_path: Path,
) -> None:
    _database, party_repository, tracks, saved_repository = build_services(tmp_path)
    source_session = party_repository.create_session("Source")
    source_queue = QueueService(party_repository, tracks, source_session.session_id)
    source = source_queue.add(1)
    source_queue.set_cue_overrides(source.queue_id, 2.0, 100.0, 6.0)
    saved = SavedQueueService(saved_repository, source_queue).save_current("Party")

    snapshot_session = party_repository.create_session("Snapshot")
    snapshot_queue = QueueService(party_repository, tracks, snapshot_session.session_id)
    SavedQueueService(saved_repository, snapshot_queue).load(
        saved.saved_queue_id,
        replace_waiting=False,
        use_saved_cues=True,
    )
    current_session = party_repository.create_session("Current")
    current_queue = QueueService(party_repository, tracks, current_session.session_id)
    SavedQueueService(saved_repository, current_queue).load(
        saved.saved_queue_id,
        replace_waiting=False,
        use_saved_cues=False,
    )

    snapshot_entry = snapshot_queue.entries()[0]
    current_entry = current_queue.entries()[0]
    assert snapshot_entry.cue_in_override == 2.0
    assert snapshot_entry.cue_override_source == "snapshot"
    assert current_entry.cue_in_override is None
    assert current_entry.cue_out_override is None
    assert current_entry.fade_duration_override is None
    assert current_entry.cue_override_source == "inherited"


def test_saved_snapshot_survives_restart_and_later_catalog_cue_changes(
    tmp_path: Path,
) -> None:
    database, party_repository, tracks, saved_repository = build_services(tmp_path)
    with database.connect() as connection:
        connection.execute("UPDATE tracks SET duration_seconds = 120 WHERE id = 1")
    cue_points = CuePointService(CuePointRepository(database), 7.0)
    track = tracks.get(1)
    assert track is not None
    cue_points.save_manual(track, 2.0, 110.0, 6.0)
    source_session = party_repository.create_session("Source")
    source_queue = QueueService(party_repository, tracks, source_session.session_id)
    source_queue.add(1)
    saved = SavedQueueService(saved_repository, source_queue, cue_points).save_current(
        "Neustart", snapshot_cues=True
    )

    cue_points.save_manual(track, 8.0, 105.0, 9.0)
    restarted_repository = SavedQueueRepository(Database(tmp_path / "test.db"))
    target_session = party_repository.create_session("Target")
    target_queue = QueueService(party_repository, tracks, target_session.session_id)
    SavedQueueService(restarted_repository, target_queue, cue_points).load(
        saved.saved_queue_id,
        replace_waiting=False,
        use_saved_cues=True,
    )

    loaded = target_queue.entries()[0]
    resolved = cue_points.resolve(track, queue_entry=loaded)
    assert (resolved.cue_in, resolved.cue_out, resolved.fade_duration) == (2.0, 110.0, 6.0)
    assert resolved.cue_in_source == "QUEUE_SNAPSHOT"


def test_shuffling_saved_queue_keeps_each_snapshot_with_its_track(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    _database, party_repository, tracks, saved_repository = build_services(tmp_path)
    source_session = party_repository.create_session("Source")
    source_queue = QueueService(party_repository, tracks, source_session.session_id)
    first = source_queue.add(1)
    second = source_queue.add(2)
    source_queue.set_cue_overrides(first.queue_id, 1.0, 100.0, 5.0)
    source_queue.set_cue_overrides(second.queue_id, 2.0, 110.0, 8.0)
    saved = SavedQueueService(saved_repository, source_queue).save_current("Party")
    monkeypatch.setattr("party_player.saved_queue_service.shuffle", lambda values: values.reverse())
    target_session = party_repository.create_session("Target")
    target_queue = QueueService(party_repository, tracks, target_session.session_id)

    SavedQueueService(saved_repository, target_queue).load(
        saved.saved_queue_id, replace_waiting=False, shuffle_tracks=True
    )

    loaded = target_queue.entries()
    assert [entry.track_id for entry in loaded] == [2, 1]
    assert (loaded[0].cue_in_override, loaded[0].fade_duration_override) == (2.0, 8.0)
    assert (loaded[1].cue_in_override, loaded[1].fade_duration_override) == (1.0, 5.0)


def test_save_rejects_snapshot_outside_known_track_duration(tmp_path: Path) -> None:
    database, party_repository, tracks, saved_repository = build_services(tmp_path)
    with database.connect() as connection:
        connection.execute("UPDATE tracks SET duration_seconds = 120 WHERE id = 1")
    session = party_repository.create_session("Source")
    queue = QueueService(party_repository, tracks, session.session_id)
    entry = queue.add(1)
    queue.set_cue_overrides(entry.queue_id, 2.0, 121.0, 6.0)

    service = SavedQueueService(saved_repository, queue)

    with pytest.raises(ValueError, match="außerhalb der Titeldauer"):
        service.save_current("Ungültig")
    assert saved_repository.list_all() == []


def test_load_validates_all_snapshots_before_replacing_waiting_queue(tmp_path: Path) -> None:
    database, party_repository, tracks, saved_repository = build_services(tmp_path)
    with database.connect() as connection:
        connection.execute("UPDATE tracks SET duration_seconds = 120 WHERE id IN (1, 2)")
    saved = saved_repository.save(
        "Defekt",
        [
            SavedQueueEntry(1, 1, 2.0, 110.0, 6.0, "snapshot"),
            SavedQueueEntry(2, 2, 80.0, 70.0, 6.0, "snapshot"),
        ],
    )
    session = party_repository.create_session("Target")
    queue = QueueService(party_repository, tracks, session.session_id)
    original = queue.add(3)

    with pytest.raises(ValueError, match="muss nach Cue In liegen"):
        SavedQueueService(saved_repository, queue).load(
            saved.saved_queue_id,
            replace_waiting=True,
        )

    assert [entry.queue_id for entry in queue.entries()] == [original.queue_id]


def test_load_without_saved_cues_ignores_invalid_legacy_snapshot(tmp_path: Path) -> None:
    _database, party_repository, tracks, saved_repository = build_services(tmp_path)
    saved = saved_repository.save(
        "Altbestand",
        [SavedQueueEntry(1, 1, 20.0, 10.0, 15.0, "snapshot")],
    )
    session = party_repository.create_session("Target")
    queue = QueueService(party_repository, tracks, session.session_id)

    assert SavedQueueService(saved_repository, queue).load(
        saved.saved_queue_id,
        replace_waiting=False,
        use_saved_cues=False,
    ) == (1, 0)
    loaded = queue.entries()[0]
    assert loaded.cue_in_override is None
    assert loaded.cue_out_override is None
