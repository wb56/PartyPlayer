"""Silent application dialogs that never trigger operating-system alert sounds."""

from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from time import monotonic
from tkinter import TclError
from typing import Any, Literal

import customtkinter as ctk  # type: ignore[import-untyped]
from party_player.ui.responsive_dialog import (
    apply_responsive_dialog_geometry,
    bind_dialog_escape,
    release_dialog,
)

from party_player.analysis.loudness_service import LoudnessAnalysisJob
from party_player.controllers.cue_point_controller import CuePointController, CuePointEditorState
from party_player.controllers.loudness_controller import LoudnessController
from party_player.controllers.track_editor_controller import (
    TrackEditorChanges,
    TrackEditorController,
    TrackEditorViewModel,
)
from party_player.models import Track
from party_player.metadata_editor import (
    FIELD_LABELS,
    MetadataSaveResult,
    StagedSuggestionAction,
    SuggestionEditorAction,
    TrackMetadataChanges,
    TrackMetadataEditorViewModel,
    ValueRemovalMode,
)
from party_player.metadata_persistence import MetadataRevisionConflict
from party_player.metadata_rules import MetadataFieldKey, RecordingClassification, RecordingKind
from party_player.ui.tooltip import Tooltip


DialogKind = Literal["info", "error", "yes_no", "yes_no_cancel"]


def _parse_gain_db(raw: str) -> float:
    normalized = raw.strip().replace("−", "-")
    if normalized.casefold().endswith("db"):
        normalized = normalized[:-2].strip()
    if not normalized:
        raise ValueError("Bitte einen Gain-Wert eingeben.")
    try:
        return float(normalized.replace(",", "."))
    except ValueError as exc:
        raise ValueError("Ungültiger Gain-Wert. Beispiele: −4, −4 dB oder −4,5 dB.") from exc


class LoudnessDialog(ctk.CTkToplevel):  # type: ignore[misc]
    """Edit or reset a manual per-track gain without a native system dialog."""

    def __init__(self, parent: Any, controller: LoudnessController, track_id: int) -> None:
        super().__init__(parent)
        self._controller = controller
        self._track_id = track_id
        state = controller.state(track_id)
        self.title("Lautstärkeanpassung")
        self.geometry("560x390")
        self.resizable(False, False)
        self.transient(parent)
        self.protocol("WM_DELETE_WINDOW", self._cancel)
        self.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(self, text=state.title, font=("Segoe UI", 18, "bold"), anchor="w").grid(
            row=0, column=0, padx=24, pady=(24, 12), sticky="ew"
        )
        self._status = ctk.CTkLabel(
            self,
            text=(
                f"Aktive Quelle: {state.source_text}\n"
                f"Metadatenstatus: {state.metadata_status_text}\n"
                f"Effektiver Gain: {state.resolved.effective_gain_db:+.2f} dB\n"
                f"{state.clip_protection_text}"
            ),
            justify="left",
            anchor="w",
            text_color="#b8c7d9",
        )
        self._status.grid(row=1, column=0, padx=24, pady=(0, 18), sticky="ew")
        self._analysis_job: LoudnessAnalysisJob | None = None
        ctk.CTkLabel(
            self,
            text="Manuelle Korrektur von −12 bis +12 dB:",
            anchor="w",
        ).grid(row=2, column=0, padx=24, pady=(0, 6), sticky="ew")
        self._gain = ctk.CTkEntry(self, placeholder_text="z. B. −2,50")
        self._gain.grid(row=3, column=0, padx=24, pady=(0, 6), sticky="ew")
        if state.manual_gain_db is not None:
            self._gain.insert(0, f"{state.manual_gain_db:.2f}")
        self._error = ctk.CTkLabel(
            self,
            text="",
            anchor="w",
            text_color="#ff8585",
            wraplength=500,
        )
        self._error.grid(row=4, column=0, padx=24, pady=(2, 14), sticky="ew")

        buttons = ctk.CTkFrame(self, fg_color="transparent")
        buttons.grid(row=5, column=0, padx=24, pady=(8, 24), sticky="e")
        ctk.CTkButton(
            buttons,
            text="Abbrechen",
            fg_color="#555555",
            command=self._cancel,
        ).pack(side="right", padx=(6, 0))
        ctk.CTkButton(
            buttons,
            text="Speichern",
            fg_color="#2f7d4f",
            command=self._save,
        ).pack(side="right", padx=6)
        ctk.CTkButton(
            buttons,
            text="Zurücksetzen",
            fg_color="#7d3030",
            command=self._reset,
        ).pack(side="right", padx=(0, 6))
        self._analysis_button = ctk.CTkButton(
            buttons,
            text="EBU R128 analysieren",
            command=self._analyze,
        )
        self._analysis_button.pack(side="right", padx=(0, 6))
        analysis_available, analysis_message = controller.analysis_availability()
        if not analysis_available:
            self._analysis_button.configure(state="disabled")
            self._error.configure(text=analysis_message)
            Tooltip(self._analysis_button, analysis_message)

        self.bind("<Return>", lambda _event: self._save())
        self.bind("<Escape>", lambda _event: self._cancel())
        self.grab_set()
        self._gain.focus_set()

    def _save(self) -> None:
        try:
            raw = self._gain.get().strip()
            if not raw:
                raise ValueError("Bitte einen Gain-Wert eingeben oder „Zurücksetzen“ wählen.")
            gain = _parse_gain_db(raw)
            self._controller.save_manual_gain(self._track_id, gain)
        except ValueError as exc:
            self._error.configure(text=str(exc))
            return
        self.destroy()

    def _reset(self) -> None:
        try:
            self._controller.save_manual_gain(self._track_id, None)
        except ValueError as exc:
            self._error.configure(text=str(exc))
            return
        self.destroy()

    def _analyze(self) -> None:
        self._analysis_button.configure(state="disabled")
        self._status.configure(text="EBU-R128-Analyse läuft im Hintergrund …")
        try:
            self._analysis_job = self._controller.analyze_track(
                self._track_id,
                self._analysis_completed,
            )
        except (ValueError, RuntimeError) as exc:
            self._analysis_button.configure(state="normal")
            self._error.configure(text=str(exc))

    def _analysis_completed(self, _result: object, error: str | None) -> None:
        if not self.winfo_exists():
            return
        self._analysis_job = None
        self._analysis_button.configure(state="normal")
        if error is not None:
            self._error.configure(text=error)
            self._status.configure(text="EBU-R128-Analyse fehlgeschlagen.")
            return
        state = self._controller.state(self._track_id)
        self._error.configure(text="")
        self._status.configure(
            text=(
                "EBU-R128-Analyse abgeschlossen.\n"
                f"Aktive Quelle: {state.source_text}\n"
                f"Effektiver Gain: {state.resolved.effective_gain_db:+.2f} dB\n"
                f"{state.clip_protection_text}"
            )
        )

    def _cancel(self) -> None:
        analysis_job = getattr(self, "_analysis_job", None)
        if analysis_job is not None:
            analysis_job.cancel()
        self.destroy()


