import json
from pathlib import Path

from pytest import MonkeyPatch

from party_player.database.connection import Database
from party_player.models import SavedQueueEntry
from party_player.playlist_transfer import (
    PlaylistConflictStrategy,
    PlaylistTransferErrorCode,
    PlaylistTransferFormat,
    PlaylistTransferService,
)
from party_player.repositories.saved_queue_repository import SavedQueueRepository
from party_player.repositories.track_repository import TrackRepository


def build_service(
    database: Database,
) -> tuple[PlaylistTransferService, SavedQueueRepository]:
    with database.connect() as connection:
        connection.executemany(
            """INSERT INTO tracks
               (id, file_path, title, artist, duration_seconds)
               VALUES (?, ?, ?, ?, ?)""",
            [
                (1, r"C:\Musik\eins.mp3", "Eins", "Künstlerin", 181.8),
                (2, r"C:\Musik\zwei.mp3", "Zwei", "Künstler", 202.2),
            ],
        )
    playlists = SavedQueueRepository(database)
    return PlaylistTransferService(playlists, TrackRepository(database)), playlists


def test_json_roundtrip_preserves_order_paths_and_cue_snapshot(
    temporary_database: Database, tmp_path: Path
) -> None:
    service, playlists = build_service(temporary_database)
    source = playlists.save(
        "Tanzfläche",
        [
            SavedQueueEntry(2, 1, 1.5, 190.0, 5.0, "snapshot"),
            SavedQueueEntry(1, 2),
            SavedQueueEntry(2, 3),
        ],
    )
    destination = tmp_path / "playlist.json"

    exported = service.export(source.saved_queue_id, destination, PlaylistTransferFormat.JSON)
    imported = service.import_file(
        destination, PlaylistTransferFormat.JSON, PlaylistConflictStrategy.RENAME
    )

    assert exported.success and imported.success
    envelope = json.loads(destination.read_text(encoding="utf-8"))
    assert set(envelope) == {"type", "format_version", "created_at", "payload"}
    assert envelope["type"] == "partyplayer-playlist"
    assert envelope["format_version"] == 1
    assert imported.playlist is not None
    assert imported.playlist.name == "Tanzfläche (2)"
    assert imported.playlist.track_ids == (2, 1, 2)
    assert imported.playlist.entries[0].cue_in == 1.5
    assert imported.playlist.entries[0].cue_out == 190.0
    assert imported.playlist.entries[0].fade_duration == 5.0
    assert imported.playlist.entries[0].cue_source == "snapshot"


def test_m3u8_export_is_utf8_and_imports_catalog_paths(
    temporary_database: Database, tmp_path: Path
) -> None:
    service, playlists = build_service(temporary_database)
    source = playlists.save(
        "Morgen",
        [SavedQueueEntry(1, 1), SavedQueueEntry(2, 2)],
    )
    destination = tmp_path / "morgen.m3u8"

    assert service.export(source.saved_queue_id, destination, PlaylistTransferFormat.M3U8).success
    text = destination.read_text(encoding="utf-8")
    assert text.startswith("#EXTM3U\n#PLAYLIST:Morgen\n")
    assert "#EXTINF:181,Künstlerin - Eins" in text

    imported = service.import_file(
        destination, PlaylistTransferFormat.M3U8, PlaylistConflictStrategy.RENAME
    )
    assert imported.success
    assert imported.playlist is not None
    assert imported.playlist.track_ids == (1, 2)
    assert all(entry.cue_source == "inherited" for entry in imported.playlist.entries)


def test_m3u8_relative_path_is_resolved_from_playlist_directory(
    temporary_database: Database, tmp_path: Path
) -> None:
    relative_target = (tmp_path / "music" / "drei.mp3").resolve()
    with temporary_database.connect() as connection:
        connection.execute(
            "INSERT INTO tracks (id, file_path, title) VALUES (?, ?, ?)",
            (3, str(relative_target), "Drei"),
        )
    playlists = SavedQueueRepository(temporary_database)
    service = PlaylistTransferService(playlists, TrackRepository(temporary_database))
    source = tmp_path / "relative.m3u8"
    source.write_text("#EXTM3U\n#PLAYLIST:Relativ\nmusic/drei.mp3\n", encoding="utf-8")

    result = service.import_file(source, PlaylistTransferFormat.M3U8)

    assert result.success
    assert result.playlist is not None and result.playlist.track_ids == (3,)


def test_all_name_conflict_strategies_are_explicit_and_non_destructive(
    temporary_database: Database, tmp_path: Path
) -> None:
    service, playlists = build_service(temporary_database)
    original = playlists.save("Set", [SavedQueueEntry(1, 1)])
    source = tmp_path / "set.json"
    assert service.export(original.saved_queue_id, source, PlaylistTransferFormat.JSON).success

    error = service.import_file(source, PlaylistTransferFormat.JSON)
    skipped = service.import_file(
        source, PlaylistTransferFormat.JSON, PlaylistConflictStrategy.SKIP
    )
    renamed = service.import_file(
        source, PlaylistTransferFormat.JSON, PlaylistConflictStrategy.RENAME
    )
    appended = service.import_file(
        source, PlaylistTransferFormat.JSON, PlaylistConflictStrategy.APPEND
    )
    replaced = service.import_file(
        source, PlaylistTransferFormat.JSON, PlaylistConflictStrategy.REPLACE
    )

    assert error.error_code is PlaylistTransferErrorCode.NAME_CONFLICT
    assert skipped.success and skipped.skipped and skipped.playlist is None
    assert renamed.playlist is not None and renamed.playlist.name == "Set (2)"
    assert appended.playlist is not None and appended.playlist.track_ids == (1, 1)
    assert replaced.success and replaced.playlist is not None
    assert replaced.playlist.saved_queue_id == original.saved_queue_id


