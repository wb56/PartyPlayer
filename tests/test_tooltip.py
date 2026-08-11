from party_player.ui.tooltip import SharedTooltipManager, Tooltip


class FakeWidget:
    def bind(self, *_args: object, **_kwargs: object) -> None:
        pass


def test_tooltip_instance_counters_are_stable_after_close() -> None:
    before = Tooltip.statistics()
    tooltip = Tooltip(FakeWidget(), "Text")

    created = Tooltip.statistics()
    assert created.current == before.current + 1
    assert created.created_total == before.created_total + 1

    tooltip.close()
    tooltip.close()

    closed = Tooltip.statistics()
    assert closed.current == before.current
    assert closed.destroyed_total == before.destroyed_total + 1


def test_shared_tooltip_manager_registers_many_targets_without_many_windows() -> None:
    manager = SharedTooltipManager()
    targets = [manager.register(FakeWidget(), f"Text {index}") for index in range(80)]

    assert manager.registered_target_count == 80
    assert manager.window_count == 0
    targets[0].set_text("Geändert")
    for target in targets:
        target.close()
    assert manager.registered_target_count == 0


def test_shared_tooltip_manager_reuses_existing_widget_registration() -> None:
    manager = SharedTooltipManager()
    widget = FakeWidget()

    first = manager.register(widget, "Erster Text")
    for index in range(100):
        current = manager.register(widget, f"Status {index}")
        assert current is first

    assert manager.registered_target_count == 1
    assert manager.window_count == 0
    assert first.text() == "Status 99"
    manager.close()
    assert manager.registered_target_count == 0
