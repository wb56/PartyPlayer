"""Composition layer for the transactional track editor."""

from dataclasses import dataclass
from collections.abc import Callable
from math import isfinite

from party_player.controllers.cue_point_controller import (
    CuePointController,
    CuePointEditorState,
)
from party_player.controllers.loudness_controller import LoudnessController, LoudnessEditorState
from party_player.models import Track
from party_player.performance_monitor import PerformanceMonitor


@dataclass(frozen=True, slots=True)
class TrackEditorViewModel:
    """Read-only data needed by the phase-A editor."""

    track_id: int
    title: str
    artist: str
    album: str
    original_release_year: int | None
    file_path: str
    duration_seconds: float | None
    cue: CuePointEditorState
    loudness: LoudnessEditorState | None = None
    equalizer_preset_key: str | None = None
    equalizer_preset_name: str | None = None
    equalizer_source: str | None = None

    @property
    def heading(self) -> str:
        return f"{self.artist} — {self.title}" if self.artist else self.title

    @property
    def analysis_state(self) -> str:
        """Classify the persisted automatic cue suggestion for presentation."""
        cue = self.cue
        automatic = (
            cue.automatic_cue_in,
            cue.automatic_cue_out,
            cue.automatic_fade_duration,
        )
        if all(value is None for value in automatic):
            return "NONE"
        if any(value is None for value in automatic):
            return "INCOMPLETE"
        manual = (
            cue.manual_cue_in,
            cue.manual_cue_out,
            cue.manual_fade_duration,
        )
        return "ADOPTED" if manual == automatic else "SUGGESTED"

    @property
    def effective_play_duration(self) -> float:
        return max(0.0, self.cue.resolved.cue_out - self.cue.resolved.cue_in)


@dataclass(frozen=True, slots=True)
class TrackEditorChanges:
    """Cue changes collected by the dialog until the user saves."""

    cue_in: float | None
    cue_out: float | None
    fade_duration: float | None
    discard_automatic: bool = False


