"""Tests for the GUI-independent queue editing controller."""

from unittest.mock import Mock, call

from party_player.controllers.queue_controller import QueueController


def test_routes_basic_edits_to_queue_service() -> None:
    service = Mock()
    controller = QueueController(service)

    controller.add(7)
    controller.add(8, source="playlist")
    controller.remove(3)
    controller.move(4, -1)
    controller.move(5, 1)
    controller.move_to_top(6)
    controller.move_to_end(7)
    controller.set_priority(8, 900)
    controller.toggle_lock(9)

    assert service.add.call_args_list == [
        call(7),
        call(8, source="playlist"),
    ]
    service.remove.assert_called_once_with(3)
    service.move_up.assert_called_once_with(4)
    service.move_down.assert_called_once_with(5)
    service.move_to_top.assert_called_once_with(6)
    service.move_to_end.assert_called_once_with(7)
    service.set_priority.assert_called_once_with(8, 900)
    service.toggle_lock.assert_called_once_with(9)


def test_routes_bulk_and_terminal_edits_to_queue_service() -> None:
    service = Mock()
    service.shuffle_waiting.return_value = 12
    controller = QueueController(service)

    controller.clear_waiting()
    controller.clear_complete()
    assert controller.shuffle_waiting() == 12
    controller.mark_played(1)
    controller.mark_skipped(2, "Operator")
    controller.retry(3)
    controller.reset_played(4)

    service.clear_waiting.assert_called_once_with()
    service.clear_complete.assert_called_once_with()
    service.mark_played.assert_called_once_with(1)
    service.mark_skipped.assert_called_once_with(2, "Operator")
    service.retry.assert_called_once_with(3)
    service.reset_played.assert_called_once_with(4)