class NormalizationSettingsDialog(ctk.CTkToplevel):  # type: ignore[misc]
    """Edit persistent normalization and safety settings in one transaction."""

    def __init__(self, parent: Any, controller: LoudnessController) -> None:
        super().__init__(parent)
        self._controller = controller
        state = controller.settings_state()
        self.title("Normalisierung einstellen")
        self.geometry("650x620")
        self.minsize(620, 590)
        self.transient(parent)
        self.protocol("WM_DELETE_WINDOW", self._cancel)
        self.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(
            self,
            text="Lautheitsnormalisierung",
            font=("Segoe UI", 20, "bold"),
        ).grid(row=0, column=0, columnspan=2, padx=24, pady=(24, 16), sticky="w")
        self._enabled = ctk.CTkSwitch(self, text="Normalisierung aktiv")
        self._enabled.grid(row=1, column=0, columnspan=2, padx=24, pady=8, sticky="w")
        if state.enabled:
            self._enabled.select()
        self._clip_protection = ctk.CTkSwitch(self, text="Clip-Schutz aktiv")
        self._clip_protection.grid(row=1, column=1, padx=24, pady=8, sticky="e")
        if state.clip_protection_enabled:
            self._clip_protection.select()
        self._mode_labels = {"Titel": "TRACK", "Album": "ALBUM", "Aus": "OFF"}
        self._mode = ctk.CTkOptionMenu(self, values=list(self._mode_labels))
        self._mode.set(
            next(label for label, mode in self._mode_labels.items() if mode == state.mode)
        )
        self._mode.grid(row=2, column=1, padx=24, pady=8, sticky="ew")
        ctk.CTkLabel(self, text="Modus:", anchor="w").grid(
            row=2, column=0, padx=24, pady=8, sticky="w"
        )
        self._target_loudness = self._field(3, "Ziel-Lautheit (LUFS)", state.target_loudness_lufs)
        self._maximum_positive = self._field(
            4, "Maximale positive Verstärkung (dB)", state.maximum_positive_gain_db
        )
        self._maximum_negative = self._field(
            5, "Maximale negative Verstärkung (dB)", state.maximum_negative_gain_db
        )
        self._maximum_peak = self._field(
            6, "Absolute Peak-Grenze (dBFS)", state.maximum_output_peak_db
        )
        self._headroom = self._field(7, "Zusätzlicher Headroom (dB)", state.headroom_db)
        self._fallback = self._field(
            8, "Positiver Fallback ohne Peak (dB)", state.fallback_positive_gain_db
        )
        self._smoothing = self._field(9, "Gain-Glättung (Sekunden)", state.smoothing_seconds)
        ctk.CTkLabel(
            self,
            text=(
                "Sicherer Ausgangspegel = absolute Peak-Grenze minus Headroom. "
                "Änderungen an geladenen Titeln werden weich eingeblendet."
            ),
            justify="left",
            wraplength=590,
            text_color="#9fb3c8",
        ).grid(row=10, column=0, columnspan=2, padx=24, pady=(10, 4), sticky="w")
        self._error = ctk.CTkLabel(self, text="", text_color="#ff8585", wraplength=590, anchor="w")
        self._error.grid(row=11, column=0, columnspan=2, padx=24, pady=6, sticky="ew")
        buttons = ctk.CTkFrame(self, fg_color="transparent")
        buttons.grid(row=12, column=0, columnspan=2, padx=24, pady=(10, 24), sticky="e")
        ctk.CTkButton(buttons, text="Abbrechen", fg_color="#555555", command=self._cancel).pack(
            side="right", padx=(6, 0)
        )
        ctk.CTkButton(buttons, text="Speichern", fg_color="#2f7d4f", command=self._save).pack(
            side="right", padx=6
        )
        self.bind("<Return>", lambda _event: self._save())
        self.bind("<Escape>", lambda _event: self._cancel())
        self.grab_set()

    def _field(self, row: int, label: str, value: float) -> ctk.CTkEntry:
        ctk.CTkLabel(self, text=f"{label}:", anchor="w").grid(
            row=row, column=0, padx=24, pady=7, sticky="w"
        )
        entry = ctk.CTkEntry(self)
        entry.insert(0, f"{value:.2f}")
        entry.grid(row=row, column=1, padx=24, pady=7, sticky="ew")
        return entry

    @staticmethod
    def _number(entry: Any) -> float:
        return float(entry.get().strip().replace(",", "."))

    def _save(self) -> None:
        try:
            self._controller.update_normalization_settings(
                enabled=bool(self._enabled.get()),
                clip_protection_enabled=bool(self._clip_protection.get()),
                mode=self._mode_labels[self._mode.get()],
                target_loudness_lufs=self._number(self._target_loudness),
                maximum_positive_gain_db=self._number(self._maximum_positive),
                maximum_negative_gain_db=self._number(self._maximum_negative),
                maximum_output_peak_db=self._number(self._maximum_peak),
                headroom_db=self._number(self._headroom),
                fallback_positive_gain_db=self._number(self._fallback),
                smoothing_seconds=self._number(self._smoothing),
            )
        except ValueError as exc:
            self._error.configure(text=str(exc))
            return
        self.destroy()

    def _cancel(self) -> None:
        self.destroy()


