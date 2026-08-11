"""Display-independent tests for asynchronous track-editor window guards."""

from typing import Any, cast

from party_player.ui.main_window import MainWindow


class _WindowDouble:
    def __init__(self, result: bool = True, error: Exception | None = None) -> None:
        self.result = result
        self.error = error

    def winfo_exists(self) -> bool:
        if self.error is not None:
            raise self.error
        return self.result


def test_track_editor_callback_accepts_live_window() -> None:
    window = _WindowDouble(True)

    assert MainWindow._window_is_alive(cast(Any, window))


def test_track_editor_callback_rejects_destroyed_tk_interpreter() -> None:
    window = _WindowDouble(error=RuntimeError("application has been destroyed"))

    assert not MainWindow._window_is_alive(cast(Any, window))
