"""Persistence for equalizer presets and inherited assignments."""

import sqlite3

from party_player.database.connection import Database
from party_player.equalizer import EqualizerPreset


class EqualizerPresetRepository:
    def __init__(self, database: Database) -> None:
        self._database = database

    def list_enabled(self) -> list[EqualizerPreset]:
        with self._database.connect() as connection:
            rows = connection.execute(
                """SELECT id, preset_key, name, preamp_db
                   FROM equalizer_presets WHERE is_enabled = 1
                   ORDER BY is_builtin DESC, name COLLATE NOCASE"""
            ).fetchall()
            return [self._load_bands(connection, row) for row in rows]

    def get(self, preset_id: int) -> EqualizerPreset | None:
        with self._database.connect() as connection:
            row = connection.execute(
                """SELECT id, preset_key, name, preamp_db FROM equalizer_presets
                   WHERE id = ? AND is_enabled = 1""",
                (preset_id,),
            ).fetchone()
            return self._load_bands(connection, row) if row is not None else None

    def get_by_key(self, preset_key: str) -> EqualizerPreset | None:
        with self._database.connect() as connection:
            row = connection.execute(
                """SELECT id, preset_key, name, preamp_db FROM equalizer_presets
                   WHERE preset_key = ? COLLATE NOCASE AND is_enabled = 1""",
                (preset_key.strip(),),
            ).fetchone()
            return self._load_bands(connection, row) if row is not None else None

    def save_custom(self, preset: EqualizerPreset, description: str = "") -> EqualizerPreset:
        """Atomically create or update a custom preset and all of its bands."""
        key = preset.preset_id.strip()
        name = preset.name.strip()
        if not key or not name:
            raise ValueError("Equalizer-Preset benötigt Schlüssel und Namen")
        with self._database.connect() as connection:
            existing = connection.execute(
                "SELECT id, is_builtin FROM equalizer_presets WHERE preset_key = ?",
                (key,),
            ).fetchone()
            if existing is not None and bool(existing["is_builtin"]):
                raise ValueError("Eingebaute Equalizer-Presets können nicht verändert werden")
            connection.execute(
                """INSERT INTO equalizer_presets
                   (preset_key, name, description, preamp_db)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(preset_key) DO UPDATE SET
                       name = excluded.name,
                       description = excluded.description,
                       preamp_db = excluded.preamp_db,
                       updated_at = CURRENT_TIMESTAMP""",
                (key, name, description.strip(), preset.preamp_db),
            )
            row = connection.execute(
                """SELECT id, preset_key, name, preamp_db FROM equalizer_presets
                   WHERE preset_key = ?""",
                (key,),
            ).fetchone()
            assert row is not None
            preset_id = int(row["id"])
            connection.execute(
                "DELETE FROM equalizer_preset_bands WHERE preset_id = ?", (preset_id,)
            )
            connection.executemany(
                """INSERT INTO equalizer_preset_bands
                   (preset_id, band_index, frequency_hz, gain_db)
                   VALUES (?, ?, ?, ?)""",
                [
                    (preset_id, index, frequency, gain)
                    for index, (frequency, gain) in enumerate(preset.curve)
                ],
            )
            return self._load_bands(connection, row)

    def import_conflicts(
        self, preset_key: str, name: str
    ) -> tuple[tuple[int, str, str, bool], ...]:
        """Return bounded identity conflicts without mutating assignments."""
        with self._database.connect() as connection:
            rows = connection.execute(
                """SELECT id, preset_key, name, is_builtin FROM equalizer_presets
                   WHERE preset_key = ? COLLATE NOCASE OR name = ? COLLATE NOCASE
                   ORDER BY id""",
                (preset_key.strip(), name.strip()),
            ).fetchall()
        return tuple(
            (int(row["id"]), str(row["preset_key"]), str(row["name"]), bool(row["is_builtin"]))
            for row in rows
        )

    def import_custom(self, preset: EqualizerPreset, *, replace: bool) -> EqualizerPreset:
        """Import one validated custom preset and resolve all identity conflicts atomically."""
        key = preset.preset_id.strip()
        name = preset.name.strip()
        if not key or not name:
            raise ValueError("Equalizer-Preset benötigt Schlüssel und Namen")
        with self._database.transaction() as connection:
            conflicts = connection.execute(
                """SELECT id, preset_key, is_builtin FROM equalizer_presets
                   WHERE preset_key = ? COLLATE NOCASE OR name = ? COLLATE NOCASE
                   ORDER BY id""",
                (key, name),
            ).fetchall()
            if conflicts and not replace:
                raise ValueError("Equalizer-Preset steht in Konflikt")
            if any(bool(row["is_builtin"]) for row in conflicts):
                raise ValueError("Eingebaute Equalizer-Presets können nicht verändert werden")
            same_key = next(
                (row for row in conflicts if str(row["preset_key"]).casefold() == key.casefold()),
                None,
            )
            keep_id = int(same_key["id"]) if same_key is not None else None
            if keep_id is None and conflicts:
                keep_id = int(conflicts[0]["id"])
            for row in conflicts:
                row_id = int(row["id"])
                if row_id != keep_id:
                    connection.execute("DELETE FROM equalizer_presets WHERE id = ?", (row_id,))
            if keep_id is None:
                cursor = connection.execute(
                    """INSERT INTO equalizer_presets
                       (preset_key, name, description, preamp_db)
                       VALUES (?, ?, '', ?)""",
                    (key, name, preset.preamp_db),
                )
                assert cursor.lastrowid is not None
                keep_id = int(cursor.lastrowid)
            else:
                connection.execute(
                    """UPDATE equalizer_presets
                       SET preset_key = ?, name = ?, description = '', preamp_db = ?,
                           updated_at = CURRENT_TIMESTAMP WHERE id = ?""",
                    (key, name, preset.preamp_db, keep_id),
                )
                connection.execute(
                    "DELETE FROM equalizer_preset_bands WHERE preset_id = ?", (keep_id,)
                )
            connection.executemany(
                """INSERT INTO equalizer_preset_bands
                   (preset_id, band_index, frequency_hz, gain_db) VALUES (?, ?, ?, ?)""",
                [
                    (keep_id, index, frequency, gain)
                    for index, (frequency, gain) in enumerate(preset.curve)
                ],
            )
            row = connection.execute(
                """SELECT id, preset_key, name, preamp_db FROM equalizer_presets
                   WHERE id = ?""",
                (keep_id,),
            ).fetchone()
            assert row is not None
            return self._load_bands(connection, row)

    def delete_custom(self, preset_id: int) -> bool:
        with self._database.connect() as connection:
            row = connection.execute(
                "SELECT is_builtin FROM equalizer_presets WHERE id = ?", (preset_id,)
            ).fetchone()
            if row is None:
                return False
            if bool(row["is_builtin"]):
                raise ValueError("Eingebaute Equalizer-Presets können nicht gelöscht werden")
            connection.execute("DELETE FROM equalizer_presets WHERE id = ?", (preset_id,))
            return True

    @staticmethod
    def _load_bands(connection: sqlite3.Connection, row: sqlite3.Row) -> EqualizerPreset:
        band_rows = connection.execute(
            """SELECT frequency_hz, gain_db FROM equalizer_preset_bands
               WHERE preset_id = ? ORDER BY band_index""",
            (int(row["id"]),),
        ).fetchall()
        return EqualizerPreset(
            str(row["preset_key"]),
            str(row["name"]),
            float(row["preamp_db"]),
            tuple((float(item["frequency_hz"]), float(item["gain_db"])) for item in band_rows),
            int(row["id"]),
        )


