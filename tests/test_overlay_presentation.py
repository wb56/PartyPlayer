from __future__ import annotations

import pytest

from party_player.ui.overlay_presentation import (
    FavoritePadViewModel,
    OVERLAY_FOCUS_ORDER,
    OverlayLayout,
    OverlayRenderGate,
    OverlayState,
    OverlayViewModel,
    abbreviated_name,
    advanced_cue_visible,
    collapsed_overlay_stop_visible,
    ducking_switch_text,
    favorite_pad_text,
    favorite_shortcut_text,
    format_cue_time,
    mixer_overlay_header_text,
    normalized_favorite_pads,
    favorite_runtime_marker,
    favorite_position_from_shortcut,
    overlay_actions,
    overlay_details_text,
    overlay_layout,
    overlay_shortcut_allowed,
    overlay_time_text,
    parse_cue_time,
)


def test_overlay_layout_stacks_controls_below_minimum_width() -> None:
    assert overlay_layout(939) == OverlayLayout.COMPACT
    assert overlay_layout(940) == OverlayLayout.WIDE
    assert overlay_layout(-1) == OverlayLayout.COMPACT


def test_overlay_focus_order_keeps_safety_actions_before_favorites() -> None:
    assert OVERLAY_FOCUS_ORDER[:5] == (
        "category",
        "overlay",
        "start",
        "fade_out",
        "stop",
    )
    assert OVERLAY_FOCUS_ORDER[-1] == "manage"
    assert len(OVERLAY_FOCUS_ORDER) == len(set(OVERLAY_FOCUS_ORDER))
    assert OVERLAY_FOCUS_ORDER.index("fade_out") < OVERLAY_FOCUS_ORDER.index("stop")
    assert OVERLAY_FOCUS_ORDER.index("stop") < OVERLAY_FOCUS_ORDER.index("favorite_1")


def test_long_overlay_name_is_shortened_without_losing_word_normalization() -> None:
    assert abbreviated_name("  Ein   sehr langer Begrüßungs-Jingle  ", maximum=18) == (
        "Ein sehr langer B…"
    )
    assert abbreviated_name("Tusch", maximum=18) == "Tusch"


def test_render_gate_skips_identical_view_models_and_can_be_invalidated() -> None:
    gate = OverlayRenderGate()
    model = OverlayViewModel(state=OverlayState.PLAYING, active_name="Begrüßung")

    assert gate.changed(model)
    assert not gate.changed(model)
    assert gate.changed(OverlayViewModel(state=OverlayState.FADING_OUT, active_name="Begrüßung"))

    gate.invalidate()
    assert gate.changed(OverlayViewModel(state=OverlayState.FADING_OUT, active_name="Begrüßung"))


def test_render_gate_stays_constant_during_many_identical_overlay_ticks() -> None:
    gate = OverlayRenderGate()
    model = OverlayViewModel(
        state=OverlayState.PLAYING,
        active_name="Applaus",
        selected_name="Applaus",
        position_ms=5_000,
        duration_ms=20_000,
    )

    assert gate.changed(model)
    assert sum(gate.changed(model) for _ in range(1_000)) == 0


def test_progress_requires_known_positive_duration_and_position() -> None:
    assert OverlayViewModel(duration_ms=8_000, position_ms=3_000).progress_known
    assert not OverlayViewModel(duration_ms=None, position_ms=3_000).progress_known
    assert not OverlayViewModel(duration_ms=0, position_ms=0).progress_known
    assert not OverlayViewModel(duration_ms=8_000, position_ms=None).progress_known


def test_overlay_detail_text_contains_selection_volume_progress_and_remaining_time() -> None:
    model = OverlayViewModel(
        selected_name="Begrüßung",
        duration_ms=75_000,
        position_ms=7_000,
        volume_percent=75,
    )

    assert overlay_details_text(model) == (
        "Auswahl: Begrüßung · Lautstärke: 75 % · 00:07 / 01:15 · Rest: 01:08"
    )
    assert overlay_time_text(125_999) == "02:05"


def test_overlay_detail_text_falls_back_to_elapsed_time_without_duration() -> None:
    assert overlay_details_text(OverlayViewModel(position_ms=7_000)) == "Laufzeit: 00:07"


def test_favorite_runtime_markers_distinguish_each_non_animated_phase() -> None:
    assert favorite_runtime_marker(OverlayState.PREPARING) == "◌"
    assert favorite_runtime_marker(OverlayState.FADING_IN) == "◒"
    assert favorite_runtime_marker(OverlayState.PLAYING) == "●"
    assert favorite_runtime_marker(OverlayState.FADING_OUT) == "◐"
    assert favorite_runtime_marker(OverlayState.READY) == ""


def test_favorite_pad_text_is_limited_to_two_lines_without_category() -> None:
    label = favorite_pad_text(
        3,
        "Ein außergewöhnlich langer Applaus mit Publikum",
        marker="●",
        maximum_name_length=20,
    )

    assert label == "● 3\nEin außergewöhnlich…"
    assert len(label.splitlines()) == 2
    assert "Publikum" not in label


def test_ducking_switch_text_is_explicit_only_for_editor_state() -> None:
    assert ducking_switch_text(True) == "Musikabsenkung: aktiv"
    assert ducking_switch_text(False) == "Musikabsenkung: aus"