class CuePointDialog(ctk.CTkToplevel):  # type: ignore[misc]
    """Phase-A track editor retaining the complete transactional cue workflow."""

    def __init__(
        self,
        parent: Any,
        controller: CuePointController,
        track_id: int,
        *,
        track: Track,
        loudness_controller: LoudnessController | None = None,
        equalizer_state: Callable[[Track], tuple[str | None, str | None, str]] | None = None,
        editor_controller: TrackEditorController | None = None,
        view_model: TrackEditorViewModel | None = None,
        on_saved: Callable[[TrackEditorViewModel], None] | None = None,
        on_closed: Callable[[], None] | None = None,
    ) -> None:
        super().__init__(parent)
        self._controller = controller
        self._track_id = track_id
        self._on_saved = on_saved
        self._on_closed = on_closed
        self._closed = False
        self._saving = False
        self._save_had_changes = False
        self._discard_automatic = False
        self._editor_controller = editor_controller or TrackEditorController(
            controller, loudness_controller, equalizer_state
        )
        self._view_model = view_model or self._editor_controller.build_view_model(track)
        self._path_tooltip: Tooltip | None = None
        self._build_after_id: str | None = None
        self._lazy_tabs_built: set[str] = {"Cue"}
        self._metadata_loading = False
        self._metadata_entries: dict[MetadataFieldKey, Any] = {}
        self._metadata_status_labels: dict[MetadataFieldKey, Any] = {}
        self._metadata_confirmations: set[MetadataFieldKey] = set()
        self._metadata_removals: dict[MetadataFieldKey, ValueRemovalMode] = {}
        self._metadata_suggestion_actions: dict[int, StagedSuggestionAction] = {}
        self._pending_metadata_changes: TrackMetadataChanges | None = None
        self._metadata_tooltips: list[Tooltip] = []
        self.title("Titel bearbeiten")
        apply_responsive_dialog_geometry(
            self, parent, preferred_size=(780, 760), minimum_size=(620, 460)
        )
        self.transient(parent)
        self.protocol("WM_DELETE_WINDOW", self._cancel)
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)
        self._editor_content = ctk.CTkScrollableFrame(
            self,
            fg_color="transparent",
            height=1,
        )
        self._editor_content.grid(row=1, column=0, sticky="nsew")
        self._editor_content.grid_columnconfigure(0, weight=1)
        self.grab_set()
        bind_dialog_escape(self, self._cancel)
        self._build_steps: list[Callable[[], None]] = [
            self._build_header,
            self._build_tab_container,
            *(
                lambda name=name: self._tabs.add(name)
                for name in ("Cue", "Lautheit", "Equalizer", "Jingles", "Metadaten")
            ),
            self._build_cue_fields,
            self._build_reset_buttons,
            self._build_sources,
            self._build_preview_controls,
            self._build_analysis_controls,
            self._build_analysis_details,
            self._build_footer,
        ]
        self._editor_controller.record_event("track_editor.open")
        self._schedule_next_build_step()

    def _build_header(self) -> None:
        state = self._view_model.cue
        header = ctk.CTkFrame(self._editor_content, fg_color="transparent")
        header.grid(row=0, column=0, padx=20, pady=(18, 8), sticky="ew")
        ctk.CTkLabel(header, text=state.title, font=("Segoe UI", 18, "bold")).pack(anchor="w")
        album = self._view_model.album or "Album nicht angegeben"
        year = self._view_model.original_release_year
        ctk.CTkLabel(
            header,
            text=f"{album} · {year if year is not None else 'Jahr nicht angegeben'}",
            text_color="#9aa4b2",
        ).pack(anchor="w", pady=(2, 0))
        path_label = ctk.CTkLabel(
            header,
            text=f"Datei: {Path(self._view_model.file_path).name}",
            text_color="#7f8b99",
        )
        path_label.pack(anchor="w", pady=(2, 0))
        self._path_tooltip = Tooltip(path_label, self._view_model.file_path)

    def _build_tab_container(self) -> None:
        self._tabs = ctk.CTkTabview(self._editor_content, command=self._tab_changed, height=560)
        self._tabs.grid(row=1, column=0, padx=16, pady=(0, 8), sticky="ew")

    def _build_cue_fields(self) -> None:
        state = self._view_model.cue
        cue = self._tabs.tab("Cue")
        cue.grid_columnconfigure(1, weight=1)
        self._cue_parent = cue
        self._cue_in = self._field(
            1,
            "Startpunkt (Cue In, leer = Dateianfang)",
            state.manual_cue_in,
            "Aktuelle Position als Startpunkt",
        )
        self._cue_out = self._field(
            2,
            "Endpunkt/unhörbar (Cue Out, leer = Dateiende)",
            state.manual_cue_out,
            "Aktuelle Position als Endpunkt",
        )
        self._fade = self._field(3, "Überblenddauer", state.manual_fade_duration, None)

    def _build_reset_buttons(self) -> None:
        cue = self._cue_parent
        ctk.CTkButton(
            cue, text="Startpunkt zurücksetzen", command=lambda: self._clear(self._cue_in)
        ).grid(row=4, column=0, padx=20, pady=5, sticky="ew")
        ctk.CTkButton(
            cue, text="Endpunkt zurücksetzen", command=lambda: self._clear(self._cue_out)
        ).grid(row=4, column=1, padx=8, pady=5, sticky="ew")
        ctk.CTkButton(
            cue, text="Überblenddauer zurücksetzen", command=lambda: self._clear(self._fade)
        ).grid(row=4, column=2, padx=20, pady=5, sticky="ew")
        ctk.CTkButton(
            cue,
            text="Sichere Standardwerte einsetzen",
            fg_color="#8a6d1f",
            command=self._use_safe_defaults,
        ).grid(row=5, column=0, columnspan=3, padx=20, pady=5, sticky="ew")

    def _build_sources(self) -> None:
        cue = self._cue_parent
        self._sources = ctk.CTkLabel(cue, text="", justify="left", text_color="#b8c7d9")
        self._sources.grid(row=6, column=0, columnspan=3, padx=20, pady=(14, 4), sticky="w")
        self._show_sources(self._view_model.cue)

    def _build_preview_controls(self) -> None:
        cue = self._cue_parent
        preview_buttons = ctk.CTkFrame(cue, fg_color="transparent")
        preview_buttons.grid(row=7, column=0, columnspan=3, padx=20, pady=(8, 2), sticky="ew")
        ctk.CTkButton(
            preview_buttons,
            text="Ab Startpunkt vorhören",
            command=lambda: self._preview("in"),
        ).pack(side="left", padx=(0, 6))
        ctk.CTkButton(
            preview_buttons,
            text="Fade-Out testen",
            command=lambda: self._preview("out"),
        ).pack(side="left", padx=6)
        ctk.CTkButton(
            preview_buttons,
            text="Vorhören stoppen",
            fg_color="#7d3030",
            command=self._stop_preview,
        ).pack(side="left", padx=6)
        self._preview_status = ctk.CTkLabel(
            cue,
            text="Eigener Vorhörmodus – Queue und History bleiben unverändert",
            text_color="#999999",
        )
        self._preview_status.grid(row=8, column=0, columnspan=3, padx=20, pady=3, sticky="w")

    def _build_analysis_controls(self) -> None:
        cue = self._cue_parent
        self._analysis_button = ctk.CTkButton(
            cue,
            text="Automatisch analysieren",
            command=self._analyze,
        )
        self._analysis_button.grid(row=9, column=0, padx=20, pady=4, sticky="w")
        self._analysis_status = ctk.CTkLabel(
            cue,
            text="",
            text_color="#b8c7d9",
        )
        self._analysis_status.grid(row=9, column=1, columnspan=2, padx=8, pady=4, sticky="w")
        analysis_available, analysis_message = self._controller.analysis_availability()
        self._analysis_status.configure(
            text=analysis_message,
            text_color="#8fd9a8" if analysis_available else "#ff8585",
        )
        if not analysis_available:
            self._analysis_button.configure(state="disabled")

    def _build_analysis_details(self) -> None:
        cue = self._cue_parent
        self._analysis_details = ctk.CTkLabel(
            cue,
            text="",
            justify="left",
            text_color="#9fb3c8",
            wraplength=620,
        )
        self._analysis_details.grid(
            row=10, column=0, columnspan=3, padx=20, pady=(2, 4), sticky="w"
        )
        self._show_analysis_details(self._view_model)
        analysis_actions = ctk.CTkFrame(cue, fg_color="transparent")
        analysis_actions.grid(row=11, column=0, columnspan=3, padx=20, pady=(2, 4), sticky="w")
        ctk.CTkButton(
            analysis_actions,
            text="Vorschlag übernehmen",
            command=self._adopt_analysis,
        ).pack(side="left", padx=(0, 6))
        ctk.CTkButton(
            analysis_actions,
            text="Vorschlag verwerfen",
            fg_color="#7d3030",
            command=self._discard_analysis,
        ).pack(side="left", padx=6)
        self._error = ctk.CTkLabel(cue, text="", text_color="#ff8585", wraplength=650)
        self._error.grid(row=12, column=0, columnspan=3, padx=20, pady=4, sticky="w")

    def _build_footer(self) -> None:
        buttons = ctk.CTkFrame(self, fg_color="transparent")
        buttons.grid(row=2, column=0, padx=20, pady=(4, 18), sticky="e")
        ctk.CTkButton(buttons, text="Abbrechen", fg_color="#555555", command=self._cancel).pack(
            side="right", padx=5
        )
        self._save_button = ctk.CTkButton(
            buttons, text="Speichern", fg_color="#2f7d4f", command=self._save
        )
        self._save_button.pack(side="right", padx=5)
        self.focus_force()
        self._editor_controller.record_event("track_editor_open_total")

    def _schedule_next_build_step(self) -> None:
        if self._closed or not self._build_steps:
            self._build_after_id = None
            return
        self._build_after_id = self.after(1, self._run_build_step)

    def _run_build_step(self) -> None:
        self._build_after_id = None
        if not self._is_active() or not self._build_steps:
            return
        step = self._build_steps.pop(0)
        started = monotonic()
        step()
        self._editor_controller.record_duration(
            "track_editor.build_chunk",
            (monotonic() - started) * 1000.0,
        )
        self._schedule_next_build_step()

    def _tab_changed(self) -> None:
        name = self._tabs.get()
        if name in self._lazy_tabs_built or name == "Cue":
            return
        if name == "Metadaten":
            self._lazy_tabs_built.add(name)
            self._build_metadata_tab()
            return
        descriptions = {
            "Lautheit": "Lautheitsanalyse folgt in Phase B.",
            "Equalizer": "Titelbezogene Presetzuweisung folgt in Phase B.",
            "Jingles": "Jingle-Zuweisungen folgen in Phase C.",
        }
        description = descriptions.get(name)
        if description is None:
            return
        ctk.CTkLabel(
            self._tabs.tab(name),
            text=f"{name}\n\n{description}\n\nDer Bereich ist derzeit schreibgeschützt.",
            text_color="#9aa4b2",
            font=("Segoe UI", 15, "bold"),
        ).pack(expand=True, padx=24, pady=40)
        self._lazy_tabs_built.add(name)

    def _build_metadata_tab(self) -> None:
        tab = self._tabs.tab("Metadaten")
        tab.grid_columnconfigure(0, weight=1)
        self._metadata_container = ctk.CTkScrollableFrame(tab, height=470)
        self._metadata_container.grid(row=0, column=0, sticky="nsew", padx=4, pady=4)
        self._metadata_container.grid_columnconfigure(0, weight=1)
        self._metadata_loading_label = ctk.CTkLabel(
            self._metadata_container,
            text="Metadaten und Vorschläge werden geladen …",
            text_color="#9aa4b2",
        )
        self._metadata_loading_label.grid(row=0, column=0, padx=20, pady=40)
        self._metadata_loading = True
        accepted = self._editor_controller.load_metadata_async(
            self._track_id,
            self._metadata_loaded,
            self._metadata_load_failed,
        )
        if not accepted:
            self._metadata_load_failed(
                RuntimeError("Metadatenauftrag konnte nicht gestartet werden")
            )

    def _metadata_loaded(self, model: TrackMetadataEditorViewModel) -> None:
        if not self._is_active():
            return
        self._metadata_loading = False
        self._view_model = self._editor_controller.with_metadata(self._view_model, model)
        self._clear_metadata_container()
        self._render_metadata_fields(model)
        self._render_metadata_suggestions(model)

    def _metadata_load_failed(self, error: Exception) -> None:
        if not self._is_active():
            return
        self._metadata_loading = False
        if hasattr(self, "_metadata_loading_label"):
            self._metadata_loading_label.configure(
                text=f"Metadaten konnten nicht geladen werden: {error}",
                text_color="#ff8585",
            )

    def _clear_metadata_container(self) -> None:
        for tooltip in self._metadata_tooltips:
            tooltip.close()
        self._metadata_tooltips.clear()
        for child in self._metadata_container.winfo_children():
            child.destroy()
        self._metadata_entries.clear()
        self._metadata_status_labels.clear()

    def _render_metadata_fields(self, model: TrackMetadataEditorViewModel) -> None:
        groups: tuple[tuple[str, tuple[tuple[MetadataFieldKey, str, str], ...]], ...] = (
            (
                "Grunddaten",
                (
                    (MetadataFieldKey.TITLE, "Titel", "Katalogtitel der konkreten Aufnahme"),
                    (MetadataFieldKey.ARTIST, "Interpret", "Hauptinterpret"),
                    (MetadataFieldKey.ALBUM, "Album", "Album, CD oder Zusammenstellung"),
                    (MetadataFieldKey.MAIN_GENRE, "Hauptgenre", "Primäre Genrezuordnung"),
                    (MetadataFieldKey.YEAR, "Ausgabejahr", "Jahr der CD, Compilation oder Edition"),
                    (
                        MetadataFieldKey.ORIGINAL_RELEASE_YEAR,
                        "Ursprüngliches Erscheinungsjahr",
                        "Erste Veröffentlichung dieser konkreten Aufnahme",
                    ),
                ),
            ),
            (
                "Aufnahme und Tempo",
                (
                    (MetadataFieldKey.BPM, "Wirksame BPM", "Fachlich wirksamer Tempowert"),
                    (
                        MetadataFieldKey.ALTERNATIVE_BPM,
                        "Alternative BPM",
                        "Optionaler alternativer Tempowert",
                    ),
                    (
                        MetadataFieldKey.BPM_CONFIDENCE,
                        "BPM-Konfidenz",
                        "Technischer Analysewert von 0 bis 1; schreibgeschützt",
                    ),
                ),
            ),
            (
                "Musikalische Einordnung",
                (
                    (MetadataFieldKey.ENERGY, "Energie", "Redaktioneller Wert von 0 bis 100"),
                    (
                        MetadataFieldKey.DANCEABILITY,
                        "Tanzbarkeit",
                        "Redaktioneller Wert von 0 bis 100",
                    ),
                    (MetadataFieldKey.LANGUAGE, "Sprache", "Sprache des Titels"),
                    (MetadataFieldKey.RATING, "Bewertung", "Persönliche Bewertung von 1 bis 5"),
                ),
            ),
        )
        row = 0
        for heading, fields in groups:
            ctk.CTkLabel(
                self._metadata_container,
                text=heading,
                font=("Segoe UI", 15, "bold"),
            ).grid(row=row, column=0, padx=12, pady=(14, 4), sticky="w")
            row += 1
            for key, label, help_text in fields:
                row = self._metadata_field_row(model, row, key, label, help_text)
            if heading == "Grunddaten":
                self._metadata_decade_label = ctk.CTkLabel(
                    self._metadata_container,
                    text=f"Erscheinungsjahrzehnt: {model.release_decade or '—'} (automatisch berechnet)",
                    text_color="#9aa4b2",
                )
                self._metadata_decade_label.grid(row=row, column=0, padx=16, pady=4, sticky="w")
                row += 1
            if heading == "Aufnahme und Tempo":
                row = self._metadata_recording_row(model, row)
        row = self._metadata_multivalue_rows(model, row)
        ctk.CTkLabel(
            self._metadata_container, text="Redaktion", font=("Segoe UI", 15, "bold")
        ).grid(row=row, column=0, padx=12, pady=(14, 4), sticky="w")
        row += 1
        row = self._metadata_field_row(
            model,
            row,
            MetadataFieldKey.COMMENT,
            "Kommentar",
            "Redaktioneller Katalogtext; die Musikdatei wird nicht verändert",
        )
        self._metadata_suggestions_row = row

    def _metadata_field_row(
        self,
        model: TrackMetadataEditorViewModel,
        row: int,
        key: MetadataFieldKey,
        label: str,
        help_text: str,
    ) -> int:
        field = model.field(key)
        frame = ctk.CTkFrame(self._metadata_container)
        frame.grid(row=row, column=0, padx=10, pady=3, sticky="ew")
        frame.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(frame, text=label, width=190, anchor="w").grid(
            row=0, column=0, padx=8, pady=(7, 2), sticky="w"
        )
        entry = (
            ctk.CTkTextbox(frame, height=90)
            if key is MetadataFieldKey.COMMENT
            else ctk.CTkEntry(frame)
        )
        entry.grid(row=0, column=1, padx=6, pady=(7, 2), sticky="ew")
        if field.value is not None:
            entry.insert("1.0" if key is MetadataFieldKey.COMMENT else 0, str(field.value))
        if key is MetadataFieldKey.BPM_CONFIDENCE:
            entry.configure(state="disabled")
        self._metadata_entries[key] = entry
        status = ctk.CTkLabel(
            frame,
            text=self._metadata_status_text(
                field.source_text, field.status_text, field.has_suggestion
            ),
            text_color="#9fb3c8",
            anchor="w",
            justify="left",
            wraplength=360,
        )
        status.grid(row=1, column=1, padx=6, pady=(0, 6), sticky="ew")
        self._metadata_status_labels[key] = status
        buttons = ctk.CTkFrame(frame, fg_color="transparent")
        buttons.grid(row=0, column=2, rowspan=2, padx=6, pady=4)
        if key is not MetadataFieldKey.BPM_CONFIDENCE:
            ctk.CTkButton(
                buttons,
                text="Bestätigen",
                width=90,
                command=lambda selected=key: self._confirm_metadata_value(selected),
            ).pack(pady=2)
            ctk.CTkButton(
                buttons,
                text="Ohne Wert",
                width=90,
                fg_color="#6b5b2a",
                command=lambda selected=key: self._confirm_metadata_empty(selected),
            ).pack(pady=2)
        self._metadata_tooltips.append(Tooltip(entry, help_text))
        return row + 1

    def _metadata_recording_row(self, model: TrackMetadataEditorViewModel, row: int) -> int:
        field = model.field(MetadataFieldKey.RECORDING_CLASSIFICATION)
        value = field.value
        recording = (
            value
            if isinstance(value, RecordingClassification)
            else RecordingClassification(RecordingKind.UNKNOWN)
        )
        frame = ctk.CTkFrame(self._metadata_container)
        frame.grid(row=row, column=0, padx=10, pady=3, sticky="ew")
        frame.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(frame, text="Aufnahmeart", width=190, anchor="w").grid(
            row=0, column=0, padx=8, pady=(7, 2), sticky="w"
        )
        labels = {
            RecordingKind.ORIGINAL: "Original",
            RecordingKind.RE_RECORDING: "Neuaufnahme",
            RecordingKind.LIVE: "Liveaufnahme",
            RecordingKind.REMIX: "Remix",
            RecordingKind.RADIO_EDIT: "Radio Edit",
            RecordingKind.UNKNOWN: "Unbekannt",
        }
        self._recording_labels = labels
        controls = ctk.CTkFrame(frame, fg_color="transparent")
        controls.grid(row=0, column=1, padx=6, pady=(7, 2), sticky="w")
        self._recording_menu = ctk.CTkOptionMenu(controls, values=list(labels.values()))
        self._recording_menu.set(labels[recording.kind])
        self._recording_menu.pack(side="left")
        self._remastered_switch = ctk.CTkSwitch(controls, text="Remastert")
        self._remastered_switch.pack(side="left", padx=12)
        if recording.traits:
            self._remastered_switch.select()
        status = ctk.CTkLabel(
            frame,
            text=self._metadata_status_text(
                field.source_text, field.status_text, field.has_suggestion
            ),
            text_color="#9fb3c8",
            anchor="w",
            justify="left",
            wraplength=520,
        )
        status.grid(row=1, column=1, padx=6, pady=(0, 7), sticky="ew")
        self._metadata_status_labels[MetadataFieldKey.RECORDING_CLASSIFICATION] = status
        return row + 1

    def _metadata_multivalue_rows(self, model: TrackMetadataEditorViewModel, row: int) -> int:
        ctk.CTkLabel(
            self._metadata_container,
            text="Mehrfachwerte",
            font=("Segoe UI", 15, "bold"),
        ).grid(row=row, column=0, padx=12, pady=(14, 4), sticky="w")
        row += 1
        definitions = (
            (MetadataFieldKey.MUSICAL_DECADES, "Musikalische Dekaden", "z. B. 1970, 1980"),
            (MetadataFieldKey.ADDITIONAL_GENRES, "Zusätzliche Genres/Stile", "Kontrollierte Stile"),
            (MetadataFieldKey.MOODS, "Stimmungen", "Redaktionelle Stimmungen"),
            (MetadataFieldKey.TAGS, "Freie Tags", "Freie Begriffe"),
        )
        for key, label, hint in definitions:
            field = model.field(key)
            frame = ctk.CTkFrame(self._metadata_container)
            frame.grid(row=row, column=0, padx=10, pady=3, sticky="ew")
            frame.grid_columnconfigure(0, weight=1)
            ctk.CTkLabel(frame, text=label, anchor="w").grid(
                row=0, column=0, columnspan=2, padx=8, pady=(7, 2), sticky="ew"
            )
            textbox = ctk.CTkTextbox(frame, height=54)
            textbox.grid(row=1, column=0, padx=(8, 6), pady=4, sticky="ew")
            values = field.value if isinstance(field.value, tuple) else ()
            textbox.insert("1.0", "\n".join(str(item) for item in values))
            self._metadata_entries[key] = textbox
            ctk.CTkLabel(
                frame,
                text=f"{hint} · {field.source_text} · {field.status_text}",
                text_color="#9fb3c8",
                anchor="w",
                justify="left",
                wraplength=520,
            ).grid(row=2, column=0, columnspan=2, padx=8, pady=(0, 7), sticky="ew")
            actions = ctk.CTkFrame(frame, fg_color="transparent")
            actions.grid(row=1, column=1, padx=(2, 8), pady=4, sticky="n")
            if key is MetadataFieldKey.MUSICAL_DECADES:
                decade_menu = ctk.CTkOptionMenu(
                    actions,
                    values=[str(year) for year in range(1940, 2030, 10)],
                    width=90,
                )
                decade_menu.pack(pady=2)
                ctk.CTkButton(
                    actions,
                    text="Hinzufügen",
                    width=90,
                    command=lambda menu=decade_menu, target=textbox: self._add_metadata_decade(
                        menu, target
                    ),
                ).pack(pady=2)
            ctk.CTkButton(
                actions,
                text="Bestätigen",
                width=90,
                command=lambda selected=key: self._confirm_metadata_value(selected),
            ).pack(pady=2)
            ctk.CTkButton(
                actions,
                text="Ohne Wert",
                width=90,
                fg_color="#6b5b2a",
                command=lambda selected=key: self._confirm_metadata_empty(selected),
            ).pack(pady=2)
            row += 1
        return row

    @staticmethod
    def _add_metadata_decade(menu: Any, textbox: Any) -> None:
        existing = {
            value.strip()
            for value in str(textbox.get("1.0", "end")).replace(",", "\n").splitlines()
            if value.strip()
        }
        existing.add(str(menu.get()))
        textbox.delete("1.0", "end")
        textbox.insert("1.0", "\n".join(sorted(existing)))

    def _render_metadata_suggestions(self, model: TrackMetadataEditorViewModel) -> None:
        row = self._metadata_suggestions_row
        ctk.CTkLabel(
            self._metadata_container,
            text="Offene Vorschläge",
            font=("Segoe UI", 15, "bold"),
        ).grid(row=row, column=0, padx=12, pady=(16, 4), sticky="w")
        row += 1
        if not model.suggestions:
            ctk.CTkLabel(
                self._metadata_container,
                text="Für diesen Titel liegen keine offenen Vorschläge vor.",
                text_color="#9aa4b2",
            ).grid(row=row, column=0, padx=16, pady=8, sticky="w")
            return
        for suggestion in model.suggestions:
            frame = ctk.CTkFrame(self._metadata_container, border_width=1)
            frame.grid(row=row, column=0, padx=10, pady=4, sticky="ew")
            text = (
                f"{FIELD_LABELS[suggestion.field_key]}: {suggestion.current_value or '—'} → "
                f"{suggestion.suggested_value}\n{suggestion.source_text} · "
                f"Konfidenz {suggestion.confidence:.0%} · {suggestion.source_detail or 'ohne Detail'} · "
                f"{suggestion.created_at}"
            )
            if suggestion.protected_conflict:
                text += " · Geschützter Wert – ausdrückliche Bestätigung erforderlich"
            ctk.CTkLabel(frame, text=text, justify="left", wraplength=620).pack(
                anchor="w", padx=8, pady=6
            )
            actions = ctk.CTkFrame(frame, fg_color="transparent")
            actions.pack(anchor="w", padx=8, pady=(0, 6))
            for label, action in (
                ("Übernehmen", SuggestionEditorAction.ACCEPT),
                ("Übernehmen und bestätigen", SuggestionEditorAction.ACCEPT_AND_CONFIRM),
                ("Ablehnen", SuggestionEditorAction.REJECT),
            ):
                ctk.CTkButton(
                    actions,
                    text=label,
                    command=lambda item=suggestion, selected=action: self._stage_suggestion(
                        item.suggestion_id, selected, item.protected_conflict
                    ),
                ).pack(side="left", padx=3)
            ctk.CTkButton(
                actions, text="Später prüfen", fg_color="#555555", command=lambda: None
            ).pack(side="left", padx=3)
            row += 1

    @staticmethod
    def _metadata_status_text(source: str, status: str, suggestion: bool) -> str:
        return f"{source} · {status}" + (" · Neuer Vorschlag vorhanden" if suggestion else "")

    def _confirm_metadata_value(self, key: MetadataFieldKey) -> None:
        self._metadata_confirmations.add(key)
        self._metadata_removals.pop(key, None)
        self._error.configure(text="Bestätigung lokal vorgemerkt; erst Speichern übernimmt sie.")

    def _confirm_metadata_empty(self, key: MetadataFieldKey) -> None:
        self._clear_metadata_widget(key)
        self._metadata_confirmations.discard(key)
        self._metadata_removals[key] = ValueRemovalMode.CONFIRMED_EMPTY
        self._error.configure(text="Bewusster Leerwert lokal vorgemerkt.")

    def _clear_metadata_widget(self, key: MetadataFieldKey) -> None:
        widget = self._metadata_entries.get(key)
        if widget is None:
            return
        if isinstance(widget, ctk.CTkTextbox):
            widget.delete("1.0", "end")
        else:
            widget.delete(0, "end")

    def _stage_suggestion(
        self, suggestion_id: int, action: SuggestionEditorAction, protected: bool
    ) -> None:
        allow_override = False
        if protected and action is not SuggestionEditorAction.REJECT:
            answer = ask_silent_yes_no_cancel(
                self,
                "Geschützten Wert ersetzen?",
                "Der aktuelle Wert ist manuell geschützt. Soll der Vorschlag bewusst übernommen werden?",
            )
            if answer is not True:
                return
            allow_override = True
        self._metadata_suggestion_actions[suggestion_id] = StagedSuggestionAction(
            suggestion_id, action, allow_override
        )
        self._error.configure(text="Vorschlagsentscheidung lokal vorgemerkt.")

    def _field(
        self, row: int, label: str, value: float | None, current_button: str | None
    ) -> ctk.CTkEntry:
        parent = self._cue_parent
        ctk.CTkLabel(parent, text=f"{label} in Sekunden:", anchor="w").grid(
            row=row, column=0, padx=20, pady=8, sticky="w"
        )
        entry = ctk.CTkEntry(parent)
        entry.grid(row=row, column=1, padx=8, pady=8, sticky="ew")
        if value is not None:
            entry.insert(0, f"{value:.3f}")
        if current_button is not None:
            ctk.CTkButton(
                parent, text=current_button, command=lambda target=entry: self._set_current(target)
            ).grid(row=row, column=2, padx=20, pady=8, sticky="ew")
        else:
            ctk.CTkLabel(parent, text="leer = globale Einstellung", text_color="#999999").grid(
                row=row, column=2, padx=20, pady=8, sticky="w"
            )
        return entry

    def _collect_metadata_changes(self) -> TrackMetadataChanges | None:
        model = self._view_model.metadata
        if model is None:
            if self._metadata_loading:
                raise ValueError("Metadaten werden noch geladen. Bitte kurz warten.")
            return None
        scalar_keys = (
            MetadataFieldKey.TITLE,
            MetadataFieldKey.ARTIST,
            MetadataFieldKey.ALBUM,
            MetadataFieldKey.MAIN_GENRE,
            MetadataFieldKey.YEAR,
            MetadataFieldKey.ORIGINAL_RELEASE_YEAR,
            MetadataFieldKey.BPM,
            MetadataFieldKey.ALTERNATIVE_BPM,
            MetadataFieldKey.ENERGY,
            MetadataFieldKey.DANCEABILITY,
            MetadataFieldKey.LANGUAGE,
            MetadataFieldKey.RATING,
            MetadataFieldKey.COMMENT,
        )
        multivalue_keys = (
            MetadataFieldKey.MUSICAL_DECADES,
            MetadataFieldKey.ADDITIONAL_GENRES,
            MetadataFieldKey.MOODS,
            MetadataFieldKey.TAGS,
        )
        scalar_inputs = {
            key: str(
                self._metadata_entries[key].get("1.0", "end")
                if key is MetadataFieldKey.COMMENT
                else self._metadata_entries[key].get()
            )
            for key in scalar_keys
        }
        multivalue_inputs = {
            key: str(self._metadata_entries[key].get("1.0", "end")) for key in multivalue_keys
        }
        removals = dict(self._metadata_removals)
        for key, raw in (*scalar_inputs.items(), *multivalue_inputs.items()):
            original = model.field(key).value
            if raw.strip() or original in (None, "", ()) or key in removals:
                continue
            answer = ask_silent_yes_no_cancel(
                self,
                "Metadatenwert entfernen",
                (
                    f"Der Wert für „{FIELD_LABELS[key]}“ wurde geleert.\n\n"
                    "Ja: bewusst ohne Wert bestätigen\n"
                    "Nein: als fehlend/ungeprüft speichern\n"
                    "Abbrechen: nicht speichern"
                ),
            )
            if answer is None:
                raise ValueError("Speichern wurde abgebrochen.")
            removals[key] = ValueRemovalMode.CONFIRMED_EMPTY if answer else ValueRemovalMode.MISSING
        selected_recording = self._recording_menu.get()
        recording_kind = next(
            key for key, label in self._recording_labels.items() if label == selected_recording
        )
        return self._editor_controller.build_metadata_changes(
            model,
            scalar_inputs,
            recording_kind,
            bool(self._remastered_switch.get()),
            multivalue_inputs,
            frozenset(self._metadata_confirmations),
            removals,
            tuple(self._metadata_suggestion_actions.values()),
        )

    def _set_current(self, entry: ctk.CTkEntry) -> None:
        try:
            self._replace(entry, f"{self._controller.current_position(self._track_id):.3f}")
            self._error.configure(text="")
        except ValueError as exc:
            self._error.configure(text=str(exc))

    def _use_safe_defaults(self) -> None:
        resolved = self._view_model.cue.resolved
        for entry, value in (
            (self._cue_in, resolved.cue_in),
            (self._cue_out, resolved.cue_out),
            (self._fade, resolved.fade_duration),
        ):
            self._replace(entry, f"{value:.3f}")
        self._error.configure(
            text="Sichere Werte eingesetzt. Zum dauerhaften Übernehmen noch speichern."
        )

    def _save(self) -> None:
        if self._saving:
            return
        try:
            metadata_changes = self._collect_metadata_changes()
            changes = TrackEditorChanges(
                self._editor_controller.parse_optional_seconds(self._cue_in.get(), "Cue In"),
                self._editor_controller.parse_optional_seconds(self._cue_out.get(), "Cue Out"),
                self._editor_controller.parse_optional_seconds(self._fade.get(), "Überblenddauer"),
                self._discard_automatic,
            )
            changed = self._editor_controller.has_cue_changes(self._view_model, changes)
            self._saving = True
            self._save_had_changes = changed
            self._pending_metadata_changes = metadata_changes
            self._save_button.configure(state="disabled", text="Speichert …")
            self._editor_controller.save_async(
                self._view_model,
                changes,
                self._save_completed,
                self._save_failed,
            )
        except ValueError as exc:
            self._saving = False
            self._save_button.configure(state="normal", text="Speichern")
            self._error.configure(text=str(exc))
            self._editor_controller.record_event("track_editor_validation_failed_total")

    def _save_completed(self, view_model: TrackEditorViewModel) -> None:
        if not self._is_active():
            return
        self._view_model = view_model
        self._show_sources(view_model.cue)
        metadata_changes = getattr(self, "_pending_metadata_changes", None)
        if metadata_changes is not None and not metadata_changes.empty:
            accepted = self._editor_controller.save_metadata_async(
                self._track_id,
                metadata_changes,
                self._metadata_save_completed,
                self._metadata_save_failed,
            )
            if accepted:
                return
        CuePointDialog._finish_successful_save(self)

    def _metadata_save_completed(self, result: MetadataSaveResult) -> None:
        if not self._is_active():
            return
        self._view_model = self._editor_controller.with_metadata(
            self._view_model, result.view_model
        )
        self._save_had_changes = self._save_had_changes or result.revision_changed
        CuePointDialog._finish_successful_save(self)

    def _finish_successful_save(self) -> None:
        preview_was_active = self._controller.active_preview_count > 0
        self._controller.stop_preview()
        self._controller.cancel_analysis()
        if preview_was_active:
            self._editor_controller.record_event("track_editor.cue_preview_stop")
            self._editor_controller.record_event("track_editor_preview_stopped_total")
        if self._save_had_changes and self._on_saved is not None:
            self._on_saved(self._view_model)
        self._editor_controller.record_event("track_editor_save_total")
        self._finish()

    def _metadata_save_failed(self, error: Exception) -> None:
        if not self._is_active():
            return
        if isinstance(error, MetadataRevisionConflict):
            self._error.configure(
                text=(
                    "Die Metadaten wurden zwischenzeitlich geändert. Der aktuelle Stand "
                    "wird neu geladen; lokale Eingaben wurden nicht gespeichert."
                )
            )
            self._editor_controller.load_metadata_async(
                self._track_id,
                self._resolve_metadata_conflict,
                self._save_failed,
            )
            return
        self._save_failed(error)

    def _resolve_metadata_conflict(self, model: TrackMetadataEditorViewModel) -> None:
        if not self._is_active():
            return
        pending = self._pending_metadata_changes
        opened = self._view_model.metadata
        if opened is None:
            self._save_failed(RuntimeError("Geöffneter Metadatenstand ist nicht verfügbar"))
            return
        affected = set(pending.scalar_values if pending is not None else ())
        affected.update(pending.multivalue_values if pending is not None else ())
        affected.update(pending.confirmations if pending is not None else ())
        affected.update(pending.removals if pending is not None else ())
        comparison = "\n".join(
            f"• {FIELD_LABELS[key]}: geöffnet={opened.field(key).value!s}; "
            f"aktuell={model.field(key).value!s}"
            for key in sorted(affected, key=lambda item: item.value)
        )
        answer = ask_silent_yes_no_cancel(
            self,
            "Metadaten wurden geändert",
            (
                "Die folgenden Felder müssen erneut geprüft werden:\n"
                f"{comparison or '• Vorschlagsstatus wurde zwischenzeitlich geändert'}\n\n"
                "Ja: aktuellen Stand laden und lokale Eingaben verwerfen\n"
                "Nein: lokale Eingaben behalten und gegen den aktuellen Stand erneut prüfen\n"
                "Abbrechen: Dialog unverändert geöffnet lassen"
            ),
        )
        if answer is None:
            self._reenable_save_after_conflict()
            return
        self._view_model = self._editor_controller.with_metadata(self._view_model, model)
        if answer is False:
            self._reenable_save_after_conflict()
            self._error.configure(
                text="Lokale Eingaben beibehalten. Bitte Unterschiede prüfen und erneut speichern."
            )
            return
        self._metadata_confirmations.clear()
        self._metadata_removals.clear()
        self._metadata_suggestion_actions.clear()
        self._clear_metadata_container()
        self._render_metadata_fields(model)
        self._render_metadata_suggestions(model)
        self._reenable_save_after_conflict()
        self._error.configure(
            text="Aktueller Metadatenstand geladen. Bitte Änderungen erneut prüfen."
        )

    def _reenable_save_after_conflict(self) -> None:
        self._pending_metadata_changes = None
        self._saving = False
        self._save_button.configure(state="normal", text="Speichern")

    def _save_failed(self, error: Exception) -> None:
        if not self._is_active():
            return
        self._saving = False
        self._save_button.configure(state="normal", text="Speichern")
        self._error.configure(text=f"Speichern fehlgeschlagen: {error}")
        self._editor_controller.record_event("track_editor_persist_failed_total")

    def _preview(self, kind: str) -> None:
        try:
            self._editor_controller.record_event("track_editor.cue_preview_start")
            self._editor_controller.record_event("track_editor_preview_started_total")
            if kind == "in":
                self._controller.preview_cue_in(self._track_id, self._set_preview_status)
            else:
                self._controller.preview_cue_out(self._track_id, self._set_preview_status)
        except (ValueError, RuntimeError) as exc:
            self._error.configure(text=str(exc))

    def _stop_preview(self) -> None:
        self._controller.stop_preview()
        self._editor_controller.record_event("track_editor.cue_preview_stop")
        self._editor_controller.record_event("track_editor_preview_stopped_total")
        self._set_preview_status("Vorhören beendet")

    def _analyze(self) -> None:
        try:
            self._editor_controller.record_event("track_editor.analysis_start")
            self._analysis_button.configure(state="disabled")
            self._controller.analyze(
                self._track_id,
                None,
                self._set_analysis_status,
                state_completed=self._analysis_completed,
            )
        except (ValueError, RuntimeError) as exc:
            self._analysis_button.configure(state="normal")
            self._error.configure(text=str(exc))

    def _analysis_completed(self, state: CuePointEditorState) -> None:
        if not self._is_active():
            return
        self._view_model = replace(self._view_model, cue=state)
        self._show_sources(state)
        self._show_analysis_details(self._view_model)
        self._analysis_button.configure(state="normal")
        self._error.configure(text="")
        self._editor_controller.record_event("track_editor.analysis_complete")

    def _set_analysis_status(self, message: str) -> None:
        if self._is_active():
            self._analysis_status.configure(text=message)
            if "fehlgeschlagen" in message:
                self._analysis_button.configure(state="normal")

    def _adopt_analysis(self) -> None:
        try:
            suggestion = self._editor_controller.automatic_suggestion(self._view_model)
        except ValueError as exc:
            self._error.configure(text=str(exc))
            return
        self._replace(
            self._cue_in,
            f"{suggestion.cue_in:.3f}" if suggestion.cue_in is not None else "",
        )
        self._replace(
            self._cue_out,
            f"{suggestion.cue_out:.3f}" if suggestion.cue_out is not None else "",
        )
        self._replace(
            self._fade,
            f"{suggestion.fade_duration:.3f}" if suggestion.fade_duration is not None else "",
        )
        self._set_analysis_status(
            "Vorschlag übernommen. Die Werte werden erst mit „Speichern“ gesichert."
        )
        self._error.configure(text="")

    def _discard_analysis(self) -> None:
        if self._view_model.analysis_state == "NONE":
            self._set_analysis_status("Es ist kein automatischer Vorschlag vorhanden.")
            return
        self._discard_automatic = True
        self._analysis_details.configure(
            text="Der automatische Vorschlag wird erst mit „Speichern“ verworfen."
        )
        self._set_analysis_status("Verwerfen lokal vorgemerkt.")
        self._error.configure(text="")

    def _cancel(self) -> None:
        if getattr(self, "_saving", False):
            self._error.configure(
                text="Der Titel wird gerade gespeichert. Bitte einen Moment warten."
            )
            return
        preview_was_active = self._controller.active_preview_count > 0
        self._controller.stop_preview()
        self._controller.cancel_analysis()
        if preview_was_active:
            self._editor_controller.record_event("track_editor.cue_preview_stop")
            self._editor_controller.record_event("track_editor_preview_stopped_total")
        self._editor_controller.record_event("track_editor_cancel_total")
        self._finish()

    def _finish(self) -> None:
        if self._closed:
            return
        self._closed = True
        build_after_id = getattr(self, "_build_after_id", None)
        if build_after_id is not None:
            try:
                self.after_cancel(build_after_id)
            except (RuntimeError, TclError):
                pass
            self._build_after_id = None
        path_tooltip = getattr(self, "_path_tooltip", None)
        if path_tooltip is not None:
            path_tooltip.close()
            self._path_tooltip = None
        for tooltip in getattr(self, "_metadata_tooltips", ()):
            tooltip.close()
        if hasattr(self, "_metadata_tooltips"):
            self._metadata_tooltips.clear()
        self._editor_controller.record_event("track_editor.close")
        release_dialog(self)
        self.destroy()
        if self._on_closed is not None:
            self._on_closed()

    def _set_preview_status(self, message: str) -> None:
        if self._is_active():
            self._preview_status.configure(text=message)

    def _is_active(self) -> bool:
        if self._closed:
            return False
        try:
            return bool(self.winfo_exists())
        except (RuntimeError, TclError):
            return False

    def _show_sources(self, state: Any) -> None:
        warning = f"\n\n⚠ {state.resolved.warning}" if state.resolved.warning else ""
        self._sources.configure(
            text=(
                f"Start: {state.resolved.cue_in:.2f} s — {state.cue_in_source_text}\n"
                f"Überblendstart: {state.resolved.crossfade_start:.2f} s\n"
                f"Ende: {state.resolved.cue_out:.2f} s — {state.cue_out_source_text}\n"
                f"Dauer: {state.resolved.fade_duration:.2f} s — {state.fade_source_text}"
                f"{warning}"
            )
        )

    def _show_analysis_details(self, view_model: TrackEditorViewModel) -> None:
        state = view_model.cue
        if view_model.analysis_state == "NONE":
            self._analysis_details.configure(
                text=(
                    "Analysezustand: keine Analyse\n"
                    f"Effektive Spieldauer: {view_model.effective_play_duration:.2f} s"
                )
            )
            return
        if view_model.analysis_state == "INCOMPLETE":
            self._analysis_details.configure(
                text=(
                    "Analysezustand: unvollständiges gespeichertes Ergebnis\n"
                    "Der Vorschlag kann nicht übernommen werden."
                )
            )
            return
        status = (
            "gespeicherter und übernommener Vorschlag"
            if view_model.analysis_state == "ADOPTED"
            else "gespeicherter, noch nicht übernommener Vorschlag"
        )
        confidence = (
            f"{state.confidence:.0%}" if state.confidence is not None else "nicht verfügbar"
        )
        level_range = (
            f"{state.minimum_level_dbfs:.1f} bis {state.maximum_level_dbfs:.1f} dBFS"
            if state.minimum_level_dbfs is not None and state.maximum_level_dbfs is not None
            else "nicht verfügbar"
        )
        peak = f"{state.peak:.3f}" if state.peak is not None else "nicht verfügbar"
        self._analysis_details.configure(
            text=(
                f"Analysezustand: {status}\n"
                f"Cue In: {state.automatic_cue_in:.2f} s · "
                f"Cue Out: {state.automatic_cue_out:.2f} s · "
                f"Überblendung: {state.automatic_fade_duration or 0.0:.2f} s\n"
                f"Effektive Spieldauer: {view_model.effective_play_duration:.2f} s\n"
                f"Pegel: {level_range} · Peak: {peak} · Konfidenz: {confidence}\n"
                f"Version: {state.analysis_version or '—'} · "
                f"Backend: {state.analysis_backend or '—'} · "
                f"Zeitpunkt: {state.analysed_at or '—'}"
            )
        )

    @staticmethod
    def _replace(entry: ctk.CTkEntry, value: str) -> None:
        entry.delete(0, "end")
        entry.insert(0, value)

    @classmethod
    def _clear(cls, entry: ctk.CTkEntry) -> None:
        cls._replace(entry, "")


