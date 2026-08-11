"""Immutable overlay playback models and generation-safe state transitions."""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path


SUPPORTED_OVERLAY_FORMATS = frozenset({".mp3", ".flac"})


class OverlayStatus(StrEnum):
    """Complete runtime state of the independent overlay channel."""

    IDLE = "idle"
    PREPARING = "preparing"
    READY = "ready"
    FADING_IN = "fading_in"
    PLAYING = "playing"
    FADING_OUT = "fading_out"
    STOPPING = "stopping"
    FINISHED = "finished"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class OverlayDefinition:
    """Persistable settings captured when one playback is requested."""

    overlay_id: int
    name: str
    file_path: str
    category: str = ""
    volume_percent: int = 75
    fade_in_ms: int = 300
    fade_out_ms: int = 500
    cue_in_ms: int = 0
    cue_out_ms: int | None = None
    ducking_enabled: bool = True
    ducking_db: float = -8.0
    ducking_attack_ms: int = 200
    ducking_release_ms: int = 500


@dataclass(frozen=True, slots=True)
class ResolvedOverlayPlayback:
    """Validated, immutable values consumed by the audio channel."""

    definition: OverlayDefinition
    path: Path
    duration_ms: int
    cue_in_ms: int
    cue_out_ms: int
    fade_in_ms: int
    fade_out_ms: int
    volume: float


@dataclass(frozen=True, slots=True)
class OverlayRuntime:
    """Observable state snapshot published after every accepted transition."""

    status: OverlayStatus = OverlayStatus.IDLE
    generation: int = 0
    definition: OverlayDefinition | None = None
    playback: ResolvedOverlayPlayback | None = None
    position_ms: int | None = None
    error: str = ""


@dataclass(frozen=True, slots=True)
class OverlayRecord:
    """Stored overlay definition plus list and shortcut metadata."""

    definition: OverlayDefinition
    enabled: bool = True
    favorite_position: int | None = None
    keyboard_shortcut: str | None = None
    created_at: str = ""
    updated_at: str = ""


class OverlayPlayResult(StrEnum):
    COMPLETED = "COMPLETED"
    FADED_OUT = "FADED_OUT"
    STOPPED = "STOPPED"
    FAILED = "FAILED"


