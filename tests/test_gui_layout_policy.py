from party_player.ui.main_window import (
    MainWindow,
    _diagnostic_toggle_text,
    _automatic_help_text,
    _initial_catalog_pool_target,
    _main_layout_spacing,
    _optionmenu_changes,
    _queue_model_count,
    _queue_pool_size,
)
from party_player.presentation import Workspace


class Disposable:
    def __init__(self) -> None:
        self.dispose_count = 0

    def dispose(self) -> None:
        self.dispose_count += 1


class Closable:
    def close(self) -> None:
        pass


class GridDouble:
    def __init__(self) -> None:
        self.rows: dict[int, dict[str, int]] = {}

    def grid_rowconfigure(self, row: int, **values: int) -> None:
        self.rows[row] = values


class SplitControllerDouble:
    def __init__(self) -> None:
        self.saved: list[float] = []

    def set_workspace_catalog_ratio(self, ratio: float) -> None:
        self.saved.append(ratio)


class FocusDouble:
    def __init__(self) -> None:
        self.focus_count = 0

    def focus_set(self) -> None:
        self.focus_count += 1


def test_initial_catalog_pool_is_bounded_and_reuses_existing_rows() -> None:
    assert _initial_catalog_pool_target(50, 0) == 10
    assert _initial_catalog_pool_target(10, 0) == 10


def test_main_layout_spacing_uses_only_two_stable_size_classes() -> None:
    assert _main_layout_spacing(1180) == (8, 4, 6)
    assert _main_layout_spacing(1349) == (8, 4, 6)
    assert _main_layout_spacing(1350) == (16, 8, 8)
    assert _main_layout_spacing(1920) == (16, 8, 8)
    assert _initial_catalog_pool_target(50, 24) == 24


def test_diagnostic_disclosure_label_matches_expanded_state() -> None:
    assert _diagnostic_toggle_text(False) == "Diagnose und Analyse anzeigen ▼"
    assert _diagnostic_toggle_text(True) == "Diagnose und Analyse ausblenden ▲"


def test_automatic_help_explains_safe_queue_and_playback_controls() -> None:
    text = _automatic_help_text()

    for expected in (
        "Ersetzen",
        "Anhängen",
        "Vollständig abspielen",
        "ersten wartenden Titel",
        "Deck-Pause",
        "Crossfader",
        "Cue-Fallback",
    ):
        assert expected in text


def test_optionmenu_policy_skips_identical_state() -> None:
    state = (("A", "B"), "A")

    assert _optionmenu_changes(state, ["A", "B"], "A") == (False, False)
    assert _optionmenu_changes(state, ["A", "B"], "B") == (False, True)
    assert _optionmenu_changes(state, ["A", "C"], "A") == (True, False)


def test_queue_pool_tracks_height_but_never_exceeds_virtualization_limits() -> None:
    assert _queue_pool_size(1) == 10
    assert _queue_pool_size(400) == 14
    assert _queue_pool_size(5000) == 20
    assert _queue_model_count(10, 20) == 20
    assert _queue_model_count(20, 10) == 20


def test_workspace_split_keeps_both_lists_visible_and_persists_choice() -> None:
    window = object.__new__(MainWindow)
    center = GridDouble()
    controller = SplitControllerDouble()
    window._center_panel = center
    window._controller = controller

    window._set_workspace_split(0.8)

    assert center.rows[2] == {
        "weight": 80,
        "minsize": 80,
        "uniform": "list_workspace",
    }
    assert center.rows[9] == {
        "weight": 20,
        "minsize": 80,
        "uniform": "list_workspace",
    }
    assert controller.saved == [0.8]

    window._set_workspace_split(1.0, persist=False)
    assert window._workspace_catalog_ratio == 0.8
    assert controller.saved == [0.8]


def test_workspace_focus_moves_to_live_action_or_preparation_search() -> None:
    window = object.__new__(MainWindow)
    live = FocusDouble()
    search = FocusDouble()
    window._automatic_queue_button = live
    window._search = search

    window._focus_workspace(Workspace.LIVE)
    assert live.focus_count == 1
    assert search.focus_count == 0

    window._focus_workspace(Workspace.PREPARATION)
    assert live.focus_count == 1
    assert search.focus_count == 1


def test_queue_dispose_counts_destroyed_widgets_once() -> None:
    window = object.__new__(MainWindow)
    rows = (Disposable(), Disposable())
    window._scheduled_after_ids = set()
    window._static_tooltips = []
    window.deck_a = Disposable()
    window.deck_b = Disposable()
    window._catalog_rows = []
    window._queue_rows = list(rows)
    window._queue_tooltip_manager = Closable()
    window._cover_images = {}
    window._queue_lifecycle_counters = {"destroyed_widget_count": 0}
    window._render_counters = {"widgets_destroyed_total": 0}

    window._dispose_resources()
    window._dispose_resources()

    assert [row.dispose_count for row in rows] == [1, 1]
    assert window._queue_lifecycle_counters["destroyed_widget_count"] == 12
    assert window._render_counters["widgets_destroyed_total"] == 12
