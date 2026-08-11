import json
from pathlib import Path

from party_player.database.connection import Database
from party_player.equalizer import EqualizerPreset
from party_player.equalizer_transfer import (
    EqualizerConflictStrategy,
    EqualizerTransferErrorCode,
    EqualizerTransferService,
)
from party_player.repositories.equalizer_repository import EqualizerPresetRepository


def custom(key: str = "party", name: str = "Party") -> EqualizerPreset:
    return EqualizerPreset(key, name, -4.0, ((60.0, 2.0), (1000.0, -1.5)))


def test_json_export_and_copy_import_preserve_curve(
    temporary_database: Database, tmp_path: Path
) -> None:
    repository = EqualizerPresetRepository(temporary_database)
    saved = repository.save_custom(custom())
    service = EqualizerTransferService(repository)
    destination = tmp_path / "party-eq.json"

    exported = service.export(saved.preset_id, destination)
    preview = service.preview_import(destination)
    imported = service.import_preview(preview, EqualizerConflictStrategy.COPY)

    assert exported.success and preview.valid and imported.success
    assert preview.has_conflict and not preview.builtin_conflict
    assert imported.preset is not None
    assert imported.preset.preset_id == "party-copy-2"
    assert imported.preset.name == "Party (2)"
    assert imported.preset.preamp_db == -4.0
    assert imported.preset.curve == ((60.0, 2.0), (1000.0, -1.5))
    envelope = json.loads(destination.read_text(encoding="utf-8"))
    assert set(envelope) == {"type", "format_version", "created_at", "payload"}


def test_skip_and_replace_are_explicit_and_replace_bands_atomically(
    temporary_database: Database, tmp_path: Path
) -> None:
    repository = EqualizerPresetRepository(temporary_database)
    original = repository.save_custom(custom())
    service = EqualizerTransferService(repository)
    source = tmp_path / "party.json"
    assert service.export(original.preset_id, source).success

    skipped = service.import_preview(service.preview_import(source), EqualizerConflictStrategy.SKIP)
    assert skipped.success and skipped.skipped

    document = json.loads(source.read_text(encoding="utf-8"))
    document["payload"]["preamp_db"] = -6.0
    document["payload"]["bands"] = [{"frequency_hz": 120.0, "gain_db": 3.0}]
    source.write_text(json.dumps(document), encoding="utf-8")
    replaced = service.import_preview(
        service.preview_import(source), EqualizerConflictStrategy.REPLACE
    )

    assert replaced.success and replaced.preset is not None
    assert replaced.preset.database_id == original.database_id
    assert replaced.preset.preamp_db == -6.0
    assert replaced.preset.curve == ((120.0, 3.0),)


def test_builtin_can_be_skipped_or_copied_but_never_replaced(
    temporary_database: Database, tmp_path: Path
) -> None:
    repository = EqualizerPresetRepository(temporary_database)
    service = EqualizerTransferService(repository)
    source = tmp_path / "rock.json"
    assert service.export("rock", source).success
    preview = service.preview_import(source)

    replaced = service.import_preview(preview, EqualizerConflictStrategy.REPLACE)
    copied = service.import_preview(preview, EqualizerConflictStrategy.COPY)

    assert preview.builtin_conflict
    assert replaced.error_code is EqualizerTransferErrorCode.BUILTIN_CONFLICT
    assert copied.success and copied.preset is not None
    assert copied.preset.preset_id == "rock-copy-2"
    rock = repository.get_by_key("rock")
    assert rock is not None and rock.name == "Rock"


def test_replace_resolves_distinct_key_and_name_conflicts_in_one_transaction(
    temporary_database: Database, tmp_path: Path
) -> None:
    repository = EqualizerPresetRepository(temporary_database)
    by_key = repository.save_custom(custom("key-a", "Name A"))
    by_name = repository.save_custom(custom("key-b", "Name B"))
    service = EqualizerTransferService(repository)
    source = tmp_path / "combined.json"
    assert service.export(by_key.preset_id, source).success
    document = json.loads(source.read_text(encoding="utf-8"))
    document["payload"]["name"] = "Name B"
    source.write_text(json.dumps(document), encoding="utf-8")
    preview = service.preview_import(source)

    result = service.import_preview(preview, EqualizerConflictStrategy.REPLACE)

    assert len(preview.conflicts) == 2
    assert result.success and result.preset is not None
    assert result.preset.database_id == by_key.database_id
    assert repository.get_by_key("key-b") is None
    assert all(item.database_id != by_name.database_id for item in repository.list_enabled())


def test_invalid_band_values_are_rejected_before_database_write(
    temporary_database: Database, tmp_path: Path
) -> None:
    repository = EqualizerPresetRepository(temporary_database)
    service = EqualizerTransferService(repository)
    source = tmp_path / "invalid.json"
    source.write_text(
        json.dumps(
            {
                "type": "partyplayer-equalizer-preset",
                "format_version": 1,
                "created_at": "2026-08-10T12:00:00+00:00",
                "payload": {
                    "preset_key": "unsafe",
                    "name": "Unsicher",
                    "preamp_db": 0.0,
                    "bands": [{"frequency_hz": 60.0, "gain_db": 21.0}],
                },
            }
        ),
        encoding="utf-8",
    )

    preview = service.preview_import(source)

    assert not preview.valid
    assert preview.error_code is EqualizerTransferErrorCode.FORMAT_INVALID
    assert repository.get_by_key("unsafe") is None


def test_changed_file_or_conflict_state_invalidates_preview(
    temporary_database: Database, tmp_path: Path
) -> None:
    repository = EqualizerPresetRepository(temporary_database)
    saved = repository.save_custom(custom())
    service = EqualizerTransferService(repository)
    source = tmp_path / "party.json"
    assert service.export(saved.preset_id, source).success
    preview = service.preview_import(source)
    source.write_text(source.read_text(encoding="utf-8") + " ", encoding="utf-8")

    result = service.import_preview(preview, EqualizerConflictStrategy.SKIP)

    assert result.error_code is EqualizerTransferErrorCode.SOURCE_CHANGED
