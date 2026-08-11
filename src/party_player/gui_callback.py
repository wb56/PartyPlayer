"""Common timing wrapper for application-owned GUI callbacks."""

from collections.abc import Callable
from functools import wraps
from typing import ParamSpec, TypeVar

from party_player.performance_monitor import PerformanceMonitor
from party_player.gui_heartbeat_watchdog import GuiCallbackState

P = ParamSpec("P")
T = TypeVar("T")


def measured_gui_callback(
    performance: PerformanceMonitor,
    name: str,
    callback: Callable[P, T],
    *,
    context: dict[str, object] | None = None,
    callback_state: GuiCallbackState | None = None,
) -> Callable[P, T]:
    """Wrap one application-owned Tk callback with a 25-ms slow-operation timer.

    ``name`` contains the callback category, for example ``after.status_tick`` or
    ``command.queue.deck_a``. Function metadata is retained so scheduled callback
    diagnostics and tests still expose meaningful names.
    """

    @wraps(callback)
    def wrapped(*args: P.args, **kwargs: P.kwargs) -> T:
        full_name = f"gui_callback.{name}"
        if callback_state is not None:
            callback_state.mark_started(full_name)
        try:
            with performance.measure(
                full_name,
                warning_threshold_ms=25.0,
                context=context,
            ):
                return callback(*args, **kwargs)
        finally:
            if callback_state is not None:
                callback_state.mark_completed(full_name)

    return wrapped
