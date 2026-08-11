"""Pure presentation rules for the responsive overlay controls.

The module deliberately has no Tk, database, or audio dependencies.  Overlay
state can therefore be rendered in response to controller events without
adding work to the application's periodic playback status tick.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class OverlayLayout(StrEnum):
    """Supported layouts of the expanded mixer overlay section."""

    WIDE = "wide"
    COMPACT = "compact"


class OverlayState(StrEnum):
    """UI-facing overlay states."""

    READY = "ready"
    PREPARING = "preparing"
    FADING_IN = "fading_in"
    PLAYING = "playing"
    FADING_OUT = "fading_out"
    FINISHED = "finished"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class OverlayViewModel:
    """Immutable snapshot consumed by the overlay panel."""

    state: OverlayState = OverlayState.READY
    selected_name: str = ""
    active_name: str = ""
    category: str = "Alle"
    duration_ms: int | None = None
    position_ms: int | None = None
    volume_percent: int | None = None
    ducking_db: float | None = None
    error_message: str = ""

    @property
    def progress_known(self) -> bool:
        """Return whether a meaningful progress display can be rendered."""

        return (
            self.duration_ms is not None
            and self.duration_ms > 0
            and self.position_ms is not None
            and self.position_ms >= 0
        )


@dataclass(frozen=True, slots=True)
class OverlayActions:
    """Enabled state and label of the overlay runtime actions."""

    start_enabled: bool
    start_label: str
    fade_out_enabled: bool
    stop_enabled: bool
    selection_enabled: bool


@dataclass(frozen=True, slots=True)
class FavoritePadViewModel:
    """Persistent favorite metadata rendered by one existing soundboard pad."""

    name: str = ""
    category: str = ""
    ducking_db: float | None = None
    shortcut: str = ""
    missing_file: bool = False
    enabled: bool = True


def normalized_favorite_pads(
    favorites: Sequence[FavoritePadViewModel],
) -> tuple[FavoritePadViewModel, ...]:
    """Return exactly the six stable MVP pad slots."""

    return tuple(
        (
            *favorites[:6],
            *([FavoritePadViewModel()] * max(0, 6 - len(favorites))),
        )
    )


def overlay_actions(model: OverlayViewModel) -> OverlayActions:
    """Return the central action matrix for one overlay snapshot."""

    active = model.state in {OverlayState.FADING_IN, OverlayState.PLAYING}
    if model.state == OverlayState.ERROR:
        return OverlayActions(
            bool(model.selected_name),
            "Erneut versuchen",
            False,
            False,
            True,
        )
    if model.state == OverlayState.PREPARING:
        return OverlayActions(False, "Start", False, True, False)
    if active:
        switching = bool(model.selected_name and model.selected_name != model.active_name)
        return OverlayActions(
            switching,
            "Wechseln" if switching else "Start",
            True,
            True,
            True,
        )
    if model.state == OverlayState.FADING_OUT:
        return OverlayActions(False, "Start", False, True, False)
    return OverlayActions(bool(model.selected_name), "Start", False, False, True)


class OverlayRenderGate:
    """Suppress repeated rendering of an identical immutable snapshot."""

    def __init__(self) -> None:
        self._last_model: OverlayViewModel | None = None

    def changed(self, model: OverlayViewModel) -> bool:
        """Remember *model* and report whether the visible state changed."""

        if model == self._last_model:
            return False
        self._last_model = model
        return True

    def invalidate(self) -> None:
        """Force the next snapshot to be rendered, for example after relayout."""

        self._last_model = None


# This is also the order in which explicit ``tk_focusNext`` links are installed
# once the concrete overlay widgets are created.
OVERLAY_FOCUS_ORDER = (
    "category",
    "overlay",
    "start",
    "fade_out",
    "stop",
    "favorite_1",
    "favorite_2",
    "favorite_3",
    "favorite_4",
    "favorite_5",
    "favorite_6",
    "manage",
)


def overlay_layout(width: int, *, compact_below: int = 940) -> OverlayLayout:
    """Choose a layout without oscillating on transient invalid widths."""

    return OverlayLayout.COMPACT if max(0, width) < compact_below else OverlayLayout.WIDE


def abbreviated_name(name: str, *, maximum: int = 34) -> str:
    """Shorten a label while keeping the complete name available to a tooltip."""

    normalized = " ".join(name.split())
    if len(normalized) <= maximum:
        return normalized
    if maximum <= 1:
        return "…"[:maximum]
    return f"{normalized[: maximum - 1].rstrip()}…"


def overlay_time_text(milliseconds: int) -> str:
    """Format a non-negative overlay time as stable minute/second text."""

    seconds = max(0, milliseconds // 1000)
    return f"{seconds // 60:02d}:{seconds % 60:02d}"


def favorite_runtime_marker(state: OverlayState) -> str:
    """Return a stable, non-animated marker for a running favorite pad."""

    return {
        OverlayState.PREPARING: "◌",
        OverlayState.FADING_IN: "◒",
        OverlayState.PLAYING: "●",
        OverlayState.FADING_OUT: "◐",
    }.get(state, "")


def favorite_pad_text(
    position: int,
    name: str,
    *,
    marker: str = "",
    maximum_name_length: int = 22,
) -> str:
    """Render a stable two-line pad label; metadata remains in the tooltip."""

    prefix = f"{marker} {position}".strip()
    return f"{prefix}\n{abbreviated_name(name, maximum=maximum_name_length)}"


def ducking_switch_text(enabled: bool) -> str:
    """Make the persistent editor state explicit without runtime noise."""

    return "Musikabsenkung: aktiv" if enabled else "Musikabsenkung: aus"


def favorite_shortcut_text(favorite_value: str) -> str:
    """Describe the fixed shortcut derived from one favorite selection."""

    return (
        f"Strg+{favorite_value}" if favorite_value in {str(value) for value in range(1, 7)} else "—"
    )


def advanced_cue_visible(cue_in_ms: int, cue_out_ms: int | None) -> bool:
    """Keep non-default cue configuration visible when an entry is loaded."""

    return cue_in_ms > 0 or cue_out_ms is not None


def format_cue_time(milliseconds: int | None) -> str:
    """Format an optional cue position for the editor."""

    if milliseconds is None:
        return ""
    total_seconds = max(0, milliseconds) // 1000
    return f"{total_seconds // 60:02d}:{total_seconds % 60:02d}"


def parse_cue_time(value: str, *, optional: bool = False) -> int | None:
    """Parse editor cue input and return milliseconds."""

    normalized = value.strip()
    if not normalized and optional:
        return None
    parts = normalized.split(":")
    if len(parts) != 2 or not all(part.isdigit() for part in parts) or len(parts[1]) != 2:
        raise ValueError("Zeit muss im Format mm:ss eingegeben werden")
    minutes, seconds = (int(part) for part in parts)
    if seconds >= 60:
        raise ValueError("Sekunden müssen zwischen 00 und 59 liegen")
    return ((minutes * 60) + seconds) * 1000


def mixer_overlay_header_text(
    *,
    expanded: bool,
    state: OverlayState,
    name: str,
) -> str:
    """Keep active/error overlays visible even when the mixer is collapsed."""

    if expanded:
        return "Mixer ausblenden ▲"
    if state == OverlayState.ERROR:
        return f"Jingle: {name or 'Fehler'} · Fehler"
    if state in {
        OverlayState.PREPARING,
        OverlayState.FADING_IN,
        OverlayState.PLAYING,
        OverlayState.FADING_OUT,
    }:
        return f"Jingle: {name or 'aktiv'} · aktiv"
    return "Mixer einblenden ▼"


def collapsed_overlay_stop_visible(*, expanded: bool, state: OverlayState) -> bool:
    """Expose emergency stop only for a collapsed, active overlay."""

    return not expanded and state in {
        OverlayState.PREPARING,
        OverlayState.FADING_IN,
        OverlayState.PLAYING,
        OverlayState.FADING_OUT,
    }


def overlay_details_text(model: OverlayViewModel) -> str:
    """Build the secondary status line without querying runtime services."""

    parts: list[str] = []
    if model.selected_name:
        parts.append(f"Auswahl: {abbreviated_name(model.selected_name)}")
    if model.volume_percent is not None:
        parts.append(f"Lautstärke: {model.volume_percent} %")
    if model.progress_known:
        assert model.position_ms is not None and model.duration_ms is not None
        position = min(model.position_ms, model.duration_ms)
        remaining = max(0, model.duration_ms - position)
        parts.append(f"{overlay_time_text(position)} / {overlay_time_text(model.duration_ms)}")
        parts.append(f"Rest: {overlay_time_text(remaining)}")
    elif model.position_ms is not None:
        parts.append(f"Laufzeit: {overlay_time_text(model.position_ms)}")
    return " · ".join(parts)


def overlay_shortcut_allowed(focused_widget: Any, *, dialog_active: bool) -> bool:
    """Guard favorite shortcuts while the user types or a dialog owns input.

    CustomTkinter entries expose an internal Tk entry.  Walking the master
    chain also covers focus reported for that internal widget.
    """

    if dialog_active:
        return False
    widget = focused_widget
    while widget is not None:
        class_name = type(widget).__name__.lower()
        if "entry" in class_name or "text" in class_name or "spinbox" in class_name:
            return False
        widget = getattr(widget, "master", None)
    return True


def favorite_position_from_shortcut(keysym: str, control_pressed: bool) -> int | None:
    """Resolve the six MVP favorite shortcuts without claiming other bindings."""

    if not control_pressed or keysym not in {"1", "2", "3", "4", "5", "6"}:
        return None
    return int(keysym)
