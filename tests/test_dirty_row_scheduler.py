from collections.abc import Callable

import pytest

from party_player.ui.dirty_row_scheduler import DirtyRowScheduler


class ManualScheduler:
    def __init__(self) -> None:
        self.callbacks: list[Callable[[], None]] = []
        self.delays: list[int] = []

    def schedule(self, delay: int, callback: Callable[[], None]) -> object:
        self.delays.append(delay)
        self.callbacks.append(callback)
        return callback

    def run_next(self) -> None:
        self.callbacks.pop(0)()


def test_dirty_rows_are_processed_with_item_budget() -> None:
    gui = ManualScheduler()
    rendered: list[int] = []
    rows = DirtyRowScheduler(gui.schedule, rendered.append, max_rows=3, budget_ms=100.0)

    rows.mark(list(range(8)))
    gui.run_next()

    assert rendered == [0, 1, 2]
    assert rows.pending_count == 5
    assert len(gui.callbacks) == 1


def test_slow_render_yields_after_one_row() -> None:
    gui = ManualScheduler()
    now = [0.0]
    rendered: list[int] = []

    def render(index: int) -> None:
        rendered.append(index)
        now[0] += 0.020

    rows = DirtyRowScheduler(
        gui.schedule,
        render,
        max_rows=10,
        budget_ms=8.0,
        clock=lambda: now[0],
    )
    rows.mark([0, 1, 2])

    gui.run_next()

    assert rendered == [0]
    assert rows.pending_count == 2
    assert len(gui.callbacks) == 1


def test_repeated_dirty_marks_are_coalesced() -> None:
    gui = ManualScheduler()
    rendered: list[int] = []
    rows = DirtyRowScheduler(gui.schedule, rendered.append)

    rows.mark([1, 2])
    rows.mark([2, 1, 3])
    gui.run_next()

    assert rendered == [1, 2, 3]


def test_replaced_render_batch_ignores_obsolete_callback() -> None:
    gui = ManualScheduler()
    rendered: list[int] = []
    rows = DirtyRowScheduler(gui.schedule, rendered.append)
    rows.replace([1, 2, 3])
    rows.replace([7, 8])

    gui.run_next()
    assert rendered == []
    gui.run_next()
    assert rendered == [7, 8]


def test_chunk_and_wall_clock_statistics_are_reported() -> None:
    gui = ManualScheduler()
    now = [0.0]
    chunks: list[tuple[float, int]] = []
    completed = []

    def render(_index: int) -> None:
        now[0] += 0.002

    rows = DirtyRowScheduler(
        gui.schedule,
        render,
        max_rows=2,
        budget_ms=8.0,
        clock=lambda: now[0],
        on_chunk=lambda duration, count: chunks.append((duration, count)),
        on_complete=completed.append,
    )
    rows.replace([0, 1, 2])
    gui.run_next()
    now[0] += 0.010
    gui.run_next()

    assert [count for _duration, count in chunks] == [2, 1]
    assert completed[0].chunk_count == 2
    assert completed[0].maximum_rows_per_chunk == 2
    assert completed[0].wall_clock_duration_ms == pytest.approx(16.0)


def test_creation_and_rebinding_use_different_row_budgets() -> None:
    creation_gui = ManualScheduler()
    created: list[int] = []
    creation_rows = DirtyRowScheduler(
        creation_gui.schedule,
        created.append,
        max_rows=5,
        is_creation=lambda _index: True,
        max_create_rows=1,
    )
    creation_rows.replace([0, 1, 2])
    creation_gui.run_next()

    assert created == [0]
    assert creation_gui.delays[-1] == 10

    rebind_gui = ManualScheduler()
    rebound: list[int] = []
    rebind_rows = DirtyRowScheduler(
        rebind_gui.schedule,
        rebound.append,
        max_rows=5,
        is_creation=lambda _index: False,
    )
    rebind_rows.replace([0, 1, 2, 3, 4, 5])
    rebind_gui.run_next()

    assert rebound == [0, 1, 2, 3, 4]


def test_creation_and_binding_can_be_split_across_chunks() -> None:
    gui = ManualScheduler()
    rendered: list[tuple[int, bool]] = []
    created: set[int] = set()

    def render(index: int) -> None:
        is_creation = index not in created
        rendered.append((index, is_creation))
        created.add(index)

    rows = DirtyRowScheduler(
        gui.schedule,
        render,
        max_rows=5,
        is_creation=lambda index: index not in created,
        split_creation_and_bind=True,
    )
    rows.replace([0])
    gui.run_next()

    assert rendered == [(0, True)]
    assert rows.pending_count == 1

    gui.run_next()

    assert rendered == [(0, True), (0, False)]
    assert rows.pending_count == 0


def test_scheduled_chunk_has_a_diagnostic_name() -> None:
    gui = ManualScheduler()
    rows = DirtyRowScheduler(
        gui.schedule,
        lambda _index: None,
        callback_name="catalog_render_chunk",
    )

    rows.replace([0])

    assert gui.callbacks[0].__name__ == "catalog_render_chunk"
