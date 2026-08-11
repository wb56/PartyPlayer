"""Playback history lifecycle tests."""

from pathlib import Path

from party_player.database.connection import Database
from party_player.database.migrations import migrate
from party_player.enums import CompletionStatus, HistoryReasonCode, QueueSource
from party_player.history_reason import history_reason_text
from party_player.models import Track
from party_player.playback_history_service import PlaybackHistoryService
from party_player.repository import PartyPlayerRepository


def test_history_service_writes_once_per_active_playback(tmp_path: Path) -> None:
    database = Database(tmp_path / "test.db")
    migrate(database)
    with database.connect() as connection:
        connection.execute(
            "INSERT INTO tracks (file_path, title) VALUES (?, ?)",
            ("song.mp3", "Song"),
        )
    repository = PartyPlayerRepository(database)
    session = repository.create_session("Test")
    now = [100.0]
    history = PlaybackHistoryService(repository, session.session_id, clock=lambda: now[0])
    track = Track(1, "song.mp3", "Song", "", "", 60.0)

    history.start("A", track, queue_id=None)
    history.start("A", track, queue_id=None)
    now[0] += 60
    assert history.finish("A", CompletionStatus.COMPLETED, 60.0)
    assert not history.finish("A", CompletionStatus.COMPLETED, 60.0)

    with database.connect() as connection:
        rows = connection.execute(
            """SELECT deck_id, completion_status, play_duration
               FROM play_history WHERE session_id = ?""",
            (session.session_id,),
        ).fetchall()
    assert len(rows) == 1
    assert rows[0]["deck_id"] == "A"
    assert rows[0]["completion_status"] == "PLAYED"
    assert rows[0]["play_duration"] == 60.0


def test_deferred_history_request_is_idempotent(tmp_path: Path) -> None:
    database = Database(tmp_path / "test.db")
    migrate(database)
    with database.connect() as connection:
        connection.execute(
            "INSERT INTO tracks (file_path, title) VALUES (?, ?)",
            ("song.mp3", "Song"),
        )
    repository = PartyPlayerRepository(database)
    session = repository.create_session("Test")
    history = PlaybackHistoryService(repository, session.session_id)
    history.start("A", Track(1, "song.mp3", "Song", "", "", 60.0))

    request = history.prepare_finish(
        "A", CompletionStatus.COMPLETED, 60.0, transition_id="transition-1"
    )

    assert request is not None
    assert history.persist(request)
    assert not history.persist(request)
    with database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM play_history").fetchone()[0] == 1


def test_history_records_skip_reason_and_audio_error(tmp_path: Path) -> None:
    database = Database(tmp_path / "test.db")
    migrate(database)
    with database.connect() as connection:
        connection.execute(
            "INSERT INTO tracks (file_path, title) VALUES (?, ?)", ("song.mp3", "Song")
        )
    repository = PartyPlayerRepository(database)
    session = repository.create_session("Test")
    history = PlaybackHistoryService(repository, session.session_id)
    track = Track(1, "song.mp3", "Song", "", "", 60.0)

    history.start("A", track)
    history.finish(
        "A",
        CompletionStatus.SKIPPED,
        12.0,
        skip_code=HistoryReasonCode.OPERATOR_SKIP,
    )
    history.start("B", track)
    history.finish("B", CompletionStatus.ERROR, 3.0, error_message="Decoderfehler")

    with database.connect() as connection:
        rows = connection.execute(
            """SELECT completion_status, skip_reason, skip_code, error_message
               FROM play_history ORDER BY id"""
        ).fetchall()
    assert tuple(rows[0]) == ("SKIPPED", None, "OPERATOR_SKIP", None)
    assert tuple(rows[1]) == ("FAILED", None, None, "Decoderfehler")
    assert history_reason_text(rows[0]["skip_code"]) == "Vom Operator übersprungen"


def test_history_snapshots_playback_metrics_queue_source_and_overrides(tmp_path: Path) -> None:
    database = Database(tmp_path / "test.db")
    migrate(database)
    with database.connect() as connection:
        connection.execute(
            "INSERT INTO tracks (file_path, title, duration_seconds) VALUES (?, ?, ?)",
            ("song.mp3", "Song", 100.0),
        )
    repository = PartyPlayerRepository(database)
    session = repository.create_session("Test")
    entry = repository.add_queue_entry(
        session.session_id,
        1,
        QueueSource.GUEST_REQUEST,
        cue_in_override=5.0,
        cue_out_override=95.0,
        fade_duration_override=4.0,
        cue_override_source="queue",
    )
    now = [10.0]
    history = PlaybackHistoryService(repository, session.session_id, clock=lambda: now[0])

    history.start("A", Track(1, "song.mp3", "Song", "", "", 100.0), entry.queue_id)
    now[0] += 25.0
    history.finish(
        "A",
        CompletionStatus.SKIPPED,
        25.0,
        skip_code=HistoryReasonCode.OPERATOR_SKIP,
    )

    with database.connect() as connection:
        row = connection.execute(
            """SELECT effective_duration, playback_ratio, queue_source, result_code,
                      skip_code, cue_in_override, cue_out_override,
                      fade_duration_override, cue_override_source, override_applied
               FROM play_history"""
        ).fetchone()
    assert row is not None
    assert tuple(row) == (
        100.0,
        0.25,
        "GUEST_REQUEST",
        "SKIPPED",
        "OPERATOR_SKIP",
        5.0,
        95.0,
        4.0,
        "queue",
        1,
    )


