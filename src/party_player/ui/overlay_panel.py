"""Responsive CustomTkinter panel for manual jingles and effects."""

from __future__ import annotations

from collections.abc import Callable, Sequence
import tkinter as tk
from typing import Any

import customtkinter as ctk  # type: ignore[import-untyped]

from party_player.ui import theme
from party_player.ui.overlay_presentation import (
    FavoritePadViewModel,
    OverlayLayout,
    OverlayRenderGate,
    OverlayState,
    OverlayViewModel,
    abbreviated_name,
    favorite_pad_text,
    favorite_runtime_marker,
    normalized_favorite_pads,
    overlay_details_text,
    overlay_actions,
    overlay_layout,
)
from party_player.ui.tooltip import SharedTooltipManager, SharedTooltipTarget


class OverlayPanel(ctk.CTkFrame):  # type: ignore[misc]
    """Render overlay state without polling, rebuilding, or audio-side work."""

    def __init__(
        self,
        master: Any,
        *,
        on_start: Callable[[], None],
        on_fade_out: Callable[[], None],
        on_stop: Callable[[], None],
        on_manage: Callable[[], None],
        on_favorite: Callable[[int], None],
        on_edit_favorite: Callable[[int], None],
        on_remove_favorite: Callable[[int], None],
        on_category: Callable[[str], None],
        on_selection: Callable[[str], None],
        favorite_hosts: tuple[Any, Any] | None = None,
    ) -> None:
        super().__init__(master, corner_radius=8)
        self._callbacks = (on_start, on_fade_out, on_stop, on_manage, on_favorite)
        self._edit_favorite = on_edit_favorite
        self._remove_favorite = on_remove_favorite
        self._gate = OverlayRenderGate()
        self._layout: OverlayLayout | None = None
        self._tooltip_manager = SharedTooltipManager()
        self._tooltip_targets: list[SharedTooltipTarget] = []
        self._favorite_tooltip_targets: list[SharedTooltipTarget] = []
        self._favorite_models = [FavoritePadViewModel()] * 6
        self._favorite_signatures: list[tuple[str, str, str] | None] = [None] * 6
        self._last_runtime_model = OverlayViewModel()
        self._favorite_hosts = favorite_hosts

        self.grid_columnconfigure(0, weight=1)
        self._header = ctk.CTkFrame(self, fg_color="transparent")
        self._header.grid(row=0, column=0, padx=12, pady=(8, 2), sticky="ew")
        self._header.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            self._header,
            text="JINGLES UND EFFEKTE",
            font=(theme.FONT_FAMILY, 13, "bold"),
        ).grid(row=0, column=0, sticky="w")
        self.channel_label = ctk.CTkLabel(
            self._header,
            text=(
                "SCHNELLSTART-PADS BEI DEN DECKS"
                if favorite_hosts is not None
                else "1 KANAL · WECHSEL MIT FADE"
            ),
            font=(theme.FONT_FAMILY, 10, "bold"),
            text_color=theme.TEXT_MUTED,
        )
        self.channel_label.grid(row=0, column=1, padx=(8, 0), sticky="e")

        self._selection = ctk.CTkFrame(self, fg_color="transparent")
        self._selection.grid(row=2, column=0, padx=12, pady=(2, 6), sticky="ew")
        self._selection.grid_columnconfigure(1, weight=1)
        self.category_menu = ctk.CTkOptionMenu(
            self._selection, values=["Alle"], command=on_category
        )
        self.overlay_menu = ctk.CTkOptionMenu(
            self._selection, values=["Keine Jingles"], command=on_selection
        )
        self.start_button = ctk.CTkButton(
            self._selection, text="▶ Start", width=105, command=on_start
        )
        self.manage_button = ctk.CTkButton(
            self._selection, text="⚙ Verwalten…", width=116, command=on_manage
        )

        self._runtime = ctk.CTkFrame(self, fg_color="transparent")
        self._runtime.grid(row=1, column=0, padx=12, pady=(2, 2), sticky="ew")
        self._runtime.grid_columnconfigure(0, weight=1)
        self.status_label = ctk.CTkLabel(self._runtime, text="Bereit", anchor="w")
        self.details_label = ctk.CTkLabel(
            self._runtime, text="", text_color=theme.TEXT_MUTED, anchor="w"
        )
        self.fade_button = ctk.CTkButton(
            self._runtime,
            text="◐ Ausfaden",
            width=112,
            fg_color=theme.WARNING,
            hover_color=theme.WARNING_HOVER,
            text_color=theme.TEXT_ON_WARNING,
            command=on_fade_out,
        )
        self.stop_button = ctk.CTkButton(
            self._runtime,
            text="■ Stop",
            width=90,
            fg_color=theme.DANGER,
            hover_color=theme.DANGER_HOVER,
            command=on_stop,
        )
        self._selection_tooltip = self._register_tooltip(
            self.overlay_menu, "Jingle oder Effekt auswählen"
        )
        self._status_tooltip = self._register_tooltip(self.status_label, "Aktueller Jingle-Zustand")
        self._register_tooltip(
            self.fade_button,
            "Jingle mit der konfigurierten Fade-out-Dauer weich ausblenden",
        )
        self._register_tooltip(
            self.stop_button,
            "Jingle mit kurzem Sicherheitsfade sofort stoppen",
        )
        self._register_tooltip(
            self.channel_label,
            "Es läuft immer genau ein Jingle. Ein neuer Start blendet den laufenden "
            "Jingle kontrolliert aus und wechselt anschließend.",
        )
        self._register_tooltip(self.manage_button, "Jingles, Effekte und Pad-Belegungen verwalten")
        self.ducking_label = ctk.CTkLabel(self, text="", text_color=theme.TEXT_MUTED, anchor="w")
        self.ducking_label.grid(row=3, column=0, padx=12, pady=(0, 2), sticky="ew")
        self.notice_label = ctk.CTkLabel(
            self,
            text="",
            text_color=theme.ERROR,
            anchor="w",
            wraplength=700,
        )
        self.notice_label.grid(row=4, column=0, padx=12, pady=(0, 4), sticky="ew")

        self._favorites = ctk.CTkFrame(self, fg_color="transparent")
        if favorite_hosts is None:
            self._favorites.grid(row=5, column=0, padx=12, pady=(4, 10), sticky="ew")
        self.favorite_buttons = tuple(
            ctk.CTkButton(
                (
                    self._favorites
                    if favorite_hosts is None
                    else favorite_hosts[(position - 1) // 3]
                ),
                text=f"{position} · + Belegen",
                height=48,
                fg_color=theme.SURFACE_RAISED,
                hover_color=theme.SURFACE_HOVER,
                border_width=1,
                border_color=theme.WARNING,
                command=lambda selected=position: on_favorite(selected),
            )
            for position in range(1, 7)
        )
        for position, button in enumerate(self.favorite_buttons, 1):
            button.bind(
                "<Button-3>",
                lambda event, selected=position: self._show_favorite_menu(event, selected),
                add="+",
            )
            target = self._register_tooltip(button, f"Favoritenplatz {position} ist unbelegt")
            self._favorite_tooltip_targets.append(target)
        if favorite_hosts is not None:
            for index, button in enumerate(self.favorite_buttons):
                button.grid(
                    row=0,
                    column=index % 3,
                    padx=3,
                    pady=3,
                    sticky="ew",
                )
        self._configure_focus_order()
        self.relayout(0)

    @property
    def focus_widgets(self) -> tuple[Any, ...]:
        """Return widgets in the documented logical focus order."""

        return (
            self.category_menu,
            self.overlay_menu,
            self.start_button,
            self.fade_button,
            self.stop_button,
            *self.favorite_buttons,
            self.manage_button,
        )

    def set_choices(
        self,
        categories: Sequence[str],
        overlays: Sequence[str],
    ) -> None:
        """Update menus only when their choices actually change."""

        category_values = list(categories) or ["Alle"]
        overlay_values = list(overlays) or ["Keine Jingles"]
        if list(self.category_menu.cget("values")) != category_values:
            self.category_menu.configure(values=category_values)
        if list(self.overlay_menu.cget("values")) != overlay_values:
            self.overlay_menu.configure(values=overlay_values)

    def select(self, *, category: str, overlay: str) -> None:
        """Synchronize menu values without invoking their commands."""

        if self.category_menu.get() != category:
            self.category_menu.set(category)
        if self.overlay_menu.get() != overlay:
            self.overlay_menu.set(overlay)
        self._selection_tooltip.set_text(
            overlay if overlay and overlay != "Keine Jingles" else "Kein Jingle ausgewählt"
        )

    def set_favorites(self, favorites: Sequence[FavoritePadViewModel]) -> None:
        """Rebind the six existing favorite widgets instead of recreating them."""

        padded = normalized_favorite_pads(favorites)
        for index, (_button, target, model) in enumerate(
            zip(
                self.favorite_buttons,
                self._favorite_tooltip_targets,
                padded,
                strict=True,
            ),
            1,
        ):
            if self._favorite_models[index - 1] == model:
                continue
            target.set_text(self._favorite_tooltip(index, model))
        self._favorite_models = list(padded)
        self._render_favorites(self._last_runtime_model)

    def show_notice(self, message: str, *, error: bool = True) -> None:
        """Show a non-modal local message without changing runtime actions."""

        self.notice_label.configure(
            text=f"⚠ {message}" if message else "",
            text_color=theme.ERROR if error else theme.WARNING,
        )

    def render(self, model: OverlayViewModel) -> bool:
        """Apply one controller event and skip identical snapshots."""

        if not self._gate.changed(model):
            return False
        self._last_runtime_model = model
        actions = overlay_actions(model)
        self.start_button.configure(
            text=f"▶ {actions.start_label}",
            state="normal" if actions.start_enabled else "disabled",
        )
        self.fade_button.configure(state="normal" if actions.fade_out_enabled else "disabled")
        self.stop_button.configure(state="normal" if actions.stop_enabled else "disabled")
        selection_state = "normal" if actions.selection_enabled else "disabled"
        self.category_menu.configure(state=selection_state)
        self.overlay_menu.configure(state=selection_state)
        self.status_label.configure(text=self._status_text(model))
        self.details_label.configure(text=overlay_details_text(model))
        self._status_tooltip.set_text(self._status_tooltip_text(model))
        ducking = (
            f"Musikabsenkung aktiv: {model.ducking_db:g} dB" if model.ducking_db is not None else ""
        )
        self.ducking_label.configure(text=ducking)
        self._render_favorites(model)
        return True

    def _render_favorites(self, runtime: OverlayViewModel) -> None:
        for index, (button, model) in enumerate(
            zip(self.favorite_buttons, self._favorite_models, strict=True),
            1,
        ):
            active = bool(model.name and model.name == runtime.active_name)
            if model.missing_file:
                visual_state = "missing"
                color = theme.DANGER
                hover = theme.DANGER_HOVER
                label = favorite_pad_text(index, "Datei fehlt")
            elif model.name and not model.enabled:
                visual_state = "disabled"
                color = theme.BORDER
                hover = theme.SURFACE_HOVER
                label = favorite_pad_text(index, "Deaktiviert")
            elif active and runtime.state == OverlayState.FADING_OUT:
                visual_state = "fading"
                color = theme.WARNING
                hover = theme.SURFACE_HOVER
                label = favorite_pad_text(
                    index,
                    model.name,
                    marker=favorite_runtime_marker(runtime.state),
                    maximum_name_length=20,
                )
            elif active and runtime.state in {
                OverlayState.PREPARING,
                OverlayState.FADING_IN,
                OverlayState.PLAYING,
            }:
                visual_state = runtime.state.value
                color = theme.WARNING if runtime.state == OverlayState.PREPARING else theme.SUCCESS
                hover = theme.SURFACE_HOVER
                label = favorite_pad_text(
                    index,
                    model.name,
                    marker=favorite_runtime_marker(runtime.state),
                    maximum_name_length=20,
                )
            elif model.name:
                visual_state = "ready"
                color = theme.SURFACE_RAISED
                hover = theme.SURFACE_HOVER
                label = favorite_pad_text(index, model.name)
            else:
                visual_state = "empty"
                color = theme.SURFACE_RAISED
                hover = theme.SURFACE_HOVER
                label = favorite_pad_text(index, "+ Belegen")
            signature = (label, visual_state, color)
            if self._favorite_signatures[index - 1] == signature:
                continue
            button.configure(text=label, fg_color=color, hover_color=hover)
            self._favorite_signatures[index - 1] = signature

    @staticmethod
    def _favorite_tooltip(index: int, model: FavoritePadViewModel) -> str:
        if not model.name:
            return f"Favoritenplatz {index} ist unbelegt"
        details = [model.name]
        if model.category:
            details.append(f"Kategorie: {model.category}")
        details.append(
            f"Musikabsenkung: {model.ducking_db:g} dB"
            if model.ducking_db is not None
            else "Musikabsenkung: aus"
        )
        if model.shortcut:
            details.append(f"Tastenkürzel: {model.shortcut}")
        if model.missing_file:
            details.append("Datei fehlt – in der Verwaltung neu zuweisen")
        if not model.enabled:
            details.append("Jingle ist deaktiviert – in der Verwaltung aktivieren")
        return "\n".join(details)

    @staticmethod
    def _status_tooltip_text(model: OverlayViewModel) -> str:
        details = []
        if model.active_name:
            details.append(f"Aktiv: {model.active_name}")
        if model.selected_name:
            details.append(f"Ausgewählt: {model.selected_name}")
        details.append(f"Zustand: {model.state.value}")
        if model.error_message:
            details.append(f"Fehler: {model.error_message}")
        return "\n".join(details)

    def _register_tooltip(self, widget: Any, text: str) -> SharedTooltipTarget:
        target = self._tooltip_manager.register(widget, text)
        self._tooltip_targets.append(target)
        return target

    def _show_favorite_menu(self, event: Any, position: int) -> str | None:
        model = self._favorite_models[position - 1]
        if not model.name:
            return None
        menu = tk.Menu(self, tearoff=False)
        menu.add_command(
            label="Belegung ändern",
            command=lambda: self._edit_favorite(position),
        )
        menu.add_command(
            label="Belegung entfernen",
            command=lambda: self._remove_favorite(position),
        )
        menu.tk_popup(int(event.x_root), int(event.y_root))
        return "break"

    def relayout(self, width: int) -> bool:
        """Re-grid existing controls when crossing the responsive breakpoint."""

        layout = overlay_layout(width)
        if layout == self._layout:
            return False
        self._layout = layout
        for widget in (
            self.category_menu,
            self.overlay_menu,
            self.start_button,
            self.fade_button,
            self.stop_button,
            self.manage_button,
        ):
            widget.grid_forget()
        if self._favorite_hosts is None:
            for button in self.favorite_buttons:
                button.grid_forget()
        if layout == OverlayLayout.WIDE:
            self.channel_label.grid()
            self.category_menu.grid(row=0, column=0, padx=(0, 6), sticky="ew")
            self.overlay_menu.grid(row=0, column=1, padx=6, sticky="ew")
            self.start_button.grid(row=0, column=2, padx=(6, 0))
            self.manage_button.grid(row=0, column=3, padx=(6, 0))
            self.status_label.grid(row=0, column=0, sticky="ew")
            self.fade_button.grid(row=0, column=1, padx=6)
            self.stop_button.grid(row=0, column=2)
            self.details_label.grid(row=1, column=0, columnspan=3, pady=(2, 0), sticky="ew")
            if self._favorite_hosts is None:
                for index, button in enumerate(self.favorite_buttons):
                    self._favorites.grid_columnconfigure(index, weight=1)
                    button.grid(row=0, column=index, padx=3, sticky="ew")
        else:
            self.channel_label.grid_remove()
            self.category_menu.grid(row=0, column=0, padx=(0, 6), sticky="ew")
            self.overlay_menu.grid(row=0, column=1, sticky="ew")
            self.start_button.grid(row=1, column=0, columnspan=2, pady=(6, 0), sticky="ew")
            self.manage_button.grid(
                row=2,
                column=0,
                columnspan=2,
                pady=(6, 0),
                sticky="ew",
            )
            self.status_label.grid(row=0, column=0, columnspan=2, sticky="ew")
            self.details_label.grid(row=1, column=0, columnspan=2, pady=(2, 0), sticky="ew")
            self.fade_button.grid(row=2, column=0, padx=(0, 3), pady=(6, 0), sticky="ew")
            self.stop_button.grid(row=2, column=1, padx=(3, 0), pady=(6, 0), sticky="ew")
            if self._favorite_hosts is None:
                for index, button in enumerate(self.favorite_buttons):
                    column = index % 3
                    self._favorites.grid_columnconfigure(column, weight=1)
                    button.grid(row=index // 3, column=column, padx=3, pady=3, sticky="ew")
        self._gate.invalidate()
        return True

    @staticmethod
    def _status_text(model: OverlayViewModel) -> str:
        labels = {
            OverlayState.READY: "Bereit",
            OverlayState.PREPARING: "Wird vorbereitet",
            OverlayState.FADING_IN: "Wird eingeblendet",
            OverlayState.PLAYING: "Spielt",
            OverlayState.FADING_OUT: "Wird ausgeblendet",
            OverlayState.FINISHED: "Beendet",
            OverlayState.ERROR: "Fehler",
        }
        name = abbreviated_name(model.active_name or model.selected_name)
        if model.state == OverlayState.READY and not name:
            return "Keine Auswahl"
        text = f"● {labels[model.state]}: {name}" if name else labels[model.state]
        if model.progress_known:
            assert model.position_ms is not None and model.duration_ms is not None
            text += f"  {model.position_ms // 1000:02d}s / {model.duration_ms // 1000:02d}s"
        if model.error_message:
            text += f" · {model.error_message}"
        return text

    def close(self) -> None:
        """Release tooltip callbacks owned by the persistent favorite widgets."""

        for target in self._tooltip_targets:
            target.close()
        self._tooltip_manager.close()

    def _configure_focus_order(self) -> None:
        widgets = self.focus_widgets
        for index, widget in enumerate(widgets):
            focus_target = getattr(widget, "_canvas", widget)
            try:
                focus_target.configure(takefocus=True)
            except (AttributeError, TypeError):
                pass
            next_widget = widgets[(index + 1) % len(widgets)]
            previous_widget = widgets[(index - 1) % len(widgets)]
            widget.bind(
                "<Tab>",
                lambda _event, target=next_widget: self._focus_and_break(target),
                add="+",
            )
            widget.bind(
                "<Shift-Tab>",
                lambda _event, target=previous_widget: self._focus_and_break(target),
                add="+",
            )

    @staticmethod
    def _focus_and_break(widget: Any) -> str:
        getattr(widget, "_canvas", widget).focus_set()
        return "break"
