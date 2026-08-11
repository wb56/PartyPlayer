"""Generation guards for asynchronous dependency dialogs without real Tk widgets."""

from queue import SimpleQueue
from typing import Any, cast

from party_player.ui.external_programs_dialog import ExternalProgramsDialog
from party_player.ui.first_run_dialog import FirstRunSetupDialog


class FakeButton:
    def __init__(self) -> None:
        self.configurations: list[dict[str, object]] = []

    def configure(self, **values: object) -> None:
        self.configurations.append(values)


def test_first_run_dialog_discards_older_worker_result() -> None:
    dialog = cast(Any, object.__new__(FirstRunSetupDialog))
    dialog._closed = False
    dialog._generation = 2
    dialog._results = SimpleQueue()
    dialog._results.put((1, RuntimeError("stale")))
    dialog._check_button = FakeButton()
    scheduled: list[tuple[int, object]] = []
    dialog.after = lambda delay, callback: scheduled.append((delay, callback))

    dialog._poll_recheck()

    assert dialog._check_button.configurations == []
    assert scheduled and scheduled[0][0] == 0


def test_external_programs_dialog_discards_result_after_generation_change() -> None:
    dialog = cast(Any, object.__new__(ExternalProgramsDialog))
    dialog._closed = False
    dialog._generation = 3
    dialog._running = True
    dialog._results = SimpleQueue()
    dialog._results.put((2, RuntimeError("stale")))
    dialog._check_button = FakeButton()
    dialog.after = lambda _delay, _callback: None

    dialog._poll()

    assert dialog._running
    assert dialog._check_button.configurations == []
