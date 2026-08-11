"""Composable, GUI-independent rules for automatic queue candidates."""

from collections import deque
from dataclasses import dataclass
import math
from typing import Protocol
import re

from party_player.enums import QueueStatus
from party_player.models import QueueEntry, Track


@dataclass(frozen=True, slots=True)
class SelectionDecision:
    """Stable result of evaluating one ordered queue candidate."""

    accepted: bool
    code: str = ""
    terminal_status: QueueStatus = QueueStatus.SKIPPED
    reason: str = ""

    @classmethod
    def allow(cls) -> "SelectionDecision":
        return cls(True)

    @classmethod
    def reject(
        cls,
        code: str,
        *,
        terminal_status: QueueStatus = QueueStatus.SKIPPED,
        reason: str = "",
    ) -> "SelectionDecision":
        if terminal_status not in {QueueStatus.SKIPPED, QueueStatus.FAILED}:
            raise ValueError("Auswahlablehnung benötigt SKIPPED oder FAILED")
        return cls(False, code, terminal_status, reason)


class SelectionRule(Protocol):
    """One deterministic rule without GUI or deck dependencies."""

    def evaluate(
        self,
        entry: QueueEntry,
        track: Track,
    ) -> SelectionDecision | None: ...


class TrackSelectionService:
    """Evaluate availability first and then injected event/business rules."""

    _NON_RELAXABLE_CODES = frozenset(
        {
            "BLOCKED_TRACK",
            "BLOCKED_ARTIST",
            "RESTRICTED_TRACK",
            "UNSUITABLE_TRACK",
        }
    )

    def __init__(self, rules: tuple[SelectionRule, ...] = ()) -> None:
        self._rules = rules

    def evaluate(
        self,
        entry: QueueEntry,
        track: Track | None,
        *,
        relaxed_codes: frozenset[str] = frozenset(),
    ) -> SelectionDecision:
        if track is None:
            return SelectionDecision.reject(
                "TRACK_MISSING",
                terminal_status=QueueStatus.FAILED,
                reason="Katalogeintrag nicht gefunden",
            )
        if (
            not track.file_path.strip()
            or not track.title.strip()
            or (
                track.duration_seconds is not None
                and (not math.isfinite(track.duration_seconds) or track.duration_seconds <= 0)
            )
        ):
            return SelectionDecision.reject(
                "INVALID_METADATA",
                terminal_status=QueueStatus.FAILED,
                reason="Der Katalogeintrag enthält ungültige Metadaten",
            )
        for rule in self._rules:
            decision = rule.evaluate(entry, track)
            if decision is not None and not decision.accepted:
                if (
                    decision.code in relaxed_codes
                    and decision.code not in self._NON_RELAXABLE_CODES
                ):
                    continue
                return decision
        return SelectionDecision.allow()


def normalize_artist_name(name: str) -> str:
    """Provide one stable baseline identity for artist selection rules."""
    normalized = name.casefold().strip()
    normalized = re.sub(r"\s+(?:feat\.?|ft\.?|featuring)\s+", "|", normalized)
    normalized = re.sub(r"\s*(?:&|/|;)\s*", "|", normalized)
    return "|".join(" ".join(part.split()) for part in normalized.split("|") if part.strip())


class BlockService:
    """Reject explicitly blocked track and normalized artist identities."""

    def __init__(
        self,
        blocked_track_ids: set[int] | None = None,
        blocked_artists: set[str] | None = None,
    ) -> None:
        self._blocked_track_ids = set(blocked_track_ids or ())
        self._blocked_artists = {normalize_artist_name(artist) for artist in blocked_artists or ()}

    def block_track(self, track_id: int) -> None:
        self._blocked_track_ids.add(track_id)

    def allow_track(self, track_id: int) -> None:
        self._blocked_track_ids.discard(track_id)

    def block_artist(self, artist: str) -> None:
        self._blocked_artists.add(normalize_artist_name(artist))

    def allow_artist(self, artist: str) -> None:
        self._blocked_artists.discard(normalize_artist_name(artist))

    def evaluate(
        self,
        _entry: QueueEntry,
        track: Track,
    ) -> SelectionDecision | None:
        if track.id in self._blocked_track_ids:
            return SelectionDecision.reject(
                "BLOCKED_TRACK",
                reason="Titel ist für die automatische Auswahl gesperrt",
            )
        if normalize_artist_name(track.artist) in self._blocked_artists:
            return SelectionDecision.reject(
                "BLOCKED_ARTIST",
                reason="Interpret ist für die automatische Auswahl gesperrt",
            )
        return None


class RepetitionService:
    """Reject tracks or artists present in bounded recent-play windows."""

    def __init__(
        self,
        *,
        track_window_size: int = 0,
        artist_window_size: int = 0,
    ) -> None:
        self.track_window_size = max(0, track_window_size)
        self.artist_window_size = max(0, artist_window_size)
        maximum = max(1, self.track_window_size, self.artist_window_size)
        self._recent_track_ids: deque[int] = deque(maxlen=maximum)
        self._recent_artists: deque[str] = deque(maxlen=maximum)

    def record_played(self, track: Track) -> None:
        self._recent_track_ids.append(track.id)
        self._recent_artists.append(normalize_artist_name(track.artist))

    def evaluate(
        self,
        _entry: QueueEntry,
        track: Track,
    ) -> SelectionDecision | None:
        if (
            self.track_window_size
            and track.id in tuple(self._recent_track_ids)[-self.track_window_size :]
        ):
            return SelectionDecision.reject(
                "TRACK_REPETITION",
                reason="Titel wurde innerhalb des Wiederholungsfensters bereits gespielt",
            )
        artist = normalize_artist_name(track.artist)
        if (
            self.artist_window_size
            and artist in tuple(self._recent_artists)[-self.artist_window_size :]
        ):
            return SelectionDecision.reject(
                "ARTIST_REPETITION",
                reason="Interpret wurde innerhalb des Wiederholungsfensters bereits gespielt",
            )
        return None
