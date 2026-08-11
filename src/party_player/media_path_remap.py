"""Preview and atomically remap persisted Windows media path prefixes."""

from dataclasses import dataclass
from enum import StrEnum
from hashlib import sha256
import json
import ntpath
import sqlite3

from party_player.database.connection import Database


class MediaPathRemapErrorCode(StrEnum):
    NONE = ""
    PATH_INVALID = "MEDIA_PATH_REMAP_PATH_INVALID"
    COLLISION = "MEDIA_PATH_REMAP_COLLISION"
    NO_MATCH = "MEDIA_PATH_REMAP_NO_MATCH"
    STATE_CHANGED = "MEDIA_PATH_REMAP_STATE_CHANGED"
    COMMIT_FAILED = "MEDIA_PATH_REMAP_COMMIT_FAILED"


@dataclass(frozen=True, slots=True)
class MediaPathRemapChange:
    table: str
    row_id: int
    old_path: str
    new_path: str


@dataclass(frozen=True, slots=True)
class MediaPathRemapPreview:
    valid: bool
    can_commit: bool
    error_code: MediaPathRemapErrorCode
    message: str
    old_base_path: str
    new_base_path: str
    state_token: str = ""
    track_count: int = 0
    overlay_count: int = 0
    emergency_history_count: int = 0
    examples: tuple[MediaPathRemapChange, ...] = ()
    collisions: tuple[str, ...] = ()

    @property
    def affected_count(self) -> int:
        return self.track_count + self.overlay_count + self.emergency_history_count


@dataclass(frozen=True, slots=True)
class MediaPathRemapResult:
    success: bool
    error_code: MediaPathRemapErrorCode
    message: str
    affected_count: int = 0


