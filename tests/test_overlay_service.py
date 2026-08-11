from pathlib import Path
from dataclasses import replace

import pytest

from party_player.database.connection import Database
from party_player.database.migrations import migrate
from party_player.overlay import OverlayDefinition, OverlayRecord
from party_player.overlay_service import OverlayService
from party_player.repositories.overlay_repository import OverlayRepository


def service(tmp_path: Path) -> OverlayService:
    database = Database(tmp_path / "service.db")
    migrate(database)
    return OverlayService(OverlayRepository(database))


def record(
    name: str,
    path: Path,
    *,
    category: str = "",
    favorite: int | None = None,
    enabled: bool = True,
) -> OverlayRecord:
    return OverlayRecord(
        OverlayDefinition(0, name, str(path), category=category),
        enabled=enabled,
        favorite_position=favorite,
        keyboard_shortcut=f"Ctrl+{favorite}" if favorite is not None else None,
    )


def test_snapshot_contains_sorted_categories_fixed_favorites_and_missing_files(
    tmp_path: Path,
) -> None:
    overlays = service(tmp_path)
    existing = tmp_path / "applaus.mp3"
    existing.write_bytes(b"ID3")
    applause = overlays.save(record("Applaus", existing, category="Effekte", favorite=2))
    greeting = overlays.save(
        record("Begrüßung", tmp_path / "missing.flac", category="Ansagen", favorite=1)
    )
    disabled = overlays.save(record("Aus", tmp_path / "disabled.mp3", enabled=False))

    snapshot = overlays.snapshot()

    assert snapshot.categories == ("Ansagen", "Effekte")
    assert snapshot.favorites[0] == greeting
    assert snapshot.favorites[1] == applause
    assert snapshot.favorites[2:] == (None, None, None, None)
    assert snapshot.missing_file_ids == frozenset(
        {greeting.definition.overlay_id, disabled.definition.overlay_id}
    )
    assert [item.definition.name for item in snapshot.records_for_category("effekte")] == [
        "Applaus"
    ]


def test_disabled_entries_are_available_to_management_snapshot(tmp_path: Path) -> None:
    overlays = service(tmp_path)
    disabled = overlays.save(record("Aus", tmp_path / "disabled.mp3", enabled=False, favorite=3))

    assert overlays.snapshot().records == ()
    assert overlays.snapshot().favorites[2] == disabled
    assert disabled.definition.overlay_id in overlays.snapshot().missing_file_ids
    assert overlays.snapshot(enabled_only=False).records == (disabled,)


def test_service_can_deactivate_an_existing_overlay(tmp_path: Path) -> None:
    overlays = service(tmp_path)
    saved = overlays.save(record("Tusch", tmp_path / "tusch.mp3"))

    disabled = overlays.set_enabled(saved.definition.overlay_id, False)

    assert not disabled.enabled
    assert overlays.snapshot().records == ()
    assert overlays.snapshot(enabled_only=False).records == (disabled,)


def test_favorite_move_and_delete_leave_no_stale_pad_reference(tmp_path: Path) -> None:
    overlays = service(tmp_path)
    saved = overlays.save(record("Tusch", tmp_path / "tusch.mp3", favorite=1))
    assert overlays.snapshot().favorites[0] == saved

    moved = overlays.save(
        replace(
            saved,
            favorite_position=4,
            keyboard_shortcut="Ctrl+4",
        )
    )
    moved_snapshot = overlays.snapshot()
    assert moved_snapshot.favorites[0] is None
    assert moved_snapshot.favorites[3] == moved

    assert overlays.delete(moved.definition.overlay_id)
    deleted_snapshot = overlays.snapshot(enabled_only=False)
    assert deleted_snapshot.records == ()
    assert deleted_snapshot.favorites == (None, None, None, None, None, None)


@pytest.mark.parametrize(
    ("definition", "message"),
    [
        (OverlayDefinition(0, "", "x.mp3"), "Name"),
        (OverlayDefinition(0, "X", "x.wav"), "MP3"),
        (OverlayDefinition(0, "X", "x.mp3", volume_percent=101), "Lautstärke"),
        (OverlayDefinition(0, "X", "x.mp3", fade_in_ms=-1), "Fade-in"),
        (OverlayDefinition(0, "X", "x.mp3", cue_in_ms=10, cue_out_ms=5), "Cue-Out"),
        (OverlayDefinition(0, "X", "x.mp3", ducking_db=-61), "Ducking"),
    ],
)
def test_validation_reports_field_specific_errors(
    definition: OverlayDefinition,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        OverlayService.validate(OverlayRecord(definition))


def test_shortcut_must_match_favorite_and_active_entry_cannot_be_deleted(
    tmp_path: Path,
) -> None:
    overlays = service(tmp_path)
    with pytest.raises(ValueError, match="Favoritenposition"):
        overlays.save(
            OverlayRecord(
                OverlayDefinition(0, "Tusch", "tusch.mp3"),
                favorite_position=1,
                keyboard_shortcut="Ctrl+2",
            )
        )
    saved = overlays.save(record("Tusch", tmp_path / "tusch.mp3"))
    with pytest.raises(ValueError, match="laufendes"):
        overlays.delete(
            saved.definition.overlay_id,
            active_overlay_id=saved.definition.overlay_id,
        )
    assert overlays.delete(saved.definition.overlay_id)


def test_full_volume_without_ducking_has_non_blocking_level_warning() -> None:
    risky = OverlayRecord(
        OverlayDefinition(
            1,
            "Tusch",
            "tusch.mp3",
            volume_percent=100,
            ducking_enabled=False,
        )
    )
    ducked = OverlayRecord(
        OverlayDefinition(
            2,
            "Applaus",
            "applaus.mp3",
            volume_percent=100,
            ducking_enabled=True,
        )
    )
    quieter = OverlayRecord(
        OverlayDefinition(
            3,
            "Intro",
            "intro.mp3",
            volume_percent=75,
            ducking_enabled=False,
        )
    )

    assert "übersteuern" in OverlayService.safety_warning(risky)
    assert OverlayService.safety_warning(ducked) == ""
    assert OverlayService.safety_warning(quieter) == ""
