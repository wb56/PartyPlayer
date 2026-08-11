from party_player.recovery_escalation import (
    GlobalRecoveryContext,
    GlobalRecoveryTrigger,
    RecoveryEscalationPolicy,
    RecoveryEscalationStage,
)


def test_escalation_order_is_explicit_and_stable() -> None:
    assert RecoveryEscalationPolicy.ORDER == (
        RecoveryEscalationStage.PRESERVE_AUDIBLE_PLAYBACK,
        RecoveryEscalationStage.PREPARE_EMERGENCY_ON_HEALTHY_DECK,
        RecoveryEscalationStage.CONFIRM_EMERGENCY_PLAYBACK,
        RecoveryEscalationStage.RECOVER_FAILED_DECK,
        RecoveryEscalationStage.ONE_DECK_MODE,
        RecoveryEscalationStage.REAPPLY_OUTPUT_DEVICE,
        RecoveryEscalationStage.REINITIALIZE_ALL_BACKENDS,
        RecoveryEscalationStage.OPERATOR_INTERVENTION,
    )


def test_automatic_global_recovery_preserves_playing_healthy_deck_first() -> None:
    result = RecoveryEscalationPolicy().assess_global_recovery(
        GlobalRecoveryTrigger.AUTOMATIC,
        GlobalRecoveryContext(
            healthy_deck_playing=True,
            emergency_playback_can_be_prepared=True,
            stable_one_deck_mode_possible=True,
        ),
    )

    assert not result.allowed
    assert result.next_stage == RecoveryEscalationStage.PRESERVE_AUDIBLE_PLAYBACK
    assert result.error_code == "HEALTHY_DECK_PLAYING"


def test_automatic_global_recovery_prefers_emergency_then_one_deck_mode() -> None:
    policy = RecoveryEscalationPolicy()

    emergency = policy.assess_global_recovery(
        GlobalRecoveryTrigger.AUTOMATIC,
        GlobalRecoveryContext(emergency_playback_can_be_prepared=True),
    )
    one_deck = policy.assess_global_recovery(
        GlobalRecoveryTrigger.AUTOMATIC,
        GlobalRecoveryContext(stable_one_deck_mode_possible=True),
    )

    assert emergency.next_stage == RecoveryEscalationStage.PREPARE_EMERGENCY_ON_HEALTHY_DECK
    assert one_deck.next_stage == RecoveryEscalationStage.ONE_DECK_MODE


def test_global_recovery_requires_an_allowed_explicit_trigger() -> None:
    policy = RecoveryEscalationPolicy()
    context = GlobalRecoveryContext(healthy_deck_playing=True)

    for trigger in (
        GlobalRecoveryTrigger.OPERATOR_REQUEST,
        GlobalRecoveryTrigger.OUTPUT_DEVICE_FAILURE,
        GlobalRecoveryTrigger.SHARED_RESOURCE_FAILURE,
    ):
        result = policy.assess_global_recovery(trigger, context)
        assert result.allowed
        assert result.next_stage == RecoveryEscalationStage.REINITIALIZE_ALL_BACKENDS
