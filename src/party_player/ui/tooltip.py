"""Small dependency-free tooltip for Tk and CustomTkinter widgets."""

import tkinter as tk
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class TooltipStatistics:
    current: int
    created_total: int
    destroyed_total: int


class Tooltip:
    """Display delayed help text near a widget without taking focus."""

    _instances_current = 0
    _instances_created = 0
    _instances_destroyed = 0

    def __init__(self, widget: Any, text: str, delay_ms: int = 500) -> None:
        self._widget = widget
        self._text = text
        self._delay_ms = delay_ms
        self._after_id: str | None = None
        self._window: tk.Toplevel | None = None
        self._closed = False
        type(self)._instances_current += 1
        type(self)._instances_created += 1
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress>", self._hide, add="+")

    def _schedule(self, _event: object | None = None) -> None:
        self._cancel_schedule()
        self._after_id = self._widget.after(self._delay_ms, self._show)

    def _show(self) -> None:
        self._after_id = None
        if self._window is not None or not self._widget.winfo_exists():
            return
        pointer_x, pointer_y = self._widget.winfo_pointerxy()
        window = tk.Toplevel(self._widget)
        window.wm_overrideredirect(True)
        window.wm_geometry(f"+{pointer_x + 12}+{pointer_y + 16}")
        label = tk.Label(
            window,
            text=self._text,
            justify="left",
            background="#fff4c2",
            foreground="#111111",
            relief="solid",
            borderwidth=1,
            padx=7,
            pady=4,
            font=("Segoe UI", 10),
        )
        label.pack()
        self._window = window

    def _hide(self, _event: object | None = None) -> None:
        self._cancel_schedule()
        if self._window is not None:
            self._window.destroy()
            self._window = None

    def _cancel_schedule(self) -> None:
        if self._after_id is not None:
            self._widget.after_cancel(self._after_id)
            self._after_id = None

    def set_text(self, text: str) -> None:
        """Update reused tooltip content without creating another binding."""
        if text == self._text:
            return
        self._hide()
        self._text = text

    def cancel(self) -> None:
        """Cancel callbacks and visible state while retaining widget bindings."""
        self._hide()

    def close(self) -> None:
        """Cancel and remove the tooltip before its widget is destroyed."""
        if self._closed:
            return
        self._hide()
        self._closed = True
        type(self)._instances_current -= 1
        type(self)._instances_destroyed += 1

    @classmethod
    def statistics(cls) -> TooltipStatistics:
        return TooltipStatistics(
            cls._instances_current,
            cls._instances_created,
            cls._instances_destroyed,
        )


class SharedTooltipTarget:
    """Mutable registration handle owned by one shared tooltip manager."""

    def __init__(self, manager: "SharedTooltipManager", widget: Any, text: str) -> None:
        self._manager = manager
        self._widget = widget
        self._text = text
        self._closed = False

    def text(self) -> str:
        return self._text

    def set_text(self, text: str) -> None:
        if text != self._text:
            self._manager.hide()
            self._text = text

    def cancel(self) -> None:
        self._manager.cancel(self._widget)

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._manager.unregister(self._widget)


class SharedTooltipManager:
    """Serve many widgets through one delayed callback and one tooltip window."""

    def __init__(self, delay_ms: int = 500) -> None:
        self._delay_ms = delay_ms
        self._targets: dict[Any, SharedTooltipTarget] = {}
        self._active_widget: Any | None = None
        self._after_id: str | None = None
        self._window: tk.Toplevel | None = None

    def register(self, widget: Any, text: str) -> SharedTooltipTarget:
        existing = self._targets.get(widget)
        if existing is not None:
            existing.set_text(text)
            return existing
        target = SharedTooltipTarget(self, widget, text)
        self._targets[widget] = target
        widget.bind("<Enter>", lambda _event, owner=widget: self._schedule(owner), add="+")
        widget.bind("<Leave>", lambda _event: self.hide(), add="+")
        widget.bind("<ButtonPress>", lambda _event: self.hide(), add="+")
        return target

    def _schedule(self, widget: Any) -> None:
        self.hide()
        self._active_widget = widget
        self._after_id = widget.after(self._delay_ms, self._show)

    def _show(self) -> None:
        self._after_id = None
        widget = self._active_widget
        target = self._targets.get(widget)
        if widget is None or target is None or not widget.winfo_exists():
            return
        pointer_x, pointer_y = widget.winfo_pointerxy()
        window = tk.Toplevel(widget)
        window.wm_overrideredirect(True)
        window.wm_geometry(f"+{pointer_x + 12}+{pointer_y + 16}")
        tk.Label(
            window,
            text=target.text(),
            justify="left",
            background="#fff4c2",
            foreground="#111111",
            relief="solid",
            borderwidth=1,
            padx=7,
            pady=4,
            font=("Segoe UI", 10),
        ).pack()
        self._window = window

    def cancel(self, widget: Any) -> None:
        if self._active_widget is widget:
            self.hide()

    def hide(self) -> None:
        widget = self._active_widget
        if self._after_id is not None and widget is not None:
            try:
                widget.after_cancel(self._after_id)
            except tk.TclError:
                pass
        self._after_id = None
        self._active_widget = None
        if self._window is not None:
            self._window.destroy()
            self._window = None

    def unregister(self, widget: Any) -> None:
        self.cancel(widget)
        self._targets.pop(widget, None)

    @property
    def registered_target_count(self) -> int:
        return len(self._targets)

    @property
    def window_count(self) -> int:
        return int(self._window is not None)

    def close(self) -> None:
        self.hide()
        self._targets.clear()