def test_history_measures_real_play_time_independent_of_seek_and_pause(tmp_path: Path) -> None:
    database = Database(tmp_path / "test.db")
    migrate(database)
    with database.connect() as connection:
        connection.execute(
            "INSERT INTO tracks (file_path, title) VALUES (?, ?)", ("song.mp3", "Song")
        )
    repository = PartyPlayerRepository(database)
    session = repository.create_session("Test")
    now = [10.0]
    history = PlaybackHistoryService(repository, session.session_id, clock=lambda: now[0])
    track = Track(1, "song.mp3", "Song", "", "", 300.0)

    history.start(
        "A",
        track,
        effective_cue_in=50.0,
        effective_cue_out=250.0,
    )
    now[0] += 12
    history.pause("A")
    now[0] += 30
    history.resume("A")
    now[0] += 8
    history.finish("A", CompletionStatus.COMPLETED, play_duration=250.0)

    with database.connect() as connection:
        row = connection.execute(
            """SELECT play_duration, effective_duration, playback_ratio,
                      effective_cue_in, effective_cue_out
               FROM play_history"""
        ).fetchone()
    assert row is not None
    assert row["play_duration"] == 20.0
    assert row["effective_duration"] == 200.0
    assert row["playback_ratio"] == 0.1
    assert row["effective_cue_in"] == 50.0
    assert row["effective_cue_out"] == 250.0


def test_same_track_creates_complete_history_for_each_replay(tmp_path: Path) -> None:
    database = Database(tmp_path / "test.db")
    migrate(database)
    with database.connect() as connection:
        connection.execute(
            "INSERT INTO tracks (file_path, title) VALUES (?, ?)", ("song.mp3", "Song")
        )
    repository = PartyPlayerRepository(database)
    session = repository.create_session("Test")
    now = [0.0]
    history = PlaybackHistoryService(repository, session.session_id, clock=lambda: now[0])
    track = Track(1, "song.mp3", "Song", "", "", 60.0)

    history.start("A", track)
    now[0] = 20.0
    assert history.finish("A", CompletionStatus.COMPLETED, 60.0)
    history.start("A", track)
    now[0] = 35.0
    assert history.finish(
        "A",
        CompletionStatus.SKIPPED,
        15.0,
        skip_code=HistoryReasonCode.OPERATOR_SKIP,
    )

    with database.connect() as connection:
        rows = connection.execute(
            """SELECT track_id, deck_id, completion_status, play_duration, skip_reason
               FROM play_history ORDER BY id"""
        ).fetchall()
    assert [tuple(row) for row in rows] == [
        (1, "A", "PARTIALLY_PLAYED", 20.0, None),
        (1, "A", "SKIPPED", 15.0, None),
    ]


def test_same_track_on_both_decks_has_independent_history(tmp_path: Path) -> None:
    database = Database(tmp_path / "test.db")
    migrate(database)
    with database.connect() as connection:
        connection.execute(
            "INSERT INTO tracks (file_path, title) VALUES (?, ?)", ("song.mp3", "Song")
        )
    repository = PartyPlayerRepository(database)
    session = repository.create_session("Test")
    now = [100.0]
    history = PlaybackHistoryService(repository, session.session_id, clock=lambda: now[0])
    track = Track(1, "song.mp3", "Song", "", "", 60.0)

    history.start("A", track)
    now[0] = 105.0
    history.start("B", track)
    now[0] = 112.0
    assert history.finish("A", CompletionStatus.STOPPED, 0.0)
    now[0] = 115.0
    assert history.finish("B", CompletionStatus.COMPLETED, 60.0)

    with database.connect() as connection:
        rows = connection.execute(
            """SELECT deck_id, completion_status, play_duration
               FROM play_history ORDER BY deck_id"""
        ).fetchall()
    assert [tuple(row) for row in rows] == [
        ("A", "PARTIALLY_PLAYED", 12.0),
        ("B", "PARTIALLY_PLAYED", 10.0),
    ]


def test_played_threshold_uses_ratio_or_absolute_duration(tmp_path: Path) -> None:
    database = Database(tmp_path / "test.db")
    migrate(database)
    with database.connect() as connection:
        connection.execute("INSERT INTO tracks (file_path, title) VALUES ('song.mp3', 'Song')")
    repository = PartyPlayerRepository(database)
    session = repository.create_session("Test")
    now = [0.0]
    history = PlaybackHistoryService(
        repository,
        session.session_id,
        clock=lambda: now[0],
        played_ratio_threshold=0.5,
        played_seconds_threshold=120.0,
    )

    history.start("A", Track(1, "song.mp3", "Song", "", "", 100.0))
    now[0] = 49.0
    history.finish("A", CompletionStatus.PLAYED, 49.0)
    history.start("A", Track(1, "song.mp3", "Song", "", "", 100.0))
    now[0] = 99.0
    history.finish("A", CompletionStatus.ABORTED, 50.0)
    history.start("A", Track(1, "song.mp3", "Song", "", "", 500.0))
    now[0] = 219.0
    history.finish("A", CompletionStatus.ABORTED, 120.0)
    history.start("A", Track(1, "song.mp3", "Song", "", "", 100.0))
    history.finish("A", CompletionStatus.ABORTED, 0.0)

    with database.connect() as connection:
        statuses = [
            str(row["completion_status"])
            for row in connection.execute("SELECT completion_status FROM play_history ORDER BY id")
        ]
    assert statuses == ["PARTIALLY_PLAYED", "PLAYED", "PLAYED", "ABORTED"]
