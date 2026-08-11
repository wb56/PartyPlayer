"""Abortable, budgeted dirty-row processing independent of concrete Tk widgets."""

from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from time import monotonic


@dataclass(frozen=True, slots=True)
class RenderBatchStatistics:
    """Wall-clock, active chunk and row-throughput values for one render batch.

    Wall-clock duration includes the intentional gaps between chunks. Chunk
    durations contain active GUI work only. This distinction prevents the
    scheduling call itself from being mistaken for the complete render duration.
    """

    wall_clock_duration_ms: float
    chunk_count: int
    maximum_chunk_duration_ms: float
    average_chunk_duration_ms: float
    maximum_rows_per_chunk: int
    average_rows_per_chunk: float
    maximum_gap_between_chunks_ms: float


class DirtyRowScheduler:
    """Render dirty rows in abortable chunks while yielding regularly to Tk.

    A generation token invalidates already queued callbacks when ``replace()`` is
    called. At most ``max_rows`` and ``budget_ms`` of active work are allowed per
    chunk. The non-zero inter-chunk delay gives heartbeat, status, playback display
    and user-input callbacks an opportunity to run.
    """

    def __init__(
        self,
        schedule: Callable[[int, Callable[[], None]], object],
        render: Callable[[int], None],
        *,
        max_rows: int = 3,
        budget_ms: float = 8.0,
        inter_chunk_delay_ms: int = 10,
        clock: Callable[[], float] = monotonic,
        on_chunk: Callable[[float, int], None] | None = None,
        on_complete: Callable[[RenderBatchStatistics], None] | None = None,
        callback_name: str = "dirty_row_flush",
        is_creation: Callable[[int], bool] | None = None,
        max_create_rows: int = 1,
        split_creation_and_bind: bool = False,
    ) -> None:
        """Configure scheduling budgets and optional diagnostic observers."""
        self._schedule = schedule
        self._render = render
        self._max_rows = max(1, max_rows)
        self._budget_ms = max(0.1, budget_ms)
        self._delay_ms = max(1, inter_chunk_delay_ms)
        self._clock = clock
        self._on_chunk = on_chunk
        self._on_complete = on_complete
        self._callback_name = callback_name
        self._is_creation = is_creation or (lambda _index: False)
        self._max_create_rows = max(1, max_create_rows)
        self._split_creation_and_bind = split_creation_and_bind
        self._dirty: deque[int] = deque()
        self._known: set[int] = set()
        self._scheduled = False
        self._generation = 0
        self._started_at: float | None = None
        self._last_chunk_at: float | None = None
        self._chunk_durations: list[float] = []
        self._chunk_rows: list[int] = []
        self._maximum_gap_ms = 0.0

    @property
    def pending_count(self) -> int:
        """Return the number of unique row indices still waiting to render."""
        return len(self._dirty)

    def replace(self, indices: list[int]) -> None:
        """Cancel an obsolete batch and schedule the newest complete render request."""
        self._generation += 1
        self._dirty.clear()
        self._known.clear()
        self._scheduled = False
        self._reset_statistics()
        self._add(indices)
        self._schedule_next(0)

    def mark(self, indices: list[int]) -> None:
        """Merge additional dirty indices into the active generation."""
        self._add(indices)
        if self._dirty and not self._scheduled:
            if self._started_at is None:
                self._reset_statistics()
            self._schedule_next(0)

    def _add(self, indices: list[int]) -> None:
        for index in indices:
            if index not in self._known:
                self._known.add(index)
                self._dirty.append(index)

    def _schedule_next(self, delay_ms: int) -> None:
        if not self._dirty:
            return
        self._scheduled = True
        # Close over the current generation. A callback already present in Tk's
        # queue becomes a cheap no-op after a newer full render replaces it.
        generation = self._generation

        def process_current_generation() -> None:
            self._process(generation)

        process_current_generation.__name__ = self._callback_name
        self._schedule(delay_ms, process_current_generation)

    def _process(self, generation: int) -> None:
        """Process one bounded chunk for the still-current render generation."""
        if generation != self._generation:
            return
        self._scheduled = False
        chunk_started = self._clock()
        if self._started_at is None:
            self._started_at = chunk_started
        if self._last_chunk_at is not None:
            self._maximum_gap_ms = max(
                self._maximum_gap_ms, (chunk_started - self._last_chunk_at) * 1000.0
            )
        self._last_chunk_at = chunk_started
        processed = 0
        created = 0
        while self._dirty and processed < self._max_rows:
            if processed and (self._clock() - chunk_started) * 1000.0 >= self._budget_ms:
                break
            index = self._dirty[0]
            creates_row = self._is_creation(index)
            if processed and creates_row and created >= self._max_create_rows:
                break
            index = self._dirty.popleft()
            self._known.remove(index)
            self._render(index)
            processed += 1
            created += int(creates_row)
            if creates_row and self._split_creation_and_bind:
                self._known.add(index)
                self._dirty.appendleft(index)
                break
        duration_ms = max(0.0, (self._clock() - chunk_started) * 1000.0)
        self._chunk_durations.append(duration_ms)
        self._chunk_rows.append(processed)
        if self._on_chunk is not None:
            self._on_chunk(duration_ms, processed)
        if self._dirty:
            self._schedule_next(self._delay_ms)
        else:
            self._complete()

    def _complete(self) -> None:
        """Aggregate one batch and notify the owning view exactly once."""
        finished = self._clock()
        started = self._started_at if self._started_at is not None else finished
        chunks = len(self._chunk_durations)
        rows = sum(self._chunk_rows)
        statistics = RenderBatchStatistics(
            (finished - started) * 1000.0,
            chunks,
            max(self._chunk_durations, default=0.0),
            sum(self._chunk_durations) / chunks if chunks else 0.0,
            max(self._chunk_rows, default=0),
            rows / chunks if chunks else 0.0,
            self._maximum_gap_ms,
        )
        self._started_at = None
        if self._on_complete is not None:
            self._on_complete(statistics)

    def _reset_statistics(self) -> None:
        self._started_at = None
        self._last_chunk_at = None
        self._chunk_durations = []
        self._chunk_rows = []
        self._maximum_gap_ms = 0.0
