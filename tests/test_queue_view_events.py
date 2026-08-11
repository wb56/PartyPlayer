from party_player.queue_view_events import (
    QueueViewEvent,
    QueueViewEventType,
    QueueViewRevision,
    queue_view_events,
)
from dataclasses import replace

from party_player.enums import QueueStatus
from party_player.models import QueueEntry


def test_old_queue_view_events_are_rejected() -> None:
    revisions = QueueViewRevision()

    assert revisions.accepts(QueueViewEvent(QueueViewEventType.ENTRY_STATUS_CHANGED, 7, 4))
    assert not revisions.accepts(QueueViewEvent(QueueViewEventType.ENTRY_CONTENT_CHANGED, 7, 3))
    assert revisions.current == 4


def test_same_revision_can_update_multiple_rows() -> None:
    revisions = QueueViewRevision()

    assert revisions.accepts(QueueViewEvent(QueueViewEventType.ENTRY_MOVED, 1, 8, 0))
    assert revisions.accepts(QueueViewEvent(QueueViewEventType.ENTRY_MOVED, 2, 8, 1))


def entry(queue_id: int, position: int, status: QueueStatus = QueueStatus.WAITING) -> QueueEntry:
    return QueueEntry(queue_id, 1, position, status)


def test_diff_classifies_add_remove_move_and_status_changes() -> None:
    first = entry(1, 1)
    second = entry(2, 2)
    updated_second = replace(second, position=1, status=QueueStatus.LOADED)
    third = entry(3, 2)

    events = queue_view_events([first, second], [updated_second, third], 12)

    assert {(event.event_type, event.queue_entry_id) for event in events} == {
        (QueueViewEventType.ENTRY_REMOVED, 1),
        (QueueViewEventType.ENTRY_ADDED, 3),
        (QueueViewEventType.ENTRY_MOVED, 2),
        (QueueViewEventType.ENTRY_CONTENT_CHANGED, 2),
    }
    assert {event.queue_revision for event in events} == {12}


def test_diff_emits_targeted_status_change() -> None:
    waiting = entry(7, 1)
    playing = replace(waiting, status=QueueStatus.PLAYING)

    events = queue_view_events([waiting], [playing], 2)

    assert events == (QueueViewEvent(QueueViewEventType.ENTRY_STATUS_CHANGED, 7, 2, 0),)
