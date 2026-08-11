"""Portable, validated playlist export and atomic catalog-bound import."""

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
import json
import os
from pathlib import Path
from collections.abc import Callable
from hashlib import sha256
from party_player.models import SavedQueue, SavedQueueEntry, Track
from party_player.repositories.saved_queue_repository import SavedQueueRepository
from party_player.repositories.track_repository import TrackRepository
from party_player.saved_queue_service import SavedQueueService


PLAYLIST_FORMAT_VERSION = 1
MAX_PLAYLIST_BYTES = 10 * 1024 * 1024
MAX_PLAYLIST_ENTRIES = 10_000


class PlaylistTransferFormat(StrEnum):
    JSON = "JSON"
    M3U8 = "M3U8"


class PlaylistConflictStrategy(StrEnum):
    ERROR = "ERROR"
    SKIP = "SKIP"
    REPLACE = "REPLACE"
    RENAME = "RENAME"
    APPEND = "APPEND"


class PlaylistTransferErrorCode(StrEnum):
    NONE = ""
    PLAYLIST_NOT_FOUND = "PLAYLIST_NOT_FOUND"
    FORMAT_INVALID = "PLAYLIST_FORMAT_INVALID"
    VERSION_UNSUPPORTED = "PLAYLIST_VERSION_UNSUPPORTED"
    ENTRY_LIMIT_EXCEEDED = "PLAYLIST_ENTRY_LIMIT_EXCEEDED"
    TRACK_NOT_FOUND = "PLAYLIST_TRACK_NOT_FOUND"
    NAME_CONFLICT = "PLAYLIST_NAME_CONFLICT"
    IO_FAILED = "PLAYLIST_IO_FAILED"
    SOURCE_CHANGED = "PLAYLIST_SOURCE_CHANGED"


@dataclass(frozen=True, slots=True)
class PlaylistTransferResult:
    success: bool
    error_code: PlaylistTransferErrorCode
    message: str
    playlist: SavedQueue | None = None
    path: Path | None = None
    skipped: bool = False


PlaylistImportedEntry = tuple[str, float | None, float | None, float | None, str]


@dataclass(frozen=True, slots=True)
class PlaylistImportPreview:
    valid: bool
    can_import: bool
    error_code: PlaylistTransferErrorCode
    message: str
    source: Path
    format: PlaylistTransferFormat
    source_sha256: str = ""
    name: str = ""
    entries: tuple[PlaylistImportedEntry, ...] = ()
    entry_count: int = 0
    duplicate_count: int = 0
    unknown_path_count: int = 0
    unknown_path_examples: tuple[str, ...] = ()
    name_conflict: bool = False
    existing_playlist_id: int | None = None


