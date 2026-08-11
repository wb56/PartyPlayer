"""Prevalidated local fallback playlist independent from network storage."""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from party_player.file_availability import FileAvailabilityService
from party_player.emergency_storage import EmergencyStoragePolicy
from party_player.models import Track
from party_player.repositories.track_repository import TrackRepository


class EmergencyMediaType(StrEnum):
    PRIMARY = "PRIMARY"
    BREAK_MUSIC = "BREAK_MUSIC"
    JINGLE = "JINGLE"
    ANNOUNCEMENT = "ANNOUNCEMENT"


@dataclass(frozen=True, slots=True)
class EmergencyPlaylistIssue:
    track_id: int
    code: str
    reason: str
    media_type: EmergencyMediaType = EmergencyMediaType.PRIMARY


@dataclass(frozen=True, slots=True)
class EmergencyMediaEntry:
    media_type: EmergencyMediaType
    track: Track
    loop_allowed: bool


@dataclass(frozen=True, slots=True)
class EmergencyPlaylistValidation:
    validated_at: str
    ready: bool
    primary_track_id: int | None
    accepted_track_ids: tuple[int, ...]
    issues: tuple[EmergencyPlaylistIssue, ...]
    accepted_media: tuple[tuple[EmergencyMediaType, tuple[int, ...]], ...] = ()


class LocalEmergencyPlaylistService:
    def __init__(
        self,
        tracks: TrackRepository,
        availability: FileAvailabilityService,
        track_ids: list[int],
        audit: Callable[[str, dict[str, object]], None] | None = None,
        media_track_ids: dict[EmergencyMediaType, list[int]] | None = None,
        storage_policy: EmergencyStoragePolicy | None = None,
    ) -> None:
        self._logger = logging.getLogger(__name__)
        configured = {
            EmergencyMediaType.PRIMARY: list(track_ids),
            EmergencyMediaType.BREAK_MUSIC: [],
            EmergencyMediaType.JINGLE: [],
            EmergencyMediaType.ANNOUNCEMENT: [],
        }
        if media_track_ids is not None:
            for media_type, configured_ids in media_track_ids.items():
                configured[EmergencyMediaType(media_type)] = list(configured_ids)
        self._tracks_by_type: dict[EmergencyMediaType, list[Track]] = {
            media_type: [] for media_type in EmergencyMediaType
        }
        issues: list[EmergencyPlaylistIssue] = []
        claimed_track_ids: set[int] = set()
        for media_type, configured_ids in configured.items():
            for track_id in dict.fromkeys(configured_ids):
                if track_id in claimed_track_ids:
                    issues.append(
                        EmergencyPlaylistIssue(
                            track_id,
                            "DUPLICATE_MEDIA_ROLE",
                            "Ein Notfallmedium darf nur genau einer Rolle zugeordnet sein",
                            media_type,
                        )
                    )
                    continue
                track = tracks.get(track_id)
                if track is None:
                    issues.append(
                        EmergencyPlaylistIssue(
                            track_id, "TRACK_MISSING", "Titel fehlt im Katalog", media_type
                        )
                    )
                    continue
                if not availability.is_local(track):
                    issues.append(
                        EmergencyPlaylistIssue(
                            track_id,
                            "NOT_LOCAL",
                            "Notfallmedien müssen auf einem lokalen Laufwerk liegen",
                            media_type,
                        )
                    )
                    continue
                if storage_policy is not None:
                    storage = storage_policy.evaluate(track.file_path)
                    if not storage.allowed:
                        issues.append(
                            EmergencyPlaylistIssue(
                                track_id, storage.code, storage.reason, media_type
                            )
                        )
                        continue
                decision = availability.evaluate(track)
                if not decision.accepted:
                    issues.append(
                        EmergencyPlaylistIssue(
                            track_id,
                            decision.code,
                            decision.reason or "Titel ist nicht verfügbar",
                            media_type,
                        )
                    )
                    continue
                self._tracks_by_type[media_type].append(track)
                claimed_track_ids.add(track_id)
        accepted_ids = tuple(track.id for track in self._tracks_by_type[EmergencyMediaType.PRIMARY])
        accepted_media = tuple(
            (media_type, tuple(track.id for track in self._tracks_by_type[media_type]))
            for media_type in EmergencyMediaType
        )
        self._validation = EmergencyPlaylistValidation(
            datetime.now().astimezone().isoformat(),
            bool(accepted_ids),
            accepted_ids[0] if accepted_ids else None,
            accepted_ids,
            tuple(issues),
            accepted_media,
        )
        if audit is not None:
            audit(
                "EMERGENCY_PLAYLIST_VALIDATED",
                {
                    "ready": self._validation.ready,
                    "validated_at": self._validation.validated_at,
                    "primary_track_id": self._validation.primary_track_id,
                    "accepted_track_ids": list(accepted_ids),
                    "issues": [
                        {
                            "track_id": issue.track_id,
                            "code": issue.code,
                            "media_type": issue.media_type.value,
                        }
                        for issue in issues
                    ],
                    "accepted_media": {
                        media_type.value: list(ids) for media_type, ids in accepted_media
                    },
                },
            )

    def candidates(
        self, media_type: EmergencyMediaType = EmergencyMediaType.PRIMARY
    ) -> tuple[Track, ...]:
        return tuple(self._tracks_by_type[EmergencyMediaType(media_type)])

    def media_entries(self) -> tuple[EmergencyMediaEntry, ...]:
        return tuple(
            EmergencyMediaEntry(
                media_type,
                track,
                media_type == EmergencyMediaType.BREAK_MUSIC,
            )
            for media_type in EmergencyMediaType
            for track in self._tracks_by_type[media_type]
        )

    @staticmethod
    def loop_allowed(media_type: EmergencyMediaType) -> bool:
        return EmergencyMediaType(media_type) == EmergencyMediaType.BREAK_MUSIC

    def validation(self) -> EmergencyPlaylistValidation:
        return self._validation
