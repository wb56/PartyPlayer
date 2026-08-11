"""Playback history lifecycle independent from UI widgets."""

import logging
from dataclasses import dataclass
from datetime import datetime
from time import monotonic
from collections.abc import Callable
from threading import Lock
from uuid import uuid4

from party_player.enums import CompletionStatus, HistoryReasonCode
from party_player.models import Track
from party_player.repository import PartyPlayerRepository
from party_player.structured_logging import log_queue_event


@dataclass(slots=True)
class ActivePlayback:
    track: Track
    started_at: datetime
    queue_id: int | None
    running_since: float
    queue_source: str | None
    effective_cue_in: float | None = None
    effective_cue_out: float | None = None
    played_seconds: float = 0.0
    running: bool = True

    @property
    def effective_duration(self) -> float | None:
        if (
            self.effective_cue_in is not None
            and self.effective_cue_out is not None
            and self.effective_cue_out > self.effective_cue_in
        ):
            return self.effective_cue_out - self.effective_cue_in
        duration = self.track.duration_seconds
        return duration if duration is not None and duration > 0 else None


@dataclass(frozen=True, slots=True)
class HistoryPersistRequest:
    """Immutable, idempotently persistable completion detached from playback."""

    transition_id: str
    track_id: int
    deck_id: str
    started_at: datetime
    completed_at: datetime
    played_duration: float
    completion_reason: CompletionStatus
    queue_id: int | None
    queue_source: str | None
    effective_duration: float | None
    playback_ratio: float | None
    effective_cue_in: float | None
    effective_cue_out: float | None
    skip_reason: str | None = None
    skip_code: str | None = None
    error_message: str | None = None


class PlaybackHistoryService:
    """Track one active history lifecycle per deck."""

    def __init__(
        self,
        repository: PartyPlayerRepository,
        session_id: int,
        clock: Callable[[], float] = monotonic,
        played_ratio_threshold: float = 0.5,
        played_seconds_threshold: float = 120.0,
    ) -> None:
        self._repository = repository
        self._session_id = session_id
        self._active: dict[str, ActivePlayback] = {}
        self._clock = clock
        self._played_ratio_threshold = min(1.0, max(0.0, played_ratio_threshold))
        self._played_seconds_threshold = max(0.0, played_seconds_threshold)
        self._persisted_transition_ids: set[str] = set()
        self._persist_lock = Lock()
        self._logger = logging.getLogger(__name__)

    def start(
        self,
        deck_id: str,
        track: Track,
        queue_id: int | None = None,
        *,
        effective_cue_in: float | None = None,
        effective_cue_out: float | None = None,
    ) -> None:
        """Start tracking unless the same deck/track is already active."""
        current = self._active.get(deck_id)
        if current is not None and current.track.id == track.id:
            self.resume(deck_id)
            return
        if current is not None:
            self.finish(
                deck_id,
                CompletionStatus.STOPPED,
                0.0,
                skip_code=HistoryReasonCode.TRACK_REPLACED,
            )
        queue_entry = self._repository.get_queue_entry(queue_id) if queue_id is not None else None
        self._active[deck_id] = ActivePlayback(
            track,
            datetime.now(),
            queue_id,
            self._clock(),
            queue_entry.source.value if queue_entry is not None else None,
            effective_cue_in,
            effective_cue_out,
        )

    def pause(self, deck_id: str) -> None:
        active = self._active.get(deck_id)
        if active is not None and active.running:
            active.played_seconds += max(0.0, self._clock() - active.running_since)
            active.running = False

    def resume(self, deck_id: str) -> None:
        active = self._active.get(deck_id)
        if active is not None and not active.running:
            active.running_since = self._clock()
            active.running = True

    def finish(
        self,
        deck_id: str,
        status: CompletionStatus,
        play_duration: float,
        skip_reason: str | None = None,
        error_message: str | None = None,
        skip_code: HistoryReasonCode | str | None = None,
    ) -> bool:
        request = self.prepare_finish(
            deck_id,
            status,
            play_duration,
            skip_reason,
            error_message,
            skip_code=skip_code,
        )
        if request is None:
            return False
        self.persist(request)
        return True

    def prepare_finish(
        self,
        deck_id: str,
        status: CompletionStatus,
        play_duration: float,
        skip_reason: str | None = None,
        error_message: str | None = None,
        *,
        transition_id: str | None = None,
        skip_code: HistoryReasonCode | str | None = None,
    ) -> HistoryPersistRequest | None:
        """Finish the in-memory lifecycle without performing repository I/O."""
        active = self._active.pop(deck_id, None)
        if active is None:
            return None
        measured_duration = active.played_seconds
        if active.running:
            measured_duration += max(0.0, self._clock() - active.running_since)
        effective_duration = active.effective_duration
        playback_ratio = (
            min(1.0, measured_duration / effective_duration) if effective_duration else None
        )
        stable_skip_code = HistoryReasonCode(skip_code).value if skip_code is not None else None
        requested_status = status
        status = self._resolved_status(status, measured_duration, playback_ratio)
        if stable_skip_code is None and requested_status in {
            CompletionStatus.SKIPPED,
            CompletionStatus.ABORTED,
        }:
            stable_skip_code = HistoryReasonCode.UNSPECIFIED.value
        return HistoryPersistRequest(
            transition_id or str(uuid4()),
            active.track.id,
            deck_id,
            active.started_at,
            datetime.now(),
            measured_duration,
            status,
            active.queue_id,
            active.queue_source,
            effective_duration,
            playback_ratio,
            active.effective_cue_in,
            active.effective_cue_out,
            None,
            stable_skip_code,
            error_message,
        )

    def _resolved_status(
        self,
        requested: CompletionStatus,
        played_duration: float,
        playback_ratio: float | None,
    ) -> CompletionStatus:
        if requested not in {CompletionStatus.PLAYED, CompletionStatus.ABORTED}:
            return requested
        qualifies = (
            playback_ratio is not None and playback_ratio >= self._played_ratio_threshold
        ) or played_duration >= self._played_seconds_threshold
        if qualifies:
            return CompletionStatus.PLAYED
        if played_duration > 0:
            return CompletionStatus.PARTIALLY_PLAYED
        return CompletionStatus.ABORTED

    def persist(self, request: HistoryPersistRequest) -> bool:
        """Persist once per transition ID; failed writes remain retryable."""
        with self._persist_lock:
            if request.transition_id in self._persisted_transition_ids:
                return False
            # Persistence is intentionally serialized under this non-GUI lock so
            # concurrent retry callers cannot pass the idempotency check together.
            self._repository.add_history(
                self._session_id,
                request.track_id,
                request.deck_id,
                request.started_at,
                request.completion_reason,
                request.played_duration,
                request.queue_id,
                request.skip_reason,
                request.error_message,
                request.completed_at,
                request.effective_duration,
                request.playback_ratio,
                request.skip_code,
                request.effective_cue_in,
                request.effective_cue_out,
            )
            self._persisted_transition_ids.add(request.transition_id)
            log_queue_event(
                self._logger,
                "HISTORY_COMPLETED",
                session_id=self._session_id,
                queue_id=request.queue_id,
                track_id=request.track_id,
                source=request.queue_source,
                status=request.completion_reason.value,
                reason_code=request.skip_code or request.completion_reason.value,
            )
        return True

    def is_active(self, deck_id: str) -> bool:
        return deck_id in self._active
