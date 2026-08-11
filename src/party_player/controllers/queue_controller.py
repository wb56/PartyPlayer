"""GUI-independent queue editing operations."""

from party_player.enums import QueueSource
from party_player.models import QueueEntry
from party_player.queue_service import QueueService


class QueueController:
    """Provide one application boundary for ordinary queue mutations."""

    def __init__(self, service: QueueService) -> None:
        self._service = service

    def add(
        self,
        track_id: int,
        *,
        source: QueueSource | str = QueueSource.MANUAL,
    ) -> QueueEntry:
        if source == QueueSource.MANUAL:
            return self._service.add(track_id)
        return self._service.add(track_id, source=source)

    def remove(self, queue_id: int) -> None:
        self._service.remove(queue_id)

    def move(self, queue_id: int, direction: int) -> None:
        if direction < 0:
            self._service.move_up(queue_id)
        else:
            self._service.move_down(queue_id)

    def move_to_top(self, queue_id: int) -> None:
        self._service.move_to_top(queue_id)

    def move_to_end(self, queue_id: int) -> None:
        self._service.move_to_end(queue_id)

    def set_priority(self, queue_id: int, priority: int) -> None:
        self._service.set_priority(queue_id, priority)

    def toggle_lock(self, queue_id: int) -> None:
        self._service.toggle_lock(queue_id)

    def clear_waiting(self) -> None:
        self._service.clear_waiting()

    def clear_complete(self) -> None:
        self._service.clear_complete()

    def shuffle_waiting(self) -> int:
        return self._service.shuffle_waiting()

    def mark_played(self, queue_id: int) -> None:
        self._service.mark_played(queue_id)

    def mark_skipped(self, queue_id: int, reason: str | None = None) -> None:
        self._service.mark_skipped(queue_id, reason)

    def retry(self, queue_id: int) -> None:
        self._service.retry(queue_id)

    def reset_played(self, queue_id: int) -> None:
        self._service.reset_played(queue_id)