class PlaylistTransferService:
    def __init__(
        self,
        playlists: SavedQueueRepository,
        tracks: TrackRepository,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._playlists = playlists
        self._tracks = tracks
        self._now = now or (lambda: datetime.now(timezone.utc))

    def export(
        self,
        saved_queue_id: int,
        destination: Path,
        format: PlaylistTransferFormat,
    ) -> PlaylistTransferResult:
        playlist = self._playlists.get(saved_queue_id)
        if playlist is None:
            return self._failure(
                PlaylistTransferErrorCode.PLAYLIST_NOT_FOUND,
                "Die zu exportierende Playlist wurde nicht gefunden.",
            )
        tracks = self._tracks.get_many(list(playlist.track_ids))
        if any(entry.track_id not in tracks for entry in playlist.entries):
            return self._failure(
                PlaylistTransferErrorCode.TRACK_NOT_FOUND,
                "Die Playlist enthält einen nicht mehr vorhandenen Katalogtitel.",
            )
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            temporary = destination.with_name(f".{destination.name}.{os.getpid()}.tmp")
            payload = (
                self._json_payload(playlist, tracks, self._now())
                if format is PlaylistTransferFormat.JSON
                else self._m3u8_payload(playlist, tracks)
            )
            temporary.write_text(payload, encoding="utf-8", newline="\n")
            os.replace(temporary, destination)
        except OSError:
            try:
                temporary.unlink(missing_ok=True)
            except (OSError, UnboundLocalError):
                pass
            return self._failure(
                PlaylistTransferErrorCode.IO_FAILED,
                "Die Playlist konnte nicht atomar exportiert werden.",
            )
        return PlaylistTransferResult(
            True,
            PlaylistTransferErrorCode.NONE,
            "Playlist wurde exportiert.",
            playlist,
            destination,
        )

    def import_file(
        self,
        source: Path,
        format: PlaylistTransferFormat,
        conflict: PlaylistConflictStrategy = PlaylistConflictStrategy.ERROR,
    ) -> PlaylistTransferResult:
        return self.import_preview(self.preview_import(source, format), conflict)

    def preview_import(self, source: Path, format: PlaylistTransferFormat) -> PlaylistImportPreview:
        try:
            if not source.is_file() or source.stat().st_size > MAX_PLAYLIST_BYTES:
                return self._preview_failure(
                    source,
                    format,
                    PlaylistTransferErrorCode.FORMAT_INVALID,
                    "Die Playlistdatei fehlt oder überschreitet die Größenbegrenzung.",
                )
            source_bytes = source.read_bytes()
            text = source_bytes.decode("utf-8-sig")
        except (OSError, UnicodeError):
            return self._preview_failure(
                source,
                format,
                PlaylistTransferErrorCode.IO_FAILED,
                "Die Playlistdatei konnte nicht gelesen werden.",
            )
        parsed = (
            self._parse_json(text)
            if format is PlaylistTransferFormat.JSON
            else self._parse_m3u8(text, source.stem)
        )
        if isinstance(parsed, PlaylistTransferResult):
            return self._preview_failure(source, format, parsed.error_code, parsed.message)
        name, imported = parsed
        if format is PlaylistTransferFormat.M3U8:
            imported = [
                (
                    str((source.parent / path).resolve()) if not Path(path).is_absolute() else path,
                    cue_in,
                    cue_out,
                    fade,
                    cue_source,
                )
                for path, cue_in, cue_out, fade, cue_source in imported
            ]
        if len(imported) > MAX_PLAYLIST_ENTRIES:
            return self._preview_failure(
                source,
                format,
                PlaylistTransferErrorCode.ENTRY_LIMIT_EXCEEDED,
                "Die Playlist enthält zu viele Einträge.",
            )
        catalog = self._tracks.get_by_file_paths([entry[0] for entry in imported])
        missing = [path for path, *_rest in imported if path.casefold() not in catalog]
        existing_playlists = {item.name.casefold(): item for item in self._playlists.list_all()}
        existing = existing_playlists.get(name.casefold())
        normalized_paths = [entry[0].casefold() for entry in imported]
        duplicate_count = len(normalized_paths) - len(set(normalized_paths))
        return PlaylistImportPreview(
            True,
            not missing,
            (
                PlaylistTransferErrorCode.NONE
                if not missing
                else PlaylistTransferErrorCode.TRACK_NOT_FOUND
            ),
            (
                "Playlist kann importiert werden."
                if not missing
                else f"{len(missing)} Playlisttitel wurden im Katalog nicht gefunden."
            ),
            source,
            format,
            sha256(source_bytes).hexdigest(),
            name,
            tuple(imported),
            len(imported),
            duplicate_count,
            len(missing),
            tuple(dict.fromkeys(missing))[:10],
            existing is not None,
            existing.saved_queue_id if existing is not None else None,
        )

    def import_preview(
        self,
        preview: PlaylistImportPreview,
        conflict: PlaylistConflictStrategy = PlaylistConflictStrategy.ERROR,
    ) -> PlaylistTransferResult:
        if not preview.valid:
            return self._failure(preview.error_code, preview.message)
        if not preview.can_import:
            return self._failure(
                PlaylistTransferErrorCode.TRACK_NOT_FOUND,
                preview.message,
            )
        try:
            current_digest = sha256(preview.source.read_bytes()).hexdigest()
        except OSError:
            current_digest = ""
        if current_digest != preview.source_sha256:
            return self._failure(
                PlaylistTransferErrorCode.SOURCE_CHANGED,
                "Die Playlistdatei wurde nach der Vorschau verändert.",
            )
        name = preview.name
        imported = list(preview.entries)
        catalog = self._tracks.get_by_file_paths([entry[0] for entry in imported])
        if any(path.casefold() not in catalog for path, *_rest in imported):
            return self._failure(
                PlaylistTransferErrorCode.TRACK_NOT_FOUND,
                "Mindestens ein Playlisttitel ist nicht mehr im Katalog vorhanden.",
            )
        existing_playlists = {item.name.casefold(): item for item in self._playlists.list_all()}
        existing_names = {key: item.name for key, item in existing_playlists.items()}
        append_to: SavedQueue | None = None
        if name.casefold() in existing_names:
            if conflict is PlaylistConflictStrategy.ERROR:
                return self._failure(
                    PlaylistTransferErrorCode.NAME_CONFLICT,
                    "Eine Playlist mit diesem Namen ist bereits vorhanden.",
                )
            if conflict is PlaylistConflictStrategy.SKIP:
                return PlaylistTransferResult(
                    True,
                    PlaylistTransferErrorCode.NONE,
                    "Vorhandene Playlist wurde unverändert übersprungen.",
                    skipped=True,
                )
            if conflict is PlaylistConflictStrategy.RENAME:
                name = self._available_name(name, set(existing_names))
            elif conflict is PlaylistConflictStrategy.APPEND:
                summary = existing_playlists[name.casefold()]
                append_to = self._playlists.get(summary.saved_queue_id)
        entries = [
            SavedQueueEntry(
                catalog[path.casefold()].id,
                position,
                cue_in,
                cue_out,
                fade,
                cue_source,
            )
            for position, (path, cue_in, cue_out, fade, cue_source) in enumerate(imported, start=1)
        ]
        try:
            if append_to is not None:
                entries = [*append_to.entries, *entries]
            for entry in entries:
                SavedQueueService.validate_snapshot(entry, self._tracks.get(entry.track_id))
        except ValueError:
            return self._failure(
                PlaylistTransferErrorCode.FORMAT_INVALID,
                "Die Playlist enthält einen ungültigen Cue-Snapshot.",
            )
        try:
            playlist = self._playlists.save(name, entries)
        except (ValueError, OSError):
            return self._failure(
                PlaylistTransferErrorCode.IO_FAILED,
                "Die importierte Playlist konnte nicht gespeichert werden.",
            )
        return PlaylistTransferResult(
            True,
            PlaylistTransferErrorCode.NONE,
            "Playlist wurde importiert.",
            playlist,
            preview.source,
        )

    @staticmethod
    def _json_payload(playlist: SavedQueue, tracks: dict[int, Track], created_at: datetime) -> str:
        value = {
            "type": "partyplayer-playlist",
            "format_version": PLAYLIST_FORMAT_VERSION,
            "created_at": created_at.astimezone(timezone.utc).isoformat(),
            "payload": {
                "name": playlist.name,
                "entries": [
                    {
                        "file_path": tracks[entry.track_id].file_path,
                        "title": tracks[entry.track_id].title,
                        "artist": tracks[entry.track_id].artist,
                        "cue_in": entry.cue_in,
                        "cue_out": entry.cue_out,
                        "fade_duration": entry.fade_duration,
                        "cue_source": entry.cue_source,
                    }
                    for entry in playlist.entries
                ],
            },
        }
        return json.dumps(value, ensure_ascii=False, indent=2) + "\n"

    @staticmethod
    def _m3u8_payload(playlist: SavedQueue, tracks: dict[int, Track]) -> str:
        lines = ["#EXTM3U", f"#PLAYLIST:{playlist.name}"]
        for entry in playlist.entries:
            track = tracks[entry.track_id]
            duration = int(track.duration_seconds) if track.duration_seconds is not None else -1
            lines.extend((f"#EXTINF:{duration},{track.artist} - {track.title}", track.file_path))
        return "\n".join(lines) + "\n"

    @staticmethod
    def _parse_json(
        text: str,
    ) -> (
        tuple[str, list[tuple[str, float | None, float | None, float | None, str]]]
        | PlaylistTransferResult
    ):
        try:
            value = json.loads(text)
        except (json.JSONDecodeError, UnicodeError):
            return PlaylistTransferService._failure(
                PlaylistTransferErrorCode.FORMAT_INVALID, "Playlist-JSON ist ungültig."
            )
        if (
            not isinstance(value, dict)
            or set(value) != {"type", "format_version", "created_at", "payload"}
            or value.get("type") != "partyplayer-playlist"
        ):
            return PlaylistTransferService._failure(
                PlaylistTransferErrorCode.FORMAT_INVALID,
                "Playlist-JSON hat ein unbekanntes Format.",
            )
        if value.get("format_version") != PLAYLIST_FORMAT_VERSION:
            return PlaylistTransferService._failure(
                PlaylistTransferErrorCode.VERSION_UNSUPPORTED,
                "Die Playlistversion wird nicht unterstützt.",
            )
        payload = value.get("payload")
        created_at = value.get("created_at")
        try:
            parsed_created_at = (
                datetime.fromisoformat(created_at) if isinstance(created_at, str) else None
            )
        except ValueError:
            parsed_created_at = None
        if (
            not isinstance(payload, dict)
            or set(payload) != {"name", "entries"}
            or parsed_created_at is None
            or parsed_created_at.tzinfo is None
        ):
            return PlaylistTransferService._failure(
                PlaylistTransferErrorCode.FORMAT_INVALID, "Playlist-JSON-Hülle ist ungültig."
            )
        name = payload.get("name")
        raw_entries = payload.get("entries")
        if (
            not isinstance(name, str)
            or not name.strip()
            or not isinstance(raw_entries, list)
            or not raw_entries
        ):
            return PlaylistTransferService._failure(
                PlaylistTransferErrorCode.FORMAT_INVALID, "Playlistname oder Einträge fehlen."
            )
        entries: list[tuple[str, float | None, float | None, float | None, str]] = []
        allowed = {
            "file_path",
            "title",
            "artist",
            "cue_in",
            "cue_out",
            "fade_duration",
            "cue_source",
        }
        for item in raw_entries:
            if not isinstance(item, dict) or set(item) != allowed:
                return PlaylistTransferService._failure(
                    PlaylistTransferErrorCode.FORMAT_INVALID, "Ein Playlist-Eintrag ist ungültig."
                )
            path = item["file_path"]
            numeric = (item["cue_in"], item["cue_out"], item["fade_duration"])
            source = item["cue_source"]
            if (
                not isinstance(path, str)
                or not path
                or not isinstance(item["title"], str)
                or not isinstance(item["artist"], str)
                or any(value is not None and type(value) not in {int, float} for value in numeric)
                or source not in {"inherited", "snapshot"}
            ):
                return PlaylistTransferService._failure(
                    PlaylistTransferErrorCode.FORMAT_INVALID, "Ein Playlist-Eintrag ist ungültig."
                )
            entries.append((path, *numeric, source))
        return name.strip(), entries

    @staticmethod
    def _parse_m3u8(
        text: str, fallback_name: str
    ) -> tuple[str, list[tuple[str, None, None, None, str]]] | PlaylistTransferResult:
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        if not lines or lines[0] != "#EXTM3U":
            return PlaylistTransferService._failure(
                PlaylistTransferErrorCode.FORMAT_INVALID, "M3U8-Kopfzeile fehlt."
            )
        name = next(
            (line[10:].strip() for line in lines if line.startswith("#PLAYLIST:")), fallback_name
        )
        paths = [line for line in lines[1:] if not line.startswith("#")]
        if not name or not paths:
            return PlaylistTransferService._failure(
                PlaylistTransferErrorCode.FORMAT_INVALID, "M3U8 enthält keine Playlisttitel."
            )
        return name, [(path, None, None, None, "inherited") for path in paths]

    @staticmethod
    def _available_name(name: str, existing: set[str]) -> str:
        suffix = 2
        while f"{name} ({suffix})".casefold() in existing:
            suffix += 1
        return f"{name} ({suffix})"

    @staticmethod
    def _preview_failure(
        source: Path,
        format: PlaylistTransferFormat,
        code: PlaylistTransferErrorCode,
        message: str,
    ) -> PlaylistImportPreview:
        return PlaylistImportPreview(False, False, code, message, source, format)

    @staticmethod
    def _failure(code: PlaylistTransferErrorCode, message: str) -> PlaylistTransferResult:
        return PlaylistTransferResult(False, code, message)
