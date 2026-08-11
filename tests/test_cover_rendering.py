from io import BytesIO
from types import SimpleNamespace

from PIL import Image

from party_player.performance_monitor import PerformanceMonitor
from party_player.gui_heartbeat_watchdog import GuiCallbackState
from party_player.ui import main_window
from party_player.ui.main_window import MainWindow


class FakeCoverLabel:
    def configure(self, **_values: object) -> None:
        pass


def test_cover_application_has_detailed_timings(monkeypatch) -> None:
    window = object.__new__(MainWindow)
    window._performance = PerformanceMonitor()
    window._callback_state = GuiCallbackState()
    window._cover_images = {}
    window.deck_a = SimpleNamespace(_cover=FakeCoverLabel())
    window.deck_b = SimpleNamespace(_cover=FakeCoverLabel())
    monkeypatch.setattr(main_window.ctk, "CTkImage", lambda **values: values)
    data = BytesIO()
    Image.new("RGB", (20, 20), "red").save(data, format="PNG")

    window.show_deck_cover("A", data.getvalue())

    timings = window._performance.statistics()
    assert "gui.cover_apply.total" in timings
    assert "gui.cover_apply.prepare_result" in timings
    assert "gui.cover_apply.create_tk_image" in timings
    assert "gui.cover_apply.configure_widget" in timings
    assert "gui.cover_apply.layout" in timings
    assert "gui.cover_apply.release_old_reference" in timings
