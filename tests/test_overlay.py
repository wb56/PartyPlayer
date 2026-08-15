from pathlib import Path

import pytest

from party_player.overlay import (
    OverlayDefinition,
    OverlayStateMachine,
    OverlayStatus,
    resolve_overlay,
)


def definition(path: str = "jingle.mp3") -> OverlayDefinition:
    return OverlayDefinition(1, "Begrüßung", path)


def test_new_overlay_does_not_lower_music_by_default() -> None:
    assert definition().ducking_db == 0.0


def test_resolve_overlay_applies_defaults_and_cue_bounds() -> None:
    resolved = resolve_overlay(definition(), duration_ms=8_000, require_file=False)

    assert resolved.path == Path("jingle.mp3")
    assert resolved.volume == 0.75
    assert resolved.cue_in_ms == 0
    assert resolved.cue_out_ms == 8_000
    assert resolved.fade_in_ms == 300
    assert resolved.fade_out_ms == 500


def test_resolve_overlay_scales_overlapping_fades_to_effective_duration() -> None:
    item = OverlayDefinition(
        1,
        "Kurz",
        "short.flac",
        cue_in_ms=1_000,
        cue_out_ms=1_500,
        fade_in_ms=400,
        fade_out_ms=600,
    )

    resolved = resolve_overlay(item, duration_ms=2_000, require_file=False)

    assert resolved.fade_in_ms + resolved.fade_out_ms == 500
    assert resolved.fade_in_ms == 200
    assert resolved.fade_out_ms == 300


@pytest.mark.parametrize("path", ["jingle.wav", "jingle", "jingle.ogg"])
def test_resolve_overlay_rejects_unsupported_formats(path: str) -> None:
    with pytest.raises(ValueError, match="Audioformat"):
        resolve_overlay(definition(path), duration_ms=1_000, require_file=False)


def test_replacement_invalidates_stale_prepare_callback() -> None:
    machine = OverlayStateMachine()
    old = definition("old.mp3")
    new = OverlayDefinition(2, "Neu", "new.mp3")
    old_generation = machine.begin_prepare(old)
    new_generation = machine.begin_prepare(new)
    old_playback = resolve_overlay(old, duration_ms=1_000, require_file=False)
    new_playback = resolve_overlay(new, duration_ms=1_000, require_file=False)

    assert not machine.prepared(old_generation, old_playback)
    assert machine.prepared(new_generation, new_playback)
    assert machine.runtime.definition == new


def test_fade_out_is_idempotent_and_invalidates_fade_in() -> None:
    machine = OverlayStateMachine()
    generation = machine.begin_prepare(definition())
    playback = resolve_overlay(definition(), duration_ms=2_000, require_file=False)
    assert machine.prepared(generation, playback)
    assert machine.start(generation)
    assert machine.runtime.status == OverlayStatus.FADING_IN

    fade_generation = machine.begin_fade_out()
    assert fade_generation is not None
    assert machine.begin_fade_out() == fade_generation
    assert not machine.fade_in_complete(generation)
    assert machine.finish(fade_generation)
    assert machine.runtime.status == OverlayStatus.FINISHED


def test_stop_during_prepare_rejects_late_prepare_result() -> None:
    machine = OverlayStateMachine()
    generation = machine.begin_prepare(definition())
    playback = resolve_overlay(definition(), duration_ms=2_000, require_file=False)

    stop_generation = machine.begin_stop()

    assert stop_generation is not None
    assert not machine.prepared(generation, playback)
    assert machine.finish(stop_generation)


def test_stale_failure_cannot_replace_new_playback_state() -> None:
    machine = OverlayStateMachine()
    old_generation = machine.begin_prepare(definition("old.mp3"))
    new_definition = OverlayDefinition(2, "Neu", "new.mp3", fade_in_ms=0)
    new_generation = machine.begin_prepare(new_definition)
    playback = resolve_overlay(new_definition, duration_ms=2_000, require_file=False)
    assert machine.prepared(new_generation, playback)
    assert machine.start(new_generation)

    assert not machine.fail(old_generation, RuntimeError("alter Fehler"))
    assert machine.runtime.status == OverlayStatus.PLAYING


def test_position_updates_are_bounded_and_generation_safe() -> None:
    machine = OverlayStateMachine()
    generation = machine.begin_prepare(definition())
    playback = resolve_overlay(definition(), duration_ms=2_000, require_file=False)
    assert machine.prepared(generation, playback)
    assert machine.start(generation)

    assert machine.update_position(generation, 750)
    assert machine.runtime.position_ms == 750
    assert not machine.update_position(generation, 750)
    assert not machine.update_position(generation - 1, 1_000)
    assert machine.update_position(generation, 5_000)
    assert machine.runtime.position_ms == 2_000