def test_invalid_version_and_missing_catalog_track_never_create_playlist(
    temporary_database: Database, tmp_path: Path
) -> None:
    service, playlists = build_service(temporary_database)
    source = playlists.save("Quelle", [SavedQueueEntry(1, 1)])
    exported = tmp_path / "source.json"
    assert service.export(source.saved_queue_id, exported, PlaylistTransferFormat.JSON).success
    document = json.loads(exported.read_text(encoding="utf-8"))

    document["format_version"] = 999
    unsupported = tmp_path / "unsupported.json"
    unsupported.write_text(json.dumps(document), encoding="utf-8")
    result = service.import_file(unsupported, PlaylistTransferFormat.JSON)
    assert result.error_code is PlaylistTransferErrorCode.VERSION_UNSUPPORTED

    document["format_version"] = 1
    document["payload"]["name"] = "Fehlender Titel"
    document["payload"]["entries"][0]["file_path"] = r"Z:\Nicht vorhanden.mp3"
    missing = tmp_path / "missing.json"
    missing.write_text(json.dumps(document), encoding="utf-8")
    result = service.import_file(missing, PlaylistTransferFormat.JSON)
    assert result.error_code is PlaylistTransferErrorCode.TRACK_NOT_FOUND
    assert {playlist.name for playlist in playlists.list_all()} == {"Quelle"}


def test_invalid_cue_snapshot_is_rejected_before_playlist_write(
    temporary_database: Database, tmp_path: Path
) -> None:
    service, playlists = build_service(temporary_database)
    document = {
        "type": "partyplayer-playlist",
        "format_version": 1,
        "created_at": "2026-08-10T12:00:00+00:00",
        "payload": {
            "name": "Ungültige Cues",
            "entries": [
                {
                    "file_path": r"C:\Musik\eins.mp3",
                    "title": "Eins",
                    "artist": "Künstlerin",
                    "cue_in": 100.0,
                    "cue_out": 50.0,
                    "fade_duration": 5.0,
                    "cue_source": "snapshot",
                }
            ],
        },
    }
    source = tmp_path / "invalid-cues.json"
    source.write_text(json.dumps(document), encoding="utf-8")

    result = service.import_file(source, PlaylistTransferFormat.JSON)

    assert result.error_code is PlaylistTransferErrorCode.FORMAT_INVALID
    assert playlists.list_all() == []


def test_preview_reports_duplicates_unknown_paths_and_conflict_without_writing(
    temporary_database: Database, tmp_path: Path
) -> None:
    service, playlists = build_service(temporary_database)
    existing = playlists.save("Vorschau", [SavedQueueEntry(1, 1)])
    source = tmp_path / "preview.json"
    assert service.export(existing.saved_queue_id, source, PlaylistTransferFormat.JSON).success
    document = json.loads(source.read_text(encoding="utf-8"))
    entries = document["payload"]["entries"]
    entries.append(dict(entries[0]))
    missing = dict(entries[0])
    missing["file_path"] = r"Z:\Fehlt.mp3"
    entries.append(missing)
    source.write_text(json.dumps(document), encoding="utf-8")

    preview = service.preview_import(source, PlaylistTransferFormat.JSON)

    assert preview.valid
    assert not preview.can_import
    assert preview.entry_count == 3
    assert preview.duplicate_count == 1
    assert preview.unknown_path_count == 1
    assert preview.unknown_path_examples == (r"Z:\Fehlt.mp3",)
    assert preview.name_conflict
    assert preview.existing_playlist_id == existing.saved_queue_id
    assert [item.name for item in playlists.list_all()] == ["Vorschau"]


def test_changed_source_is_rejected_after_successful_preview(
    temporary_database: Database, tmp_path: Path
) -> None:
    service, playlists = build_service(temporary_database)
    saved = playlists.save("Quelle", [SavedQueueEntry(1, 1)])
    source = tmp_path / "source.json"
    assert service.export(saved.saved_queue_id, source, PlaylistTransferFormat.JSON).success
    preview = service.preview_import(source, PlaylistTransferFormat.JSON)
    assert preview.valid and preview.can_import
    source.write_text(source.read_text(encoding="utf-8") + " ", encoding="utf-8")

    result = service.import_preview(preview, PlaylistConflictStrategy.RENAME)

    assert result.error_code is PlaylistTransferErrorCode.SOURCE_CHANGED
    assert [item.name for item in playlists.list_all()] == ["Quelle"]


def test_export_publish_failure_leaves_no_destination_or_temp_file(
    temporary_database: Database, tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    service, playlists = build_service(temporary_database)
    source = playlists.save("Set", [SavedQueueEntry(1, 1)])
    destination = tmp_path / "set.json"
    monkeypatch.setattr(
        "party_player.playlist_transfer.os.replace",
        lambda _source, _target: (_ for _ in ()).throw(PermissionError("denied")),
    )

    result = service.export(source.saved_queue_id, destination, PlaylistTransferFormat.JSON)

    assert result.error_code is PlaylistTransferErrorCode.IO_FAILED
    assert not destination.exists()
    assert not list(tmp_path.glob(".*.tmp"))