class MediaPathRemapService:
    _TABLES = ("tracks", "audio_overlays", "emergency_play_history")

    def __init__(self, database: Database) -> None:
        self._database = database

    def preview(self, old_base_path: str, new_base_path: str) -> MediaPathRemapPreview:
        old_base = self._normalize_base(old_base_path)
        new_base = self._normalize_base(new_base_path)
        if old_base is None or new_base is None or self._same_path(old_base, new_base):
            return MediaPathRemapPreview(
                False,
                False,
                MediaPathRemapErrorCode.PATH_INVALID,
                "Quell- und Zielbasis müssen verschiedene absolute Windows- oder UNC-Pfade sein.",
                old_base_path,
                new_base_path,
            )
        changes: list[MediaPathRemapChange] = []
        all_track_paths: dict[int, str] = {}
        try:
            with self._database.connect() as connection:
                for table in self._TABLES:
                    rows = connection.execute(
                        f"SELECT id, file_path FROM {table} ORDER BY id"
                    ).fetchall()
                    if table == "tracks":
                        all_track_paths = {int(row["id"]): str(row["file_path"]) for row in rows}
                    for row in rows:
                        current = str(row["file_path"])
                        mapped = self._mapped_path(current, old_base, new_base)
                        if mapped is not None and mapped != current:
                            changes.append(
                                MediaPathRemapChange(table, int(row["id"]), current, mapped)
                            )
        except sqlite3.Error:
            return MediaPathRemapPreview(
                False,
                False,
                MediaPathRemapErrorCode.COMMIT_FAILED,
                "Persistierte Medienpfade konnten nicht gelesen werden.",
                old_base,
                new_base,
            )
        collisions = self._track_collisions(changes, all_track_paths)
        token = self._state_token(old_base, new_base, changes, all_track_paths)
        counts = {table: sum(change.table == table for change in changes) for table in self._TABLES}
        return MediaPathRemapPreview(
            True,
            not collisions and bool(changes),
            (
                MediaPathRemapErrorCode.COLLISION
                if collisions
                else (MediaPathRemapErrorCode.NONE if changes else MediaPathRemapErrorCode.NO_MATCH)
            ),
            (
                f"{len(changes)} Medienpfade können neu zugeordnet werden."
                if not collisions
                else f"{len(collisions)} Zielpfadkollisionen verhindern die Neuzuordnung."
            ),
            old_base,
            new_base,
            token,
            counts["tracks"],
            counts["audio_overlays"],
            counts["emergency_play_history"],
            tuple(changes[:10]),
            tuple(collisions[:10]),
        )

    def commit(self, preview: MediaPathRemapPreview) -> MediaPathRemapResult:
        if not preview.valid or not preview.can_commit:
            return MediaPathRemapResult(False, preview.error_code, preview.message)
        current = self.preview(preview.old_base_path, preview.new_base_path)
        if not current.valid or current.state_token != preview.state_token:
            return MediaPathRemapResult(
                False,
                MediaPathRemapErrorCode.STATE_CHANGED,
                "Persistierte Medienpfade wurden nach der Vorschau verändert.",
            )
        try:
            with self._database.transaction() as connection:
                updates: dict[str, list[tuple[int, str, str]]] = {}
                transaction_changes: list[MediaPathRemapChange] = []
                transaction_track_paths: dict[int, str] = {}
                for table in self._TABLES:
                    rows = connection.execute(
                        f"SELECT id, file_path FROM {table} ORDER BY id"
                    ).fetchall()
                    if table == "tracks":
                        transaction_track_paths = {
                            int(row["id"]): str(row["file_path"]) for row in rows
                        }
                    updates[table] = []
                    for row in rows:
                        mapped = self._mapped_path(
                            str(row["file_path"]),
                            preview.old_base_path,
                            preview.new_base_path,
                        )
                        if mapped is not None and mapped != str(row["file_path"]):
                            old_path = str(row["file_path"])
                            row_id = int(row["id"])
                            updates[table].append((row_id, old_path, mapped))
                            transaction_changes.append(
                                MediaPathRemapChange(table, row_id, old_path, mapped)
                            )
                transaction_token = self._state_token(
                    preview.old_base_path,
                    preview.new_base_path,
                    transaction_changes,
                    transaction_track_paths,
                )
                if transaction_token != preview.state_token:
                    raise sqlite3.IntegrityError("path state changed")
                for row_id, old_path, _mapped in updates["tracks"]:
                    changed = connection.execute(
                        "UPDATE tracks SET file_path = ? WHERE id = ? AND file_path = ?",
                        (
                            f"partyplayer-remap://{current.state_token}/{row_id}",
                            row_id,
                            old_path,
                        ),
                    )
                    if changed.rowcount != 1:
                        raise sqlite3.IntegrityError("track path changed")
                for table in self._TABLES:
                    for row_id, old_path, mapped in updates[table]:
                        expected = (
                            f"partyplayer-remap://{current.state_token}/{row_id}"
                            if table == "tracks"
                            else old_path
                        )
                        changed = connection.execute(
                            f"UPDATE {table} SET file_path = ? WHERE id = ? AND file_path = ?",
                            (mapped, row_id, expected),
                        )
                        if changed.rowcount != 1:
                            raise sqlite3.IntegrityError("media path changed")
        except sqlite3.Error:
            return MediaPathRemapResult(
                False,
                MediaPathRemapErrorCode.COMMIT_FAILED,
                "Medienpfade konnten nicht vollständig neu zugeordnet werden; alle Änderungen wurden zurückgerollt.",
            )
        return MediaPathRemapResult(
            True,
            MediaPathRemapErrorCode.NONE,
            "Medienpfade wurden atomar neu zugeordnet.",
            current.affected_count,
        )

    @staticmethod
    def _normalize_base(value: str) -> str | None:
        stripped = value.strip()
        if not stripped or not ntpath.isabs(stripped):
            return None
        normalized = ntpath.normpath(stripped)
        drive, tail = ntpath.splitdrive(normalized)
        unc_share = drive.startswith("\\") and tail in {"", "\\"}
        drive_path = bool(drive) and tail.startswith("\\")
        if not unc_share and not drive_path:
            return None
        return normalized

    @staticmethod
    def _same_path(left: str, right: str) -> bool:
        return ntpath.normcase(left) == ntpath.normcase(right)

    @staticmethod
    def _mapped_path(path: str, old_base: str, new_base: str) -> str | None:
        normalized = ntpath.normpath(path.strip())
        old_key = ntpath.normcase(old_base)
        path_key = ntpath.normcase(normalized)
        if path_key == old_key:
            return new_base
        prefix = old_key if old_key.endswith("\\") else old_key + "\\"
        if not path_key.startswith(prefix):
            return None
        suffix = normalized[len(prefix) :]
        return ntpath.normpath(ntpath.join(new_base, suffix))

    @staticmethod
    def _track_collisions(
        changes: list[MediaPathRemapChange], all_track_paths: dict[int, str]
    ) -> list[str]:
        mapped = {change.row_id: change.new_path for change in changes if change.table == "tracks"}
        final_paths = {
            track_id: mapped.get(track_id, path) for track_id, path in all_track_paths.items()
        }
        owners: dict[str, int] = {}
        collisions: list[str] = []
        for track_id, path in final_paths.items():
            key = ntpath.normcase(ntpath.normpath(path))
            previous = owners.get(key)
            if previous is not None and previous != track_id:
                collisions.append(path)
            else:
                owners[key] = track_id
        return list(dict.fromkeys(collisions))

    @staticmethod
    def _state_token(
        old_base: str,
        new_base: str,
        changes: list[MediaPathRemapChange],
        all_track_paths: dict[int, str],
    ) -> str:
        payload = {
            "old": old_base,
            "new": new_base,
            "changes": [
                (change.table, change.row_id, change.old_path, change.new_path)
                for change in changes
            ],
            "tracks": sorted(all_track_paths.items()),
        }
        return sha256(json.dumps(payload, ensure_ascii=False).encode("utf-8")).hexdigest()
