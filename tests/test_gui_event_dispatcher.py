from threading import Thread, current_thread

import pytest

from party_player.gui_event_dispatcher import (
    GuiEvent,
    GuiEventDispatcher,
    GuiEventQueueFull,
    GuiEventType,
)


def test_worker_can_publish_but_handler_runs_only_when_gui_processes() -> None:
    dispatcher = GuiEventDispatcher()
    handled: list[tuple[object, str]] = []
    event = GuiEvent(GuiEventType.COVER_READY, "cover", {"deck": "A"})
    worker = Thread(target=lambda: dispatcher.publish(event), name="test-worker")
    worker.start()
    worker.join()
    assert handled == []

    dispatcher.process_pending_events(
        lambda item: handled.append((item.payload, current_thread().name))
    )
    assert handled == [({"deck": "A"}, current_thread().name)]


def test_dispatcher_coalesces_and_honors_item_budget() -> None:
    dispatcher = GuiEventDispatcher(max_items_per_cycle=2)
    dispatcher.publish(GuiEvent(GuiEventType.IMPORT_PROGRESS, "import", 1, coalesce_key="progress"))
    dispatcher.publish(GuiEvent(GuiEventType.IMPORT_PROGRESS, "import", 2, coalesce_key="progress"))
    dispatcher.publish(GuiEvent(GuiEventType.QUEUE_CHANGED, "queue", "first"))
    dispatcher.publish(GuiEvent(GuiEventType.QUEUE_CHANGED, "queue", "second"))
    handled: list[object] = []

    assert dispatcher.process_pending_events(lambda event: handled.append(event.payload)) == 2
    assert handled == [2, "first"]
    assert dispatcher.statistics().pending == 1
    assert dispatcher.statistics().coalesced == 1
    assert dispatcher.statistics().published == 4
    assert dispatcher.statistics().processed == 2
    assert dispatcher.statistics().maximum_items_processed_per_cycle == 2


def test_full_queue_discards_coalescable_but_never_silently_loses_critical_event() -> None:
    dispatcher = GuiEventDispatcher(capacity=1)
    dispatcher.publish(GuiEvent(GuiEventType.QUEUE_CHANGED, "queue", 1))
    assert not dispatcher.publish(
        GuiEvent(GuiEventType.IMPORT_PROGRESS, "import", 2, coalesce_key="progress")
    )
    with pytest.raises(GuiEventQueueFull):
        dispatcher.publish(GuiEvent(GuiEventType.ERROR_MESSAGE, "error", "critical"))
    assert dispatcher.statistics().critical_overflow == 1