class TrackEditorController:
    """Compose existing editor services without duplicating their domain rules."""

    def __init__(
        self,
        cue_controller: CuePointController,
        loudness_controller: LoudnessController | None = None,
        equalizer_state: Callable[[Track], tuple[str | None, str | None, str]] | None = None,
        performance_monitor: PerformanceMonitor | None = None,
    ) -> None:
        self._cue = cue_controller
        self._loudness = loudness_controller
        self._equalizer_state = equalizer_state
        self._performance = performance_monitor or PerformanceMonitor()

    def build_view_model(self, track: Track) -> TrackEditorViewModel:
        with self._performance.measure(
            "track_editor.build_view_model",
            warning_threshold_ms=100.0,
            context={"track_id": track.id},
        ):
            return self._build_view_model(track)

    def _build_view_model(self, track: Track) -> TrackEditorViewModel:
        equalizer_key: str | None = None
        equalizer_name: str | None = None
        equalizer_source: str | None = None
        if self._equalizer_state is not None:
            with self._performance.measure(
                "track_editor.equalizer_resolve",
                warning_threshold_ms=25.0,
                context={"track_id": track.id},
            ):
                equalizer_key, equalizer_name, equalizer_source = self._equalizer_state(track)
        return TrackEditorViewModel(
            track_id=track.id,
            title=track.title,
            artist=track.artist,
            album=track.album,
            original_release_year=track.original_release_year or track.year,
            file_path=track.file_path,
            duration_seconds=track.duration_seconds,
            cue=self._cue.state(track.id),
            loudness=self._loudness.state(track.id) if self._loudness is not None else None,
            equalizer_preset_key=equalizer_key,
            equalizer_preset_name=equalizer_name,
            equalizer_source=equalizer_source,
        )

    def validate_changes(
        self,
        view_model: TrackEditorViewModel,
        changes: TrackEditorChanges,
    ) -> TrackEditorChanges:
        cue_in = self._finite_or_none(changes.cue_in, "Cue In")
        cue_out = self._finite_or_none(changes.cue_out, "Cue Out")
        fade = self._finite_or_none(changes.fade_duration, "Überblenddauer")
        if cue_in is not None and cue_in < 0:
            raise ValueError("Cue In darf nicht negativ sein.")
        if cue_out is not None and cue_out < 0:
            raise ValueError("Cue Out darf nicht negativ sein.")
        duration = view_model.duration_seconds
        if duration is not None and cue_out is not None and cue_out > duration:
            raise ValueError("Cue Out darf nicht hinter dem Dateiende liegen.")
        effective_in = cue_in if cue_in is not None else view_model.cue.resolved.cue_in
        effective_out = cue_out if cue_out is not None else view_model.cue.resolved.cue_out
        if effective_out <= effective_in:
            raise ValueError("Cue Out muss hinter Cue In liegen.")
        if fade is not None:
            if fade < 0:
                raise ValueError("Die Überblenddauer darf nicht negativ sein.")
            if fade > effective_out - effective_in:
                raise ValueError(
                    "Die Überblenddauer darf nicht länger als der hörbare Titelbereich sein."
                )
        return TrackEditorChanges(cue_in, cue_out, fade, changes.discard_automatic)

    def save(
        self,
        view_model: TrackEditorViewModel,
        changes: TrackEditorChanges,
    ) -> TrackEditorViewModel:
        validated = self.validate_changes(view_model, changes)
        if not self.has_cue_changes(view_model, validated):
            return view_model
        cue = self._cue.save(
            view_model.track_id,
            validated.cue_in,
            validated.cue_out,
            validated.fade_duration,
        )
        return TrackEditorViewModel(
            track_id=view_model.track_id,
            title=view_model.title,
            artist=view_model.artist,
            album=view_model.album,
            original_release_year=view_model.original_release_year,
            file_path=view_model.file_path,
            duration_seconds=view_model.duration_seconds,
            cue=cue,
            loudness=view_model.loudness,
            equalizer_preset_key=view_model.equalizer_preset_key,
            equalizer_preset_name=view_model.equalizer_preset_name,
            equalizer_source=view_model.equalizer_source,
        )

    def save_async(
        self,
        view_model: TrackEditorViewModel,
        changes: TrackEditorChanges,
        completed: Callable[[TrackEditorViewModel], None],
        failed: Callable[[Exception], None],
    ) -> bool:
        """Validate immediately and persist changed cue values outside the GUI thread."""
        with self._performance.measure(
            "track_editor.save",
            warning_threshold_ms=25.0,
            context={"track_id": view_model.track_id},
        ):
            with self._performance.measure(
                "track_editor.save.validate",
                warning_threshold_ms=10.0,
                context={"track_id": view_model.track_id},
            ):
                validated = self.validate_changes(view_model, changes)
            with self._performance.measure(
                "track_editor.save.submit",
                warning_threshold_ms=10.0,
                context={"track_id": view_model.track_id},
            ):
                return self._submit_save(view_model, validated, completed, failed)

    def _submit_save(
        self,
        view_model: TrackEditorViewModel,
        validated: TrackEditorChanges,
        completed: Callable[[TrackEditorViewModel], None],
        failed: Callable[[Exception], None],
    ) -> bool:
        if not self.has_cue_changes(view_model, validated):
            completed(view_model)
            return False

        def cue_saved(cue: CuePointEditorState) -> None:
            completed(
                TrackEditorViewModel(
                    track_id=view_model.track_id,
                    title=view_model.title,
                    artist=view_model.artist,
                    album=view_model.album,
                    original_release_year=view_model.original_release_year,
                    file_path=view_model.file_path,
                    duration_seconds=view_model.duration_seconds,
                    cue=cue,
                    loudness=view_model.loudness,
                    equalizer_preset_key=view_model.equalizer_preset_key,
                    equalizer_preset_name=view_model.equalizer_preset_name,
                    equalizer_source=view_model.equalizer_source,
                )
            )

        self._cue.save_async(
            view_model.track_id,
            validated.cue_in,
            validated.cue_out,
            validated.fade_duration,
            cue_saved,
            failed,
            discard_automatic=validated.discard_automatic,
            changed_fields=self.changed_cue_fields(view_model, validated),
        )
        return True

    def record_event(self, operation: str) -> None:
        """Record a path-free editor lifecycle counter."""
        self._performance.record(operation, 1.0, float("inf"))

    def record_duration(
        self,
        operation: str,
        elapsed_ms: float,
        *,
        warning_threshold_ms: float = 50.0,
    ) -> None:
        """Record GUI work that is deliberately split across event-loop turns."""
        self._performance.record(operation, elapsed_ms, warning_threshold_ms)

    @staticmethod
    def has_cue_changes(
        view_model: TrackEditorViewModel,
        changes: TrackEditorChanges,
    ) -> bool:
        """Return whether the editable cue values differ from their loaded baseline."""
        cue = view_model.cue
        return (
            changes.cue_in != cue.manual_cue_in
            or changes.cue_out != cue.manual_cue_out
            or changes.fade_duration != cue.manual_fade_duration
            or changes.discard_automatic
            and view_model.analysis_state != "NONE"
        )

    @staticmethod
    def changed_cue_fields(
        view_model: TrackEditorViewModel,
        changes: TrackEditorChanges,
    ) -> frozenset[str]:
        """Return only manual columns whose values differ from the baseline."""
        cue = view_model.cue
        return frozenset(
            field
            for field, changed, original in (
                ("cue_in", changes.cue_in, cue.manual_cue_in),
                ("cue_out", changes.cue_out, cue.manual_cue_out),
                ("fade_duration", changes.fade_duration, cue.manual_fade_duration),
            )
            if changed != original
        )

    def automatic_suggestion(
        self,
        view_model: TrackEditorViewModel,
    ) -> TrackEditorChanges:
        """Return a stored automatic suggestion without persisting it as manual data."""
        cue = view_model.cue
        if (
            cue.automatic_cue_in is None
            or cue.automatic_cue_out is None
            or cue.automatic_fade_duration is None
        ):
            raise ValueError("Für diesen Titel liegt kein vollständiger Vorschlag vor.")
        return self.validate_changes(
            view_model,
            TrackEditorChanges(
                cue.automatic_cue_in,
                cue.automatic_cue_out,
                cue.automatic_fade_duration,
            ),
        )

    @staticmethod
    def parse_optional_seconds(raw: str, label: str) -> float | None:
        """Parse an optional German UI seconds value without inventing zero."""
        normalized = raw.strip().replace("−", "-")
        if not normalized:
            return None
        try:
            return float(normalized.replace(",", "."))
        except ValueError as exc:
            raise ValueError(f"{label} ist keine gültige Zahl. Beispiele: 12,5 oder 12.5.") from exc

    @staticmethod
    def _finite_or_none(value: float | None, label: str) -> float | None:
        if value is not None and not isfinite(value):
            raise ValueError(f"{label} muss eine endliche Zahl sein.")
        return value
