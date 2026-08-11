from dataclasses import replace

from party_player.restore_safety import (
    RestoreSafetyBlocker,
    RestoreSafetyGate,
    RestoreSafetySnapshot,
)


SAFE = RestoreSafetySnapshot(True, True, False, False, False, False, False, False, False)


def test_gate_allows_only_fully_safe_snapshot() -> None:
    result = RestoreSafetyGate(lambda: SAFE).evaluate()

    assert result.allowed
    assert result.reasons == ()


def test_gate_returns_all_blockers_as_immutable_result() -> None:
    unsafe = replace(
        SAFE,
        deck_a_stopped=False,
        crossfade_active=True,
        overlay_active=True,
        audio_recovery_active=True,
        deck_recovery_active=True,
        emergency_action_active=True,
        cue_analysis_active=True,
        loudness_analysis_active=True,
    )

    result = RestoreSafetyGate(lambda: unsafe).evaluate()

    assert not result.allowed
    assert tuple(reason.code for reason in result.reasons) == (
        RestoreSafetyBlocker.DECK_A_NOT_STOPPED,
        RestoreSafetyBlocker.CROSSFADE_ACTIVE,
        RestoreSafetyBlocker.OVERLAY_ACTIVE,
        RestoreSafetyBlocker.AUDIO_RECOVERY_ACTIVE,
        RestoreSafetyBlocker.DECK_RECOVERY_ACTIVE,
        RestoreSafetyBlocker.EMERGENCY_ACTION_ACTIVE,
        RestoreSafetyBlocker.CUE_ANALYSIS_ACTIVE,
        RestoreSafetyBlocker.LOUDNESS_ANALYSIS_ACTIVE,
    )


def test_gate_fails_closed_when_snapshot_cannot_be_read() -> None:
    def fail() -> RestoreSafetySnapshot:
        raise RuntimeError("state race")

    result = RestoreSafetyGate(fail).evaluate()

    assert not result.allowed
    assert result.reasons[0].code is RestoreSafetyBlocker.STATE_UNAVAILABLE