class OverlayStateMachine:
    """Own overlay state and reject callbacks from superseded operations."""

    def __init__(self) -> None:
        self._runtime = OverlayRuntime()

    @property
    def runtime(self) -> OverlayRuntime:
        return self._runtime

    def begin_prepare(self, definition: OverlayDefinition) -> int:
        """Start or replace an operation and return its generation token."""

        generation = self._runtime.generation + 1
        self._runtime = OverlayRuntime(
            status=OverlayStatus.PREPARING,
            generation=generation,
            definition=definition,
        )
        return generation

    def prepared(self, generation: int, playback: ResolvedOverlayPlayback) -> bool:
        if not self._accept(generation, OverlayStatus.PREPARING):
            return False
        if playback.definition != self._runtime.definition:
            return False
        self._runtime = replace(
            self._runtime,
            status=OverlayStatus.READY,
            playback=playback,
        )
        return True

    def start(self, generation: int) -> bool:
        if not self._accept(generation, OverlayStatus.READY):
            return False
        playback = self._runtime.playback
        assert playback is not None
        status = OverlayStatus.FADING_IN if playback.fade_in_ms else OverlayStatus.PLAYING
        self._runtime = replace(self._runtime, status=status, position_ms=playback.cue_in_ms)
        return True

    def fade_in_complete(self, generation: int) -> bool:
        return self._transition(generation, OverlayStatus.FADING_IN, OverlayStatus.PLAYING)

    def begin_fade_out(self) -> int | None:
        """Begin an idempotent manual/natural fade and invalidate old callbacks."""

        if self._runtime.status == OverlayStatus.FADING_OUT:
            return self._runtime.generation
        if self._runtime.status not in {
            OverlayStatus.READY,
            OverlayStatus.FADING_IN,
            OverlayStatus.PLAYING,
        }:
            return None
        generation = self._runtime.generation + 1
        self._runtime = replace(
            self._runtime,
            status=OverlayStatus.FADING_OUT,
            generation=generation,
        )
        return generation

    def begin_stop(self) -> int | None:
        """Cancel prepare/play/fade and enter the short safety-stop path."""

        if self._runtime.status in {OverlayStatus.IDLE, OverlayStatus.FINISHED}:
            return None
        if self._runtime.status == OverlayStatus.STOPPING:
            return self._runtime.generation
        generation = self._runtime.generation + 1
        self._runtime = replace(
            self._runtime,
            status=OverlayStatus.STOPPING,
            generation=generation,
        )
        return generation

    def finish(self, generation: int) -> bool:
        if generation != self._runtime.generation:
            return False
        if self._runtime.status not in {
            OverlayStatus.PLAYING,
            OverlayStatus.FADING_OUT,
            OverlayStatus.STOPPING,
        }:
            return False
        self._runtime = replace(self._runtime, status=OverlayStatus.FINISHED)
        return True

    def update_position(self, generation: int, position_ms: int) -> bool:
        """Publish a bounded position only for the current active playback."""

        if generation != self._runtime.generation or self._runtime.playback is None:
            return False
        if self._runtime.status not in {
            OverlayStatus.FADING_IN,
            OverlayStatus.PLAYING,
            OverlayStatus.FADING_OUT,
        }:
            return False
        playback = self._runtime.playback
        bounded = max(playback.cue_in_ms, min(position_ms, playback.cue_out_ms))
        if bounded == self._runtime.position_ms:
            return False
        self._runtime = replace(self._runtime, position_ms=bounded)
        return True

    def fail(self, generation: int, error: BaseException | str) -> bool:
        """Fail only the current overlay operation."""

        if generation != self._runtime.generation:
            return False
        self._runtime = replace(
            self._runtime,
            status=OverlayStatus.FAILED,
            error=str(error),
        )
        return True

    def reset(self) -> int:
        """Invalidate all callbacks and return to an empty channel."""

        generation = self._runtime.generation + 1
        self._runtime = OverlayRuntime(generation=generation)
        return generation

    def _accept(self, generation: int, status: OverlayStatus) -> bool:
        return generation == self._runtime.generation and self._runtime.status == status

    def _transition(
        self,
        generation: int,
        source: OverlayStatus,
        target: OverlayStatus,
    ) -> bool:
        if not self._accept(generation, source):
            return False
        self._runtime = replace(self._runtime, status=target)
        return True


def resolve_overlay(
    definition: OverlayDefinition,
    *,
    duration_ms: int,
    require_file: bool = True,
) -> ResolvedOverlayPlayback:
    """Validate a definition and clamp fades to the effective cue range."""

    path = Path(definition.file_path)
    if path.suffix.lower() not in SUPPORTED_OVERLAY_FORMATS:
        raise ValueError("Nicht unterstütztes Overlay-Audioformat")
    if require_file and not path.is_file():
        raise FileNotFoundError(f"Overlay-Datei nicht gefunden: {path}")
    if not definition.name.strip():
        raise ValueError("Overlay-Name darf nicht leer sein")
    if not 0 <= definition.volume_percent <= 100:
        raise ValueError("Overlay-Lautstärke muss zwischen 0 und 100 Prozent liegen")
    if duration_ms <= 0:
        raise ValueError("Overlay-Dauer muss größer als 0 sein")
    cue_in = max(0, definition.cue_in_ms)
    cue_out = duration_ms if definition.cue_out_ms is None else definition.cue_out_ms
    cue_out = min(duration_ms, cue_out)
    if cue_in >= cue_out:
        raise ValueError("Cue-In muss vor Cue-Out liegen")
    available = cue_out - cue_in
    fade_in = max(0, definition.fade_in_ms)
    fade_out = max(0, definition.fade_out_ms)
    if fade_in + fade_out > available:
        scale = available / (fade_in + fade_out)
        fade_in = round(fade_in * scale)
        fade_out = available - fade_in
    return ResolvedOverlayPlayback(
        definition=definition,
        path=path,
        duration_ms=duration_ms,
        cue_in_ms=cue_in,
        cue_out_ms=cue_out,
        fade_in_ms=fade_in,
        fade_out_ms=fade_out,
        volume=definition.volume_percent / 100.0,
    )
