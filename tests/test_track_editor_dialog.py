"""Display-independent lifecycle tests for the phase-A track editor."""

from typing import Any, cast

from pytest import MonkeyPatch

from party_player.ui import dialogs
from party_player.ui.dialogs import CuePointDialog


class _Controller:
    def __init__(self) -> None:
        self.preview_stops = 0
        self.analysis_cancels = 0
        self.active_preview_count = 0

    def stop_preview(self) -> None:
        self.preview_stops += 1

    def cancel_analysis(self) -> None:
        self.analysis_cancels += 1


class _Tooltip:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class _DialogDouble:
    def __init__(self) -> None:
        self._controller = _Controller()
        self._editor_controller = _EditorController()
        self._closed = False
        self.closed_callbacks = 0
        self._on_closed = self._closed_callback
        self.destroyed = False
        self.grab_released = False
        self._path_tooltip = _Tooltip()

    def _closed_callback(self) -> None:
        self.closed_callbacks += 1

    def grab_release(self) -> None:
        self.grab_released = True

    def destroy(self) -> None:
        self.destroyed = True

    def _finish(self) -> None:
        CuePointDialog._finish(cast(Any, self))


class _Entry:
    def __init__(self) -> None:
        self.value = ""

    def delete(self, _start: int, _end: str) -> None:
        self.value = ""

    def insert(self, _index: int, value: str) -> None:
        self.value = value


class _Label:
    def __init__(self) -> None:
        self.text = ""

    def configure(self, *, text: str) -> None:
        self.text = text


class _Button(_Label):
    def __init__(self) -> None:
        super().__init__()
        self.state = "normal"

    def configure(self, *, text: str, state: str = "normal") -> None:
        self.text = text
        self.state = state


class _EditorController:
    def __init__(self) -> None:
        self.events: list[str] = []

    def automatic_suggestion(self, _model: object) -> object:
        from party_player.controllers.track_editor_controller import TrackEditorChanges

        return TrackEditorChanges(1.25, 178.5, 6.0)

    def record_event(self, operation: str) -> None:
        self.events.append(operation)


class _AdoptionDialogDouble:
    def __init__(self) -> None:
        self._editor_controller = _EditorController()
        self._view_model = object()
        self._cue_in = _Entry()
        self._cue_out = _Entry()
        self._fade = _Entry()
        self._analysis_status = _Label()
        self._analysis_details = _Label()
        self._error = _Label()

    def _set_analysis_status(self, message: str) -> None:
        self._analysis_status.configure(text=message)

    def _replace(self, entry: _Entry, value: str) -> None:
        CuePointDialog._replace(entry, value)

    def _is_active(self) -> bool:
        return CuePointDialog._is_active(cast(Any, self))


class _SaveCompletionDialogDouble:
    def __init__(self) -> None:
        self._closed = False
        self._controller = _Controller()
        self._editor_controller = _EditorController()
        self._save_had_changes = True
        self._on_saved_models: list[object] = []
        self._on_saved = self._on_saved_models.append
        self.finished = 0
        self.shown_states: list[object] = []

    def _is_active(self) -> bool:
        return True

    def _show_sources(self, state: object) -> None:
        self.shown_states.append(state)

    def _finish(self) -> None:
        self.finished += 1


def test_window_close_matches_cancel_and_releases_preview_resources() -> None:
    dialog = _DialogDouble()

    CuePointDialog._cancel(cast(Any, dialog))

    assert dialog._controller.preview_stops == 1
    assert dialog._controller.analysis_cancels == 1
    assert dialog.grab_released
    assert dialog.destroyed
    assert dialog.closed_callbacks == 1


def test_finish_is_idempotent_for_late_close_callbacks() -> None:
    dialog = _DialogDouble()
    tooltip = dialog._path_tooltip

    CuePointDialog._finish(cast(Any, dialog))
    CuePointDialog._finish(cast(Any, dialog))

    assert dialog.closed_callbacks == 1
    assert dialog.destroyed
    assert tooltip.closed
    assert dialog._path_tooltip is None


