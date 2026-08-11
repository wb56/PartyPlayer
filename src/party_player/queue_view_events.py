"""Revisioned events used by incremental queue views."""

from dataclasses import dataclass, fields
from enum import StrEnum
from collections.abc import Sequence

from party_player.models import QueueEntry


class QueueViewEventType(StrEnum):
    """Kinds of structural and incremental changes understood by a queue view."""

    ENTRY_ADDED = "queue_entry_added"
    ENTRY_REMOVED = "queue_entry_removed"
    ENTRY_MOVED = "queue_entry_moved"
    ENTRY_STATUS_CHANGED = "queue_entry_status_changed"
    ENTRY_CONTENT_CHANGED = "queue_entry_content_changed"
    SELECTION_CHANGED = "queue_selection_changed"
    PAGE_CHANGED = "queue_page_changed"
    RESET = "queue_reset"


@dataclass(frozen=True, slots=True)
class QueueViewEvent:
    """Identify one queue view change and the revision that produced it."""

    event_type: QueueViewEventType
    queue_entry_id: int | None
    queue_revision: int
    affected_index: int | None = None
    selected: bool | None = None


class QueueViewRevision:
    """Reject stale renderer events so old worker results cannot overwrite the view."""

    def __init__(self) -> None:
        """Start before the first published queue revision."""
        self.current = 0

    def accepts(self, event: QueueViewEvent) -> bool:
        """Accept current/newer revisions and reject stale renderer work."""
        if event.queue_revision < self.current:
            return False
        self.current = event.queue_revision
        return True


def queue_view_events(
    previous: Sequence[QueueEntry],
    current: Sequence[QueueEntry],
    revision: int,
) -> tuple[QueueViewEvent, ...]:
    """Describe one queue mutation without losing structural ordering changes."""
    previous_by_id = {entry.queue_id: entry for entry in previous}
    current_by_id = {entry.queue_id: entry for entry in current}
    previous_ids = [entry.queue_id for entry in previous]
    current_ids = [entry.queue_id for entry in current]

    added = [queue_id for queue_id in current_ids if queue_id not in previous_by_id]
    removed = [queue_id for queue_id in previous_ids if queue_id not in current_by_id]
    events: list[QueueViewEvent] = []
    for queue_id in removed:
        events.append(
            QueueViewEvent(
                QueueViewEventType.ENTRY_REMOVED,
                queue_id,
                revision,
                previous_ids.index(queue_id),
            )
        )
    for queue_id in added:
        events.append(
            QueueViewEvent(
                QueueViewEventType.ENTRY_ADDED,
                queue_id,
                revision,
                current_ids.index(queue_id),
            )
        )

    common_ids = current_by_id.keys() & previous_by_id.keys()
    for queue_id in current_ids:
        if queue_id not in common_ids:
            continue
        old = previous_by_id[queue_id]
        new = current_by_id[queue_id]
        old_index = previous_ids.index(queue_id)
        new_index = current_ids.index(queue_id)
        if old_index != new_index:
            events.append(
                QueueViewEvent(QueueViewEventType.ENTRY_MOVED, queue_id, revision, new_index)
            )
        if old == new:
            continue
        if old.status != new.status and _without_status(old) == _without_status(new):
            event_type = QueueViewEventType.ENTRY_STATUS_CHANGED
        else:
            event_type = QueueViewEventType.ENTRY_CONTENT_CHANGED
        events.append(QueueViewEvent(event_type, queue_id, revision, new_index))

    if not previous and not current:
        return ()
    if not events and previous_ids != current_ids:
        return (QueueViewEvent(QueueViewEventType.RESET, None, revision),)
    return tuple(events)


def _without_status(entry: QueueEntry) -> tuple[object, ...]:
    """Return comparable field values with status omitted."""
    return tuple(getattr(entry, field.name) for field in fields(entry) if field.name != "status")