class EqualizerAssignmentRepository:
    def __init__(self, database: Database) -> None:
        self._database = database

    def track_preset_id(self, track_id: int) -> int | None:
        return self._single_id(
            "SELECT equalizer_preset_id FROM track_equalizer_assignments WHERE track_id = ?",
            track_id,
        )

    def saved_queue_preset_id(self, saved_queue_id: int) -> int | None:
        return self._single_id(
            "SELECT equalizer_preset_id FROM saved_queues WHERE id = ?",
            saved_queue_id,
        )

    def genre_preset_id(self, genre: str) -> int | None:
        key = self.normalize_genre(genre)
        if not key:
            return None
        return self._single_id(
            """SELECT equalizer_preset_id FROM genre_equalizer_assignments
               WHERE genre_key = ? COLLATE NOCASE""",
            key,
        )

    def snapshot(
        self,
    ) -> tuple[dict[int, int], dict[int, int], dict[str, int]]:
        """Load all assignment levels for lock-free runtime resolution."""
        with self._database.connect() as connection:
            tracks = {
                int(row["track_id"]): int(row["equalizer_preset_id"])
                for row in connection.execute(
                    "SELECT track_id, equalizer_preset_id FROM track_equalizer_assignments"
                )
            }
            queues = {
                int(row["id"]): int(row["equalizer_preset_id"])
                for row in connection.execute(
                    """SELECT id, equalizer_preset_id FROM saved_queues
                       WHERE equalizer_preset_id IS NOT NULL"""
                )
            }
            genres = {
                str(row["genre_key"]): int(row["equalizer_preset_id"])
                for row in connection.execute(
                    """SELECT genre_key, equalizer_preset_id
                       FROM genre_equalizer_assignments"""
                )
            }
        return tracks, queues, genres

    def assign_track(self, track_id: int, preset_id: int | None) -> None:
        self._assign(
            "track_equalizer_assignments",
            "track_id",
            track_id,
            preset_id,
        )

    def assign_saved_queue(self, saved_queue_id: int, preset_id: int | None) -> None:
        with self._database.connect() as connection:
            connection.execute(
                "UPDATE saved_queues SET equalizer_preset_id = ? WHERE id = ?",
                (preset_id, saved_queue_id),
            )

    def assign_genre(self, genre: str, preset_id: int | None) -> None:
        key = self.normalize_genre(genre)
        if not key:
            raise ValueError("Genre darf nicht leer sein")
        with self._database.connect() as connection:
            if preset_id is None:
                connection.execute(
                    "DELETE FROM genre_equalizer_assignments WHERE genre_key = ? COLLATE NOCASE",
                    (key,),
                )
            else:
                connection.execute(
                    """INSERT INTO genre_equalizer_assignments
                       (genre_key, genre_name, equalizer_preset_id) VALUES (?, ?, ?)
                       ON CONFLICT(genre_key) DO UPDATE SET
                           genre_name = excluded.genre_name,
                           equalizer_preset_id = excluded.equalizer_preset_id""",
                    (key, genre.strip(), preset_id),
                )

    @staticmethod
    def normalize_genre(genre: str) -> str:
        return " ".join(genre.strip().casefold().split())

    def _single_id(self, sql: str, value: int | str) -> int | None:
        with self._database.connect() as connection:
            row = connection.execute(sql, (value,)).fetchone()
        if row is None or row["equalizer_preset_id"] is None:
            return None
        return int(row["equalizer_preset_id"])

    def _assign(
        self,
        table: str,
        entity_column: str,
        entity_id: int,
        preset_id: int | None,
    ) -> None:
        with self._database.connect() as connection:
            if preset_id is None:
                connection.execute(f"DELETE FROM {table} WHERE {entity_column} = ?", (entity_id,))
            else:
                connection.execute(
                    f"""INSERT INTO {table} ({entity_column}, equalizer_preset_id)
                        VALUES (?, ?) ON CONFLICT({entity_column}) DO UPDATE SET
                        equalizer_preset_id = excluded.equalizer_preset_id""",
                    (entity_id, preset_id),
                )