class QueueCueDialog(ctk.CTkToplevel):  # type: ignore[misc]
    """Edit cue overrides owned by one waiting party-queue entry."""

    def __init__(self, parent: Any, controller: Any, queue_id: int) -> None:
        super().__init__(parent)
        self._controller = controller
        self._queue_id = queue_id
        state = controller.queue_cue_state(queue_id)
        self.title("Queue-Cues bearbeiten")
        self.geometry("620x440")
        self.transient(parent)
        self.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(self, text=state.title, font=("Segoe UI", 18, "bold")).grid(
            row=0, column=0, columnspan=2, padx=20, pady=(20, 14), sticky="w"
        )
        self._cue_in = self._field(1, "Cue In", state.cue_in_override)
        self._cue_out = self._field(2, "Cue Out", state.cue_out_override)
        self._fade = self._field(3, "Überblenddauer", state.fade_duration_override)
        self._effective = ctk.CTkLabel(self, text="", justify="left", text_color="#b8c7d9")
        self._effective.grid(row=4, column=0, columnspan=2, padx=20, pady=12, sticky="w")
        self._show_effective(state)
        cue_actions = ctk.CTkFrame(self, fg_color="transparent")
        cue_actions.grid(row=5, column=0, columnspan=2, padx=20, pady=4, sticky="ew")
        ctk.CTkButton(
            cue_actions, text="Titelwerte übernehmen", command=self._adopt_title_values
        ).pack(side="left", padx=(0, 6))
        ctk.CTkButton(
            cue_actions,
            text="Queue-Werte zurücksetzen",
            fg_color="#6d5555",
            command=self._reset_queue_values,
        ).pack(side="left", padx=6)
        ctk.CTkButton(
            cue_actions,
            text="Sichere Standardwerte",
            fg_color="#8a6d1f",
            command=self._use_safe_defaults,
        ).pack(side="left", padx=6)
        self._error = ctk.CTkLabel(self, text="", text_color="#ff8585", wraplength=560)
        self._error.grid(row=6, column=0, columnspan=2, padx=20, pady=4, sticky="w")
        buttons = ctk.CTkFrame(self, fg_color="transparent")
        buttons.grid(row=7, column=0, columnspan=2, padx=20, pady=(12, 20), sticky="e")
        ctk.CTkButton(buttons, text="Abbrechen", fg_color="#555555", command=self.destroy).pack(
            side="right", padx=5
        )
        ctk.CTkButton(buttons, text="Speichern", fg_color="#2f7d4f", command=self._save).pack(
            side="right", padx=5
        )
        self.grab_set()
        self.focus_force()

    def _field(self, row: int, label: str, value: float | None) -> ctk.CTkEntry:
        ctk.CTkLabel(self, text=f"{label} in Sekunden:").grid(
            row=row, column=0, padx=20, pady=8, sticky="w"
        )
        entry = ctk.CTkEntry(self)
        entry.grid(row=row, column=1, padx=20, pady=8, sticky="ew")
        if value is not None:
            entry.insert(0, f"{value:.3f}")
        return entry

    def _save(self) -> None:
        try:
            state = self._controller.save_queue_cues(
                self._queue_id,
                CuePointDialog._number(self._cue_in),
                CuePointDialog._number(self._cue_out),
                CuePointDialog._number(self._fade),
            )
        except ValueError as exc:
            self._error.configure(text=str(exc))
            return
        self._show_effective(state)
        self.destroy()

    def _show_effective(self, state: Any) -> None:
        resolved = state.resolved
        warning = f"\n⚠ {resolved.warning}" if resolved.warning else ""
        self._effective.configure(
            text=(
                f"Wirksam: {resolved.cue_in:.2f} s → {resolved.cue_out:.2f} s\n"
                f"Überblendung ab {resolved.crossfade_start:.2f} s "
                f"({resolved.fade_duration:.2f} s)\n"
                "Leere Felder erben weiterhin die aktuellen Titelwerte."
                f"{warning}"
            )
        )

    def _adopt_title_values(self) -> None:
        try:
            state = self._controller.adopt_title_cues_for_queue(self._queue_id)
        except ValueError as exc:
            self._error.configure(text=str(exc))
            return
        self._replace_fields(state)

    def _reset_queue_values(self) -> None:
        try:
            state = self._controller.reset_queue_cues(self._queue_id)
        except ValueError as exc:
            self._error.configure(text=str(exc))
            return
        self._replace_fields(state)

    def _use_safe_defaults(self) -> None:
        state = self._controller.queue_cue_state(self._queue_id)
        resolved = state.resolved
        for entry, value in (
            (self._cue_in, resolved.cue_in),
            (self._cue_out, resolved.cue_out),
            (self._fade, resolved.fade_duration),
        ):
            CuePointDialog._replace(entry, f"{value:.3f}")
        self._error.configure(
            text="Sichere Werte eingesetzt. Zum dauerhaften Übernehmen noch speichern."
        )

    def _replace_fields(self, state: Any) -> None:
        for entry, value in (
            (self._cue_in, state.cue_in_override),
            (self._cue_out, state.cue_out_override),
            (self._fade, state.fade_duration_override),
        ):
            CuePointDialog._replace(entry, "" if value is None else f"{value:.3f}")
        self._error.configure(text="")
        self._show_effective(state)


