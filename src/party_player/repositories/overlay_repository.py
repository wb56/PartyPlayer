"""Persistence for overlay definitions and their independent history."""

from __future__ import annotations

import sqlite3
from datetime import datetime

from party_player.database.connection import Database
from party_player.overlay import (
    OverlayDefinition,
    OverlayPlayResult,
    OverlayRecord,
)


class OverlayRepository:
    def __init__(self, database: Database) -> None:
        self._database = database

    def list_all(self, *, enabled_only: bool = False) -> list[OverlayRecord]:
        where = "WHERE enabled = 1" if enabled_only else ""
        with self._database.connect() as connection:
            rows = connection.execute(
                f"""SELECT * FROM audio_overlays {where}
                    ORDER BY category COLLATE NOCASE, name COLLATE NOCASE, id"""
            ).fetchall()
        return [self._from_row(row) for row in rows]

    def search(self, query: str, *, enabled_only: bool = False) -> list[OverlayRecord]:
        normalized = f"%{query.strip()}%"
        enabled_clause = "AND enabled = 1" if enabled_only else ""
        with self._database.connect() as connection:
            rows = connection.execute(
                f"""SELECT * FROM audio_overlays
                    WHERE (name LIKE ? ESCAPE '\\' COLLATE NOCASE
                       OR category LIKE ? ESCAPE '\\' COLLATE NOCASE)
                    {enabled_clause}
                    ORDER BY category COLLATE NOCASE, name COLLATE NOCASE, id""",
                (normalized, normalized),
            ).fetchall()
        return [self._from_row(row) for row in rows]

    def get(self, overlay_id: int) -> OverlayRecord | None:
        with self._database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM audio_overlays WHERE id = ?",
                (overlay_id,),
            ).fetchone()
        return self._from_row(row) if row is not None else None

    def save(self, record: OverlayRecord) -> OverlayRecord:
        definition = self._normalized_definition(record.definition)
        shortcut = record.keyboard_shortcut.strip() if record.keyboard_shortcut else None
        values = (
            definition.name,
            definition.category,
            definition.file_path,
            int(record.enabled),
            definition.volume_percent,
            definition.fade_in_ms,
            definition.fade_out_ms,
            int(definition.ducking_enabled),
            definition.ducking_db,
            definition.ducking_attack_ms,
            definition.ducking_release_ms,
            definition.cue_in_ms,
            definition.cue_out_ms,
            record.favorite_position,
            shortcut,
        )
        try:
            with self._database.connect() as connection:
                if definition.overlay_id > 0:
                    cursor = connection.execute(
                        """UPDATE audio_overlays SET
                           name = ?, category = ?, file_path = ?, enabled = ?,
                           volume_percent = ?, fade_in_ms = ?, fade_out_ms = ?,
                           ducking_enabled = ?, ducking_db = ?, ducking_attack_ms = ?,
                           ducking_release_ms = ?, cue_in_ms = ?, cue_out_ms = ?,
                           favorite_position = ?, keyboard_shortcut = ?,
                           updated_at = CURRENT_TIMESTAMP
                           WHERE id = ?""",
                        (*values, definition.overlay_id),
                    )
                    if cursor.rowcount == 0:
                        raise KeyError(f"Overlay {definition.overlay_id} wurde nicht gefunden")
                    overlay_id = definition.overlay_id
                else:
                    cursor = connection.execute(
                        """INSERT INTO audio_overlays
                           (name, category, file_path, enabled, volume_percent,
                            fade_in_ms, fade_out_ms, ducking_enabled, ducking_db,
                            ducking_attack_ms, ducking_release_ms, cue_in_ms, cue_out_ms,
                            favorite_position, keyboard_shortcut)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        values,
                    )
                    assert cursor.lastrowid is not None
                    overlay_id = int(cursor.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise ValueError(self._integrity_message(exc)) from exc
        stored = self.get(overlay_id)
        assert stored is not None
        return stored

    def set_enabled(self, overlay_id: int, enabled: bool) -> OverlayRecord:
        with self._database.connect() as connection:
            cursor = connection.execute(
                """UPDATE audio_overlays SET enabled = ?, updated_at = CURRENT_TIMESTAMP
                   WHERE id = ?""",
                (int(enabled), overlay_id),
            )
            if cursor.rowcount == 0:
                raise KeyError(f"Overlay {overlay_id} wurde nicht gefunden")
        stored = self.get(overlay_id)
        assert stored is not None
        return stored

    def delete(self, overlay_id: int) -> bool:
        with self._database.connect() as connection:
            cursor = connection.execute("DELETE FROM audio_overlays WHERE id = ?", (overlay_id,))
        return cursor.rowcount > 0

    def import_conflicts(
        self, records: tuple[OverlayRecord, ...]
    ) -> tuple[tuple[int, str, int | None, str | None], ...]:
        names = {record.definition.name.casefold() for record in records}
        favorites = {
            record.favorite_position for record in records if record.favorite_position is not None
        }
        shortcuts = {
            record.keyboard_shortcut.casefold()
            for record in records
            if record.keyboard_shortcut is not None
        }
        return tuple(
            (
                record.definition.overlay_id,
                record.definition.name,
                record.favorite_position,
                record.keyboard_shortcut,
            )
            for record in self.list_all()
            if record.definition.name.casefold() in names
            or record.favorite_position in favorites
            or (
                record.keyboard_shortcut is not None
                and record.keyboard_shortcut.casefold() in shortcuts
            )
        )

    def import_records(
        self, records: tuple[OverlayRecord, ...], *, replace_existing: bool
    ) -> tuple[OverlayRecord, ...]:
        """Import a validated snapshot in one transaction."""

        imported: list[OverlayRecord] = []
        with self._database.transaction() as connection:
            existing = self.list_all()
            if replace_existing:
                conflict_ids = {item[0] for item in self.import_conflicts(records)}
                for overlay_id in conflict_ids:
                    connection.execute("DELETE FROM audio_overlays WHERE id = ?", (overlay_id,))
                existing = [
                    item for item in existing if item.definition.overlay_id not in conflict_ids
                ]
            occupied_names = {item.definition.name.casefold() for item in existing}
            occupied_favorites = {
                item.favorite_position for item in existing if item.favorite_position is not None
            }
            occupied_shortcuts = {
                item.keyboard_shortcut.casefold()
                for item in existing
                if item.keyboard_shortcut is not None
            }
            for record in records:
                if record.definition.name.casefold() in occupied_names:
                    continue
                favorite = record.favorite_position
                shortcut = record.keyboard_shortcut
                if favorite in occupied_favorites or (
                    shortcut is not None and shortcut.casefold() in occupied_shortcuts
                ):
                    favorite = None
                    shortcut = None
                definition = record.definition
                saved = self.save(
                    OverlayRecord(
                        OverlayDefinition(
                            0,
                            definition.name,
                            definition.file_path,
                            definition.category,
                            definition.volume_percent,
                            definition.fade_in_ms,
                            definition.fade_out_ms,
                            definition.cue_in_ms,
                            definition.cue_out_ms,
                            definition.ducking_enabled,
                            definition.ducking_db,
                            definition.ducking_attack_ms,
                            definition.ducking_release_ms,
                        ),
                        record.enabled,
                        favorite,
                        shortcut,
                    )
                )
                imported.append(saved)
                occupied_names.add(saved.definition.name.casefold())
                if saved.favorite_position is not None:
                    occupied_favorites.add(saved.favorite_position)
                if saved.keyboard_shortcut is not None:
                    occupied_shortcuts.add(saved.keyboard_shortcut.casefold())
        return tuple(imported)

    def add_history(
        self,
        record: OverlayRecord,
        *,
        started_at: datetime,
        completed_at: datetime,
        result: OverlayPlayResult,
        error_message: str = "",
    ) -> int:
        return self.add_definition_history(
            record.definition,
            started_at=started_at,
            completed_at=completed_at,
            result=result,
            error_message=error_message,
        )

    def add_definition_history(
        self,
        definition: OverlayDefinition,
        *,
        started_at: datetime,
        completed_at: datetime,
        result: OverlayPlayResult,
        error_message: str = "",
    ) -> int:
        overlay_id: int | None = definition.overlay_id if definition.overlay_id > 0 else None
        with self._database.connect() as connection:
            if overlay_id is not None:
                exists = connection.execute(
                    "SELECT 1 FROM audio_overlays WHERE id = ?",
                    (overlay_id,),
                ).fetchone()
                if exists is None:
                    overlay_id = None
            cursor = connection.execute(
                """INSERT INTO overlay_play_history
                   (overlay_id, overlay_name, started_at, completed_at, result, error_message)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    overlay_id,
                    definition.name,
                    started_at.isoformat(timespec="milliseconds"),
                    completed_at.isoformat(timespec="milliseconds"),
                    result.value,
                    error_message,
                ),
            )
        assert cursor.lastrowid is not None
        return int(cursor.lastrowid)

    @staticmethod
    def _normalized_definition(definition: OverlayDefinition) -> OverlayDefinition:
        name = " ".join(definition.name.split())
        category = " ".join(definition.category.split())
        file_path = definition.file_path.strip()
        if not name:
            raise ValueError("Overlay-Name darf nicht leer sein")
        if not file_path:
            raise ValueError("Overlay-Dateipfad darf nicht leer sein")
        return OverlayDefinition(
            definition.overlay_id,
            name,
            file_path,
            category,
            definition.volume_percent,
            definition.fade_in_ms,
            definition.fade_out_ms,
            definition.cue_in_ms,
            definition.cue_out_ms,
            definition.ducking_enabled,
            definition.ducking_db,
            definition.ducking_attack_ms,
            definition.ducking_release_ms,
        )

    @staticmethod
    def _integrity_message(error: sqlite3.IntegrityError) -> str:
        message = str(error)
        if "favorite_position" in message:
            return "Favoritenposition ist bereits belegt"
        if "keyboard_shortcut" in message:
            return "Tastenkürzel ist bereits belegt"
        if "name" in message:
            return "Overlay-Name ist bereits vergeben"
        return f"Ungültige Overlay-Einstellungen: {message}"

    @staticmethod
    def _from_row(row: sqlite3.Row) -> OverlayRecord:
        definition = OverlayDefinition(
            int(row["id"]),
            str(row["name"]),
            str(row["file_path"]),
            str(row["category"]),
            int(row["volume_percent"]),
            int(row["fade_in_ms"]),
            int(row["fade_out_ms"]),
            int(row["cue_in_ms"]),
            int(row["cue_out_ms"]) if row["cue_out_ms"] is not None else None,
            bool(row["ducking_enabled"]),
            float(row["ducking_db"]),
            int(row["ducking_attack_ms"]),
            int(row["ducking_release_ms"]),
        )
        return OverlayRecord(
            definition,
            bool(row["enabled"]),
            int(row["favorite_position"]) if row["favorite_position"] is not None else None,
            str(row["keyboard_shortcut"]) if row["keyboard_shortcut"] is not None else None,
            str(row["created_at"]),
            str(row["updated_at"]),
        )
