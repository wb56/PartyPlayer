from party_player.database.connection import Database
from party_player.media_path_remap import (
    MediaPathRemapErrorCode,
    MediaPathRemapService,
)


def insert_paths(database: Database) -> None:
    with database.connect() as connection:
        connection.executemany(
            "INSERT INTO tracks (id, file_path, title) VALUES (?, ?, ?)",
            [
                (1, r"D:\Musik\Album\eins.mp3", "Eins"),
                (2, r"D:\Musik2\zwei.mp3", "Zwei"),
            ],
        )
        connection.execute(
            "INSERT INTO audio_overlays (id, name, file_path) VALUES (?, ?, ?)",
            (1, "Jingle", r"d:\musik\Jingles\start.wav"),
        )
        connection.execute(
            """INSERT INTO emergency_play_history
               (id, track_id, deck_id, media_type, title, file_path)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (1, 1, "A", "PRIMARY", "Eins", r"D:\Musik\Album\eins.mp3"),
        )


def test_preview_respects_case_insensitive_prefix_boundaries_and_commit_is_atomic(
    temporary_database: Database,
) -> None:
    insert_paths(temporary_database)
    service = MediaPathRemapService(temporary_database)

    preview = service.preview(r"d:\MUSIK", r"E:\Neue Musik")

    assert preview.valid and preview.can_commit
    assert preview.track_count == 1
    assert preview.overlay_count == 1
    assert preview.emergency_history_count == 1
    assert preview.affected_count == 3
    assert len(preview.examples) == 3
    assert all(change.new_path.startswith(r"E:\Neue Musik") for change in preview.examples)

    result = service.commit(preview)
    assert result.success and result.affected_count == 3
    with temporary_database.connect() as connection:
        tracks = connection.execute("SELECT id, file_path FROM tracks ORDER BY id").fetchall()
        overlay = connection.execute("SELECT file_path FROM audio_overlays").fetchone()
        history = connection.execute("SELECT file_path FROM emergency_play_history").fetchone()
    assert tracks[0]["file_path"] == r"E:\Neue Musik\Album\eins.mp3"
    assert tracks[1]["file_path"] == r"D:\Musik2\zwei.mp3"
    assert overlay["file_path"] == r"E:\Neue Musik\Jingles\start.wav"
    assert history["file_path"] == r"E:\Neue Musik\Album\eins.mp3"


def test_unc_mapping_preserves_suffix_and_rejects_sibling_share_prefix(
    temporary_database: Database,
) -> None:
    with temporary_database.connect() as connection:
        connection.executemany(
            "INSERT INTO tracks (id, file_path, title) VALUES (?, ?, ?)",
            [
                (1, r"\\server\share\Musik\eins.mp3", "Eins"),
                (2, r"\\server\share2\zwei.mp3", "Zwei"),
            ],
        )
    preview = MediaPathRemapService(temporary_database).preview(
        r"\\SERVER\SHARE", r"\\nas-neu\audio"
    )

    assert preview.can_commit and preview.track_count == 1
    assert preview.examples[0].new_path == r"\\nas-neu\audio\Musik\eins.mp3"


def test_track_collision_blocks_commit_without_changes(temporary_database: Database) -> None:
    with temporary_database.connect() as connection:
        connection.executemany(
            "INSERT INTO tracks (id, file_path, title) VALUES (?, ?, ?)",
            [
                (1, r"D:\Alt\eins.mp3", "Alt"),
                (2, r"E:\Neu\eins.mp3", "Neu"),
            ],
        )
    service = MediaPathRemapService(temporary_database)

    preview = service.preview(r"D:\Alt", r"E:\Neu")
    result = service.commit(preview)

    assert preview.error_code is MediaPathRemapErrorCode.COLLISION
    assert not preview.can_commit and preview.collisions == (r"E:\Neu\eins.mp3",)
    assert result.error_code is MediaPathRemapErrorCode.COLLISION
    with temporary_database.connect() as connection:
        paths = [row[0] for row in connection.execute("SELECT file_path FROM tracks ORDER BY id")]
    assert paths == [r"D:\Alt\eins.mp3", r"E:\Neu\eins.mp3"]


def test_changed_persistence_state_invalidates_preview(temporary_database: Database) -> None:
    insert_paths(temporary_database)
    service = MediaPathRemapService(temporary_database)
    preview = service.preview(r"D:\Musik", r"E:\Neu")
    with temporary_database.connect() as connection:
        connection.execute(
            "UPDATE audio_overlays SET file_path = ? WHERE id = 1",
            (r"D:\Musik\Jingles\changed.wav",),
        )

    result = service.commit(preview)

    assert result.error_code is MediaPathRemapErrorCode.STATE_CHANGED


def test_sql_failure_rolls_back_every_table_and_track_placeholder(
    temporary_database: Database,
) -> None:
    insert_paths(temporary_database)
    service = MediaPathRemapService(temporary_database)
    preview = service.preview(r"D:\Musik", r"E:\Neu")
    with temporary_database.connect() as connection:
        connection.execute(
            """CREATE TRIGGER reject_overlay_remap BEFORE UPDATE OF file_path ON audio_overlays
               BEGIN SELECT RAISE(ABORT, 'blocked'); END"""
        )

    result = service.commit(preview)

    assert result.error_code is MediaPathRemapErrorCode.COMMIT_FAILED
    with temporary_database.connect() as connection:
        track = connection.execute("SELECT file_path FROM tracks WHERE id = 1").fetchone()
        overlay = connection.execute("SELECT file_path FROM audio_overlays WHERE id = 1").fetchone()
    assert track["file_path"] == r"D:\Musik\Album\eins.mp3"
    assert overlay["file_path"] == r"d:\musik\Jingles\start.wav"


def test_overlapping_source_and_target_uses_collision_safe_track_staging(
    temporary_database: Database,
) -> None:
    with temporary_database.connect() as connection:
        connection.executemany(
            "INSERT INTO tracks (id, file_path, title) VALUES (?, ?, ?)",
            [
                (1, r"D:\Musik\eins.mp3", "Eins"),
                (2, r"D:\Musik\Verschoben\eins.mp3", "Zwei"),
            ],
        )
    service = MediaPathRemapService(temporary_database)
    preview = service.preview(r"D:\Musik", r"D:\Musik\Verschoben")

    result = service.commit(preview)

    assert result.success
    with temporary_database.connect() as connection:
        paths = [row[0] for row in connection.execute("SELECT file_path FROM tracks ORDER BY id")]
    assert paths == [
        r"D:\Musik\Verschoben\eins.mp3",
        r"D:\Musik\Verschoben\Verschoben\eins.mp3",
    ]
