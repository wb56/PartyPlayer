"""Independent deck playback state and operations."""

import logging
import math
from collections.abc import Callable
from pathlib import Path

from party_player.audio.base import AudioBackend, EqualizerBackend, RuntimeClipProtectionBackend
from party_player.enums import DeckState
from party_player.equalizer import ResolvedEqualizerPreset
from party_player.loudness import ResolvedLoudnessSettings
from party_player.models import Deck, Track

SUPPORTED_FORMATS = {".mp3", ".flac"}


class DeckController:
    def __init__(self, deck_id: str, backend: AudioBackend) -> None:
        if deck_id not in {"A", "B"}:
            raise ValueError("deck_id muss A oder B sein")
        self.model = Deck(deck_id=deck_id)
        self.backend = backend
        self.fade_level = 1.0
        self.normalization_factor = 1.0
        self.transition_muted = False
        self.emergency_muted = False
        self.is_fading = False
        self._fade_generation = 0
        self._normalization_generation = 0
        self._effective_volume: float | None = None
        self._volume_changed: Callable[[], None] | None = None
        self._command_result: Callable[[str, bool, str], None] | None = None
        self._logger = logging.getLogger(__name__)

    def set_volume_changed_callback(self, callback: Callable[[], None]) -> None:
        self._volume_changed = callback

    def set_command_result_callback(
        self, callback: Callable[[str, bool, str], None] | None
    ) -> None:
        """Report backend command outcomes without coupling the deck to recovery."""
        self._command_result = callback

    def _report_command(self, command: str, succeeded: bool, detail: str = "") -> None:
        if self._command_result is not None:
            self._command_result(command, succeeded, detail)

    def load(self, track: Track, *, validate_file: bool = True) -> None:
        path = Path(track.file_path)
        try:
            if path.suffix.lower() not in SUPPORTED_FORMATS:
                raise ValueError("Nicht unterstütztes Audioformat")
            if validate_file and not path.is_file():
                raise FileNotFoundError(f"Datei nicht gefunden: {path}")
            self.backend.load(path)
            self._finish_load(track)
            self._report_command("load", True)
        except Exception as exc:
            self._report_command("load", False, str(exc))
            self._set_error(exc)
            raise

    def prepare(self, track: Track) -> object:
        return self.backend.prepare(Path(track.file_path))

    def load_prepared(self, track: Track, prepared: object) -> None:
        try:
            self.backend.load_prepared(Path(track.file_path), prepared)
            self._finish_load(track)
            self._report_command("load_prepared", True)
        except Exception as exc:
            self._report_command("load_prepared", False, str(exc))
            self._set_error(exc)
            raise

    def play(self) -> None:
        if self.model.state == DeckState.FINISHED:
            self.backend.stop()
            self.model.position = 0.0
        self._perform(self.backend.play, DeckState.PLAYING, "Wiedergabestart angefordert")

    def pause(self) -> None:
        self._perform(self.backend.pause, DeckState.PAUSED, "Wiedergabe pausiert")

    def resume(self) -> None:
        self._perform(self.backend.resume, DeckState.PLAYING, "Wiedergabe fortgesetzt")

    def stop(self) -> None:
        self.cancel_fade()
        self.cancel_normalization_ramp(settle=True)
        self._perform(self.backend.stop, DeckState.STOPPED, "Wiedergabe gestoppt")
        self.model.position = 0.0

    def seek(self, position_seconds: float) -> None:
        self.backend.seek(position_seconds)
        self.model.position = max(0.0, min(position_seconds, self.model.duration))

    def set_volume(self, volume: float) -> None:
        self.model.volume = max(0.0, min(volume, 1.0))
        self._notify_volume_changed()

    def set_normalization_factor(self, factor: float) -> None:
        """Set resolved track gain independently from the user's deck volume."""
        self.cancel_normalization_ramp()
        self.normalization_factor = max(
            0.0, min(float(factor), self.backend.maximum_volume_factor())
        )
        self._notify_volume_changed()

    def set_resolved_loudness(self, settings: ResolvedLoudnessSettings | None) -> None:
        """Apply gain and retain the complete resolved state on this deck."""
        self._store_loudness_state(settings)
        self._configure_runtime_clip_protection(settings)
        self.set_normalization_factor(settings.linear_gain_factor if settings is not None else 1.0)

    def smooth_resolved_loudness(
        self,
        settings: ResolvedLoudnessSettings,
        duration: float,
        schedule: Callable[[int, Callable[[], None]], object],
    ) -> None:
        """Store a new target state and ramp its effective factor."""
        self._store_loudness_state(settings)
        self._configure_runtime_clip_protection(settings)
        self.smooth_normalization_factor(
            settings.linear_gain_factor,
            duration,
            schedule,
        )

    def _configure_runtime_clip_protection(
        self,
        settings: ResolvedLoudnessSettings | None,
    ) -> None:
        backend = self.backend
        if not isinstance(backend, RuntimeClipProtectionBackend):
            return
        if not backend.supports_runtime_clip_protection():
            return
        enabled = settings.runtime_clip_protection_enabled if settings is not None else False
        ceiling = settings.output_peak_ceiling_dbfs if settings is not None else 0.0
        if not backend.set_runtime_clip_protection(enabled, ceiling):
            self._logger.warning(
                "Audio-Backend von Deck %s hat den Laufzeit-Clip-Schutz abgelehnt",
                self.model.deck_id,
            )

    def set_transition_muted(self, muted: bool) -> None:
        """Gate one deck during automatic preparation without changing user volume."""
        self.transition_muted = bool(muted)
        self._notify_volume_changed()

    def set_emergency_muted(self, muted: bool) -> None:
        """Immediately gate this deck independently from all visible mixer state."""
        normalized = bool(muted)
        if normalized == self.emergency_muted:
            return
        if normalized:
            self.cancel_fade()
            self.cancel_normalization_ramp()
        self.emergency_muted = normalized
        if normalized:
            self.apply_effective_volume(0.0)
        self._notify_volume_changed()

    def smooth_normalization_factor(
        self,
        factor: float,
        duration: float,
        schedule: Callable[[int, Callable[[], None]], object],
    ) -> None:
        """Ramp a settings change without altering deck or crossfader controls."""
        self._normalization_generation += 1
        generation = self._normalization_generation
        start = self.normalization_factor
        target = max(0.0, min(float(factor), self.backend.maximum_volume_factor()))
        steps = max(1, round(max(0.05, duration) * 50))

        def tick(step: int = 1) -> None:
            if generation != self._normalization_generation:
                return
            progress = min(1.0, step / steps)
            self.normalization_factor = start + (target - start) * progress
            self._notify_volume_changed()
            if step < steps:

                def normalization_gain_tick() -> None:
                    tick(step + 1)

                schedule(20, normalization_gain_tick)

        schedule(20, tick)

    def cancel_normalization_ramp(self, *, settle: bool = False) -> None:
        """Invalidate scheduled gain ticks and optionally apply the resolved target."""
        self._normalization_generation += 1
        if settle:
            target = max(
                0.0,
                min(
                    10.0 ** (self.model.loudness_effective_gain_db / 20.0),
                    self.backend.maximum_volume_factor(),
                ),
            )
            if not math.isclose(self.normalization_factor, target, abs_tol=1e-9):
                self.normalization_factor = target
                self._notify_volume_changed()

    def apply_effective_volume(self, volume: float) -> None:
        normalized = max(0.0, min(volume, self.backend.maximum_volume_factor()))
        if self._effective_volume is not None and abs(normalized - self._effective_volume) < 0.001:
            return
        self.backend.set_volume(normalized)
        self._effective_volume = normalized

    @property
    def effective_volume(self) -> float:
        """Return the last volume value actually submitted to the backend."""
        return self._effective_volume if self._effective_volume is not None else 0.0

    def update_status(self) -> None:
        position = self.backend.get_position()
        state_reader = getattr(self.backend, "playback_state", None)
        playback_state = (
            str(state_reader()).upper()
            if callable(state_reader)
            else ("ENDED" if self.backend.is_finished() else "UNKNOWN")
        )
        self.model.backend_state = playback_state
        finished = playback_state == "ENDED"
        if position > 0 or not finished:
            self.model.position = position
        if self.model.duration <= 0:
            self.model.duration = self.backend.get_duration() or self.model.duration
        if self.model.state == DeckState.PLAYING and finished:
            self.model.position = self.model.duration
            self.model.state = DeckState.FINISHED

    def update_on_air(self, effective_volume: float) -> None:
        self.model.is_on_air = self.model.state == DeckState.PLAYING and effective_volume > 0

    def eject(self) -> None:
        cleanup = self.detach_for_cleanup()
        cleanup()

    def detach_for_cleanup(self) -> Callable[[], None]:
        """Reset logical deck state immediately and return backend cleanup work."""
        self.cancel_fade()
        self.cancel_normalization_ramp()
        volume = self.model.volume
        self.model = Deck(deck_id=self.model.deck_id, volume=volume)
        self.fade_level = 1.0
        self.normalization_factor = 1.0
        self._store_loudness_state(None)
        self.transition_muted = False
        self._notify_volume_changed()
        return self.backend.stop

    def start_fade(
        self,
        target: float,
        duration: float,
        schedule: Callable[[int, Callable[[], None]], object],
        *,
        stop_after: bool = False,
    ) -> None:
        duration = max(1.0, min(duration, 30.0))
        target = max(0.0, min(target, 1.0))
        self.cancel_fade()
        self.is_fading = True
        generation = self._fade_generation
        start = self.fade_level
        steps = max(1, round(duration * 20))

        def tick(step: int = 1) -> None:
            if generation != self._fade_generation:
                return
            self.fade_level = start + (target - start) * (step / steps)
            self._notify_volume_changed()
            if step < steps:

                def deck_fade_tick() -> None:
                    tick(step + 1)

                schedule(50, deck_fade_tick)
            else:
                self.is_fading = False
                if stop_after and target == 0:
                    self.stop()

        schedule(50, tick)

    def set_fade_level_immediately(self, level: float) -> None:
        """Set a bounded fade gate before starting a safety ramp."""
        self.cancel_fade()
        self.fade_level = max(0.0, min(float(level), 1.0))
        self._notify_volume_changed()

    def cancel_fade(self) -> None:
        self._fade_generation += 1
        self.is_fading = False

    def close(self) -> None:
        self.cancel_fade()
        self.cancel_normalization_ramp(settle=True)
        self.backend.close()

    def replace_backend(self, replacement: AudioBackend) -> None:
        """Replace only this deck's backend and reset its stale playback state."""
        previous = self.backend
        volume = self.model.volume
        self.cancel_fade()
        self.cancel_normalization_ramp()
        self.backend = replacement
        self.model = Deck(deck_id=self.model.deck_id, volume=volume)
        self.fade_level = 1.0
        self.normalization_factor = 1.0
        self.transition_muted = True
        self.emergency_muted = False
        self._effective_volume = None
        self._notify_volume_changed()
        try:
            previous.close()
        except Exception:
            self._logger.exception(
                "Deck %s: Ersetztes Audio-Backend konnte nicht sauber geschlossen werden",
                self.model.deck_id,
            )

    def commit_recovered_backend(
        self,
        replacement: AudioBackend,
        restored_model: Deck,
        *,
        normalization_factor: float,
    ) -> AudioBackend:
        """Atomically adopt a prevalidated muted backend and restored deck context."""
        previous = self.backend
        self.cancel_fade()
        self.cancel_normalization_ramp()
        self.backend = replacement
        self.model = restored_model
        self.fade_level = 1.0
        self.normalization_factor = max(
            0.0, min(normalization_factor, replacement.maximum_volume_factor())
        )
        self.transition_muted = True
        self.emergency_muted = False
        self._effective_volume = 0.0
        self._notify_volume_changed()
        return previous

    def equalizer_band_frequencies(self) -> tuple[float, ...]:
        if not isinstance(self.backend, EqualizerBackend):
            return ()
        return self.backend.equalizer_band_frequencies()

    def apply_equalizer(self, preset: ResolvedEqualizerPreset) -> bool:
        """Apply one deck-local snapshot without affecting volume or crossfade state."""
        if not isinstance(self.backend, EqualizerBackend):
            self._store_equalizer_state(
                ResolvedEqualizerPreset.disabled("UNSUPPORTED"),
                applied=False,
                error="Audio-Backend unterstützt keinen Equalizer",
            )
            return False
        try:
            changed = self.backend.apply_equalizer(preset)
        except Exception as exc:
            disabled = ResolvedEqualizerPreset.disabled("ERROR")
            try:
                self.backend.apply_equalizer(disabled)
            except Exception:
                self._logger.exception(
                    "Deck %s: Equalizer konnte nach Fehler nicht deaktiviert werden",
                    self.model.deck_id,
                )
            self._store_equalizer_state(disabled, applied=False, error=str(exc))
            self._logger.warning(
                "equalizer.apply_failed deck=%s error=%s",
                self.model.deck_id,
                exc,
            )
            return False
        self._store_equalizer_state(preset, applied=preset.enabled)
        self._logger.info(
            "%s deck=%s preset=%s source=%s changed=%s",
            "equalizer.apply" if preset.enabled else "equalizer.disable",
            self.model.deck_id,
            preset.name,
            preset.source,
            changed,
        )
        return changed

    def discard_prepared(self, prepared: object) -> None:
        """Explicitly release a worker-prepared medium that was not adopted."""
        self.backend.release_prepared(prepared)

    def _perform(self, operation: Callable[[], None], state: DeckState, message: str) -> None:
        if self.model.loaded_track is None:
            raise RuntimeError("Kein Titel geladen")
        try:
            operation()
            self._report_command(operation.__name__, True)
            self.model.state = state
            self.model.error_message = ""
            self._logger.info("Deck %s: %s", self.model.deck_id, message)
            self._notify_volume_changed()
        except Exception as exc:
            self._report_command(operation.__name__, False, str(exc))
            self._set_error(exc)
            raise

    def _finish_load(self, track: Track) -> None:
        self.cancel_normalization_ramp()
        self.model.loaded_track = track
        self.model.cue_boundaries_ready = False
        self.model.duration = self.backend.get_duration() or track.duration_seconds or 0.0
        self.model.position = 0.0
        self.model.error_message = ""
        self.model.state = DeckState.LOADED
        self.fade_level = 1.0
        self.normalization_factor = 1.0
        self._store_loudness_state(None)
        self.transition_muted = False
        self._logger.info(
            "Deck %s: Titel geladen: %s - %s",
            self.model.deck_id,
            track.artist,
            track.title,
        )

    def _store_equalizer_state(
        self,
        preset: ResolvedEqualizerPreset,
        *,
        applied: bool,
        error: str = "",
    ) -> None:
        self.model.equalizer_preset_name = preset.name
        self.model.equalizer_source = preset.source
        self.model.equalizer_preamp_db = preset.preamp_db
        self.model.equalizer_band_count = len(preset.band_gains_db)
        self.model.equalizer_applied = applied
        self.model.equalizer_error = error

    def _store_loudness_state(self, settings: ResolvedLoudnessSettings | None) -> None:
        self.model.loudness_requested_gain_db = (
            settings.requested_gain_db if settings is not None else 0.0
        )
        self.model.loudness_effective_gain_db = (
            settings.effective_gain_db if settings is not None else 0.0
        )
        self.model.loudness_source = settings.source if settings is not None else "NONE"
        self.model.loudness_peak_limited = settings.peak_limited if settings is not None else False

    def _set_error(self, error: Exception) -> None:
        self.cancel_normalization_ramp(settle=True)
        self.model.state = DeckState.ERROR
        self.model.error_message = str(error)
        self._logger.exception("Deck %s: Audiofehler: %s", self.model.deck_id, error)

    def _notify_volume_changed(self) -> None:
        if self._volume_changed is not None:
            self._volume_changed()