def test_favorite_shortcut_text_tracks_only_the_six_visible_pads() -> None:
    assert favorite_shortcut_text("1") == "Strg+1"
    assert favorite_shortcut_text("6") == "Strg+6"
    assert favorite_shortcut_text("Keiner") == "—"
    assert favorite_shortcut_text("7") == "—"


def test_advanced_cue_section_opens_for_any_non_default_cue() -> None:
    assert not advanced_cue_visible(0, None)
    assert advanced_cue_visible(250, None)
    assert advanced_cue_visible(0, 5_000)


def test_cue_editor_formats_and_parses_minute_second_values() -> None:
    assert format_cue_time(0) == "00:00"
    assert format_cue_time(125_999) == "02:05"
    assert format_cue_time(None) == ""
    assert parse_cue_time("02:05") == 125_000
    assert parse_cue_time("", optional=True) is None


@pytest.mark.parametrize("value", ["", "2", "2:5", "02:60", "x:10"])
def test_invalid_cue_editor_values_are_rejected(value: str) -> None:
    with pytest.raises(ValueError):
        parse_cue_time(value)


def test_collapsed_mixer_keeps_active_overlay_and_stop_visible() -> None:
    assert (
        mixer_overlay_header_text(
            expanded=False,
            state=OverlayState.PLAYING,
            name="Tusch",
        )
        == "Jingle: Tusch · aktiv"
    )
    assert collapsed_overlay_stop_visible(
        expanded=False,
        state=OverlayState.PLAYING,
    )
    assert not collapsed_overlay_stop_visible(
        expanded=True,
        state=OverlayState.PLAYING,
    )
    assert (
        mixer_overlay_header_text(
            expanded=False,
            state=OverlayState.READY,
            name="",
        )
        == "Mixer einblenden ▼"
    )


def test_favorite_pad_model_keeps_stable_metadata_and_missing_state() -> None:
    pad = FavoritePadViewModel(
        name="Applaus",
        category="Publikum",
        ducking_db=-8.0,
        shortcut="Ctrl+1",
        missing_file=True,
        enabled=False,
    )

    assert pad.name == "Applaus"
    assert pad.missing_file
    assert not pad.enabled


def test_favorite_pad_normalization_covers_empty_and_six_assigned_slots() -> None:
    empty = normalized_favorite_pads(())
    assigned = tuple(
        FavoritePadViewModel(name=f"Jingle {position}", shortcut=f"Ctrl+{position}")
        for position in range(1, 7)
    )

    assert len(empty) == 6
    assert all(not pad.name for pad in empty)
    assert normalized_favorite_pads(assigned) == assigned
    assert normalized_favorite_pads((*assigned, FavoritePadViewModel(name="Zu viel"))) == assigned


class CTkEntry:
    def __init__(self, master: object | None = None) -> None:
        self.master = master


class InternalWidget:
    def __init__(self, master: object | None = None) -> None:
        self.master = master


class CTkButton:
    master = None


def test_favorite_shortcuts_are_blocked_for_entries_and_dialogs() -> None:
    assert not overlay_shortcut_allowed(CTkEntry(), dialog_active=False)
    assert not overlay_shortcut_allowed(
        InternalWidget(master=CTkEntry()),
        dialog_active=False,
    )
    assert not overlay_shortcut_allowed(CTkButton(), dialog_active=True)
    assert overlay_shortcut_allowed(CTkButton(), dialog_active=False)


def test_only_control_one_through_six_resolve_to_favorites() -> None:
    assert favorite_position_from_shortcut("1", True) == 1
    assert favorite_position_from_shortcut("6", True) == 6
    assert favorite_position_from_shortcut("7", True) is None
    assert favorite_position_from_shortcut("1", False) is None


def test_action_matrix_keeps_stop_available_during_prepare_and_fade_out() -> None:
    preparing = overlay_actions(
        OverlayViewModel(state=OverlayState.PREPARING, selected_name="Tusch")
    )
    assert not preparing.start_enabled
    assert preparing.stop_enabled
    assert not preparing.selection_enabled

    fading = overlay_actions(OverlayViewModel(state=OverlayState.FADING_OUT, active_name="Tusch"))
    assert not fading.fade_out_enabled
    assert fading.stop_enabled


def test_active_overlay_only_offers_switch_for_another_selection() -> None:
    same = overlay_actions(
        OverlayViewModel(
            state=OverlayState.PLAYING,
            active_name="Tusch",
            selected_name="Tusch",
        )
    )
    assert not same.start_enabled

    other = overlay_actions(
        OverlayViewModel(
            state=OverlayState.PLAYING,
            active_name="Tusch",
            selected_name="Applaus",
        )
    )
    assert other.start_enabled
    assert other.start_label == "Wechseln"


def test_error_without_valid_selection_cannot_retry() -> None:
    unavailable = overlay_actions(OverlayViewModel(state=OverlayState.ERROR))
    retryable = overlay_actions(OverlayViewModel(state=OverlayState.ERROR, selected_name="Tusch"))

    assert not unavailable.start_enabled
    assert retryable.start_enabled
    assert retryable.start_label == "Erneut versuchen"