class SilentDialog(ctk.CTkToplevel):  # type: ignore[misc]
    """Modal CustomTkinter dialog without native message-box sounds."""

    def __init__(self, parent: Any, title: str, message: str, kind: DialogKind) -> None:
        super().__init__(parent)
        self.result: bool | None = None
        self.title(title)
        self.resizable(False, False)
        self.transient(parent)
        self.protocol("WM_DELETE_WINDOW", self._cancel)

        color = "#ff8585" if kind == "error" else "#f2f2f2"
        ctk.CTkLabel(
            self,
            text=message,
            wraplength=440,
            justify="left",
            text_color=color,
            font=("Segoe UI", 14),
        ).pack(fill="both", expand=True, padx=24, pady=(24, 18))
        buttons = ctk.CTkFrame(self, fg_color="transparent")
        buttons.pack(fill="x", padx=20, pady=(0, 20))

        if kind in {"info", "error"}:
            self._button(buttons, "OK", True, "#2f6da5")
        else:
            self._button(buttons, "Ja", True, "#2f7d4f")
            self._button(buttons, "Nein", False, "#7d3030")
            if kind == "yes_no_cancel":
                self._button(buttons, "Abbrechen", None, "#555555")

        # Deliberate one-shot exception: requested geometry is only known after
        # Tk has laid out this small modal. This is not used by catalog/queue
        # rendering and cannot create a recurring application-wide draw loop.
        self.update_idletasks()
        width = max(420, self.winfo_reqwidth())
        height = max(170, self.winfo_reqheight())
        x = parent.winfo_rootx() + max(0, (parent.winfo_width() - width) // 2)
        y = parent.winfo_rooty() + max(0, (parent.winfo_height() - height) // 2)
        self.geometry(f"{width}x{height}+{x}+{y}")
        self.grab_set()
        self.focus_force()

    def _button(self, parent: Any, text: str, result: bool | None, color: str) -> None:
        ctk.CTkButton(
            parent,
            text=text,
            width=105,
            fg_color=color,
            command=lambda: self._finish(result),
        ).pack(side="right", padx=5)

    def _finish(self, result: bool | None) -> None:
        self.result = result
        self.grab_release()
        self.destroy()

    def _cancel(self) -> None:
        self._finish(None)


def show_silent_message(parent: Any, title: str, message: str, *, error: bool = False) -> None:
    dialog = SilentDialog(parent, title, message, "error" if error else "info")
    parent.wait_window(dialog)


def ask_silent_yes_no(parent: Any, title: str, message: str) -> bool:
    dialog = SilentDialog(parent, title, message, "yes_no")
    parent.wait_window(dialog)
    return dialog.result is True


def ask_silent_yes_no_cancel(parent: Any, title: str, message: str) -> bool | None:
    dialog = SilentDialog(parent, title, message, "yes_no_cancel")
    parent.wait_window(dialog)
    return dialog.result
