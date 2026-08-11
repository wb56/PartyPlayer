"""Resolve equalizer inheritance without coupling it to a concrete audio deck."""

import logging

from party_player.equalizer import EqualizerPreset, QueueEqualizerContext
from party_player.models import Track
from party_player.repositories.equalizer_repository import (
    EqualizerAssignmentRepository,
    EqualizerPresetRepository,
)


class EqualizerResolver:
    """Resolve title > queue/playlist > genre > global > disabled."""

    def __init__(
        self,
        presets: EqualizerPresetRepository,
        assignments: EqualizerAssignmentRepository,
    ) -> None:
        self._presets = presets
        self._assignments = assignments
        self._logger = logging.getLogger(__name__)
        self._preset_by_id: dict[int, EqualizerPreset] = {}
        self._preset_by_key: dict[str, EqualizerPreset] = {}
        self._track_assignments: dict[int, int] = {}
        self._queue_assignments: dict[int, int] = {}
        self._genre_assignments: dict[str, int] = {}
        self.refresh()

    def refresh(self) -> None:
        """Refresh the immutable runtime lookup data after an assignment change."""
        presets = self._presets.list_enabled()
        self._preset_by_id = {
            preset.database_id: preset for preset in presets if preset.database_id is not None
        }
        self._preset_by_key = {preset.preset_id.casefold(): preset for preset in presets}
        (
            self._track_assignments,
            self._queue_assignments,
            self._genre_assignments,
        ) = self._assignments.snapshot()

    def list_presets(self) -> tuple[EqualizerPreset, ...]:
        return tuple(sorted(self._preset_by_key.values(), key=lambda item: item.name.casefold()))

    def preset_by_key(self, preset_key: str) -> EqualizerPreset | None:
        return self._preset_by_key.get(preset_key.casefold())

    def assign_track(self, track_id: int, preset_key: str | None) -> None:
        self._assignments.assign_track(track_id, self._database_id(preset_key))
        self.refresh()

    def assign_saved_queue(self, saved_queue_id: int, preset_key: str | None) -> None:
        self._assignments.assign_saved_queue(saved_queue_id, self._database_id(preset_key))
        self.refresh()

    def assign_genre(self, genre: str, preset_key: str | None) -> None:
        self._assignments.assign_genre(genre, self._database_id(preset_key))
        self.refresh()

    def track_assignment_key(self, track_id: int) -> str | None:
        return self._key_for_id(self._track_assignments.get(track_id))

    def saved_queue_assignment_key(self, saved_queue_id: int) -> str | None:
        return self._key_for_id(self._queue_assignments.get(saved_queue_id))

    def genre_assignment_key(self, genre: str) -> str | None:
        key = self._assignments.normalize_genre(genre)
        return self._key_for_id(self._genre_assignments.get(key))

    def save_custom(self, preset: EqualizerPreset) -> EqualizerPreset:
        saved = self._presets.save_custom(preset)
        self.refresh()
        return saved

    def delete_custom(self, preset_key: str) -> bool:
        preset = self.preset_by_key(preset_key)
        if preset is None or preset.database_id is None:
            return False
        deleted = self._presets.delete_custom(preset.database_id)
        self.refresh()
        return deleted

    def _database_id(self, preset_key: str | None) -> int | None:
        if preset_key is None or preset_key == "inherit":
            return None
        preset = self.preset_by_key(preset_key)
        if preset is None or preset.database_id is None:
            raise ValueError("Unbekanntes Equalizer-Preset")
        return preset.database_id

    def _key_for_id(self, preset_id: int | None) -> str | None:
        preset = self._preset_by_id.get(preset_id) if preset_id is not None else None
        return preset.preset_id if preset is not None else None

    def resolve(
        self,
        track: Track,
        queue: QueueEqualizerContext | None,
        global_preset_key: str | None,
    ) -> tuple[EqualizerPreset | None, str]:
        candidates: list[tuple[int | None, str]] = [
            (self._track_assignments.get(track.id), "TITLE"),
        ]
        if queue is not None:
            candidates.append((queue.transient_preset_id, "QUEUE"))
            if queue.saved_queue_id is not None:
                candidates.append(
                    (
                        self._queue_assignments.get(queue.saved_queue_id),
                        "PLAYLIST",
                    )
                )
        genre_key = self._assignments.normalize_genre(track.genre)
        candidates.append((self._genre_assignments.get(genre_key), "GENRE"))
        for preset_id, source in candidates:
            if preset_id is None:
                continue
            preset = self._preset_by_id.get(preset_id)
            if preset is not None:
                if preset.preset_id == "disabled":
                    return None, source
                return preset, source
            self._logger.warning(
                "Equalizer-Zuweisung %s verweist auf fehlendes Preset %s",
                source,
                preset_id,
            )
        if global_preset_key:
            preset = self._preset_by_key.get(global_preset_key.casefold())
            if preset is not None:
                if preset.preset_id == "disabled":
                    return None, "GLOBAL"
                return preset, "GLOBAL"
            self._logger.warning(
                "Globales Equalizer-Preset %r ist nicht verfügbar; Equalizer wird deaktiviert",
                global_preset_key,
            )
        return None, "DISABLED"
