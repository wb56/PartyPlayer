import json
from pathlib import Path

from party_player.overlay import OverlayDefinition, OverlayRecord
from party_player.overlay_transfer import (
    OverlayConflictStrategy,
    OverlayTransferErrorCode,
    OverlayTransferService,
)
from party_player.repositories.overlay_repository import OverlayRepository


def overlay(
    name: str,
    *,
    favorite: int | None = None,
    path: str | None = None,
) -> OverlayRecord:
    return OverlayRecord(
        OverlayDefinition(0, name, path or f"C:/Jingles/{name}.mp3", "Jingles"),
        favorite_position=favorite,
        keyboard_shortcut=f"Ctrl+{favorite}" if favorite is not None else None,
    )


def test_export_and_import_keep_audio_as_reference(temporary_database, tmp_path: Path) -> None:
    repository = OverlayRepository(temporary_database)
    repository.save(overlay("Tusch", favorite=1))
    service = OverlayTransferService(repository)
    destination = tmp_path / "overlays.json"

    exported = service.export(destination)
    document = json.loads(destination.read_text(encoding="utf-8"))

    assert exported.success
    assert document["type"] == "partyplayer-overlay-catalog"
    assert document["format_version"] == 1
    assert document["payload"]["overlays"][0]["file_path"] == "C:/Jingles/Tusch.mp3"


def test_keep_existing_skips_name_and_preserves_favorite(
    temporary_database, tmp_path: Path
) -> None:
    repository = OverlayRepository(temporary_database)
    original = repository.save(overlay("Vorhanden", favorite=1))
    service = OverlayTransferService(repository)
    source = tmp_path / "overlays.json"
    service.export(source)
    document = json.loads(source.read_text(encoding="utf-8"))
    document["payload"]["overlays"] = [
        {**document["payload"]["overlays"][0], "name": "Vorhanden", "file_path": "C:/Neu.mp3"},
        {
            **document["payload"]["overlays"][0],
            "name": "Neu",
            "file_path": "C:/Neu2.mp3",
            "favorite_position": None,
            "keyboard_shortcut": None,
        },
    ]
    source.write_text(json.dumps(document), encoding="utf-8")

    result = service.import_preview(
        service.preview_import(source), OverlayConflictStrategy.KEEP_EXISTING
    )

    records = repository.list_all()
    assert result.success and result.imported_count == 1
    assert (
        repository.get(original.definition.overlay_id).definition.file_path
        == original.definition.file_path
    )
    assert {item.definition.name for item in records} == {"Vorhanden", "Neu"}


def test_replace_existing_replaces_name_and_favorite_atomically(
    temporary_database, tmp_path: Path
) -> None:
    repository = OverlayRepository(temporary_database)
    repository.save(overlay("Alt", favorite=1))
    service = OverlayTransferService(repository)
    source = tmp_path / "overlays.json"
    service.export(source)
    document = json.loads(source.read_text(encoding="utf-8"))
    item = document["payload"]["overlays"][0]
    item.update({"name": "Neu", "file_path": "C:/Neu.mp3"})
    source.write_text(json.dumps(document), encoding="utf-8")

    preview = service.preview_import(source)
    result = service.import_preview(preview, OverlayConflictStrategy.REPLACE_EXISTING)

    records = repository.list_all()
    assert preview.conflicts and result.success
    assert [(item.definition.name, item.favorite_position) for item in records] == [("Neu", 1)]


def test_invalid_internal_favorite_conflict_is_rejected(temporary_database, tmp_path: Path) -> None:
    repository = OverlayRepository(temporary_database)
    repository.save(overlay("Eins", favorite=1))
    service = OverlayTransferService(repository)
    source = tmp_path / "overlays.json"
    service.export(source)
    document = json.loads(source.read_text(encoding="utf-8"))
    duplicate = {**document["payload"]["overlays"][0], "name": "Zwei"}
    document["payload"]["overlays"].append(duplicate)
    source.write_text(json.dumps(document), encoding="utf-8")

    preview = service.preview_import(source)

    assert not preview.valid
    assert preview.error_code is OverlayTransferErrorCode.FORMAT_INVALID


def test_changed_source_or_database_invalidates_preview(temporary_database, tmp_path: Path) -> None:
    repository = OverlayRepository(temporary_database)
    repository.save(overlay("Eins"))
    service = OverlayTransferService(repository)
    source = tmp_path / "overlays.json"
    service.export(source)
    preview = service.preview_import(source)
    current = repository.list_all()[0]
    repository.save(
        OverlayRecord(
            current.definition,
            favorite_position=2,
            keyboard_shortcut="Ctrl+2",
        )
    )

    result = service.import_preview(preview, OverlayConflictStrategy.KEEP_EXISTING)

    assert not result.success
    assert result.error_code is OverlayTransferErrorCode.STATE_CHANGED


def test_unknown_version_is_rejected_without_write(temporary_database, tmp_path: Path) -> None:
    repository = OverlayRepository(temporary_database)
    repository.save(overlay("Eins"))
    service = OverlayTransferService(repository)
    source = tmp_path / "overlays.json"
    service.export(source)
    document = json.loads(source.read_text(encoding="utf-8"))
    document["format_version"] = 99
    source.write_text(json.dumps(document), encoding="utf-8")

    preview = service.preview_import(source)

    assert preview.error_code is OverlayTransferErrorCode.VERSION_UNSUPPORTED
    assert len(repository.list_all()) == 1