def test_adopting_analysis_only_stages_values_until_save() -> None:
    dialog = _AdoptionDialogDouble()

    CuePointDialog._adopt_analysis(cast(Any, dialog))

    assert dialog._cue_in.value == "1.250"
    assert dialog._cue_out.value == "178.500"
    assert dialog._fade.value == "6.000"
    assert "erst mit „Speichern“" in dialog._analysis_status.text
    assert dialog._error.text == ""


def test_second_save_click_is_ignored_while_persistence_is_running() -> None:
    dialog = cast(Any, object.__new__(_AdoptionDialogDouble))
    dialog._saving = True

    CuePointDialog._save(dialog)

    assert dialog._saving


def test_persistence_failure_keeps_dialog_open_and_reenables_save() -> None:
    dialog = cast(Any, object.__new__(_AdoptionDialogDouble))
    dialog._closed = False
    dialog._saving = True
    dialog._editor_controller = _EditorController()
    dialog._save_button = _Button()
    dialog._error = _Label()
    dialog.winfo_exists = lambda: True

    CuePointDialog._save_failed(dialog, RuntimeError("Datenbank gesperrt"))

    assert not dialog._saving
    assert dialog._save_button.state == "normal"
    assert dialog._save_button.text == "Speichern"
    assert "Datenbank gesperrt" in dialog._error.text


def test_discarding_analysis_is_only_staged_until_save() -> None:
    dialog = _AdoptionDialogDouble()
    dialog._view_model = type(
        "Model",
        (),
        {"analysis_state": "SUGGESTED"},
    )()
    dialog._discard_automatic = False

    CuePointDialog._discard_analysis(cast(Any, dialog))

    assert dialog._discard_automatic
    assert "erst mit „Speichern“" in dialog._analysis_details.text
    assert dialog._analysis_status.text == "Verwerfen lokal vorgemerkt."


def test_successful_save_stops_preview_and_analysis_before_close() -> None:
    dialog = _SaveCompletionDialogDouble()
    view_model = type("Model", (), {"cue": object()})()

    CuePointDialog._save_completed(cast(Any, dialog), cast(Any, view_model))

    assert dialog._controller.preview_stops == 1
    assert dialog._controller.analysis_cancels == 1
    assert dialog._on_saved_models == [view_model]
    assert dialog.finished == 1


def test_destroyed_dialog_is_inactive_even_if_tk_lookup_raises() -> None:
    dialog = cast(Any, object.__new__(_AdoptionDialogDouble))
    dialog._closed = False

    def missing_window() -> bool:
        raise RuntimeError("application has been destroyed")

    dialog.winfo_exists = missing_window

    assert not CuePointDialog._is_active(dialog)


def test_placeholder_tab_is_built_lazily_and_only_once(monkeypatch: MonkeyPatch) -> None:
    packed: list[tuple[str, str]] = []

    class Tabs:
        selected = "Lautheit"

        def get(self) -> str:
            return self.selected

        def tab(self, name: str) -> str:
            return name

    class Label:
        def __init__(self, parent: str, *, text: str, **_kwargs: object) -> None:
            self.parent = parent
            self.text = text

        def pack(self, **_kwargs: object) -> None:
            packed.append((self.parent, self.text))

    monkeypatch.setattr(dialogs.ctk, "CTkLabel", Label)
    dialog = cast(Any, object.__new__(_AdoptionDialogDouble))
    dialog._tabs = Tabs()
    dialog._lazy_tabs_built = {"Cue"}

    CuePointDialog._tab_changed(dialog)
    CuePointDialog._tab_changed(dialog)

    assert len(packed) == 1
    assert packed[0][0] == "Lautheit"
    assert "schreibgeschützt" in packed[0][1]
    assert dialog._lazy_tabs_built == {"Cue", "Lautheit"}
