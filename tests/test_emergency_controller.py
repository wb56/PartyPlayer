from threading import Event, Thread

from party_player.emergency_controller import EmergencyController
from party_player.emergency_playback import EmergencyPlaybackResult
from party_player.emergency_playlist import EmergencyMediaType
from party_player.emergency_state import EmergencyStateService
from party_player.audio_recovery import AudioRecoveryResult, DeckRestartAssessment


class PlaybackStub:
    def __init__(self) -> None:
        self.prepare_result = EmergencyPlaybackResult(True, "PREPARED", "A", 7)
        self.activate_result = EmergencyPlaybackResult(True, "PLAYING", "A", 7)

    def prepare_primary(self) -> EmergencyPlaybackResult:
        return self.prepare_result

    def activate_prepared(self) -> EmergencyPlaybackResult:
        return self.activate_result

    def prepare_media(
        self, media_type: EmergencyMediaType, *, loop: bool = False
    ) -> EmergencyPlaybackResult:
        self.media_request = (media_type, loop)
        return self.prepare_result

    def mute_deck_immediately(self, deck_id: str) -> EmergencyPlaybackResult:
        self.muted_deck_id = deck_id
        return EmergencyPlaybackResult(True, "MUTED", deck_id)

    def immediate_replace(self, deck_id: str) -> EmergencyPlaybackResult:
        self.replaced_deck_id = deck_id
        return EmergencyPlaybackResult(True, "PLAYING", "B", 7)


def test_controller_audits_structured_prepare_and_activate_results() -> None:
    events: list[tuple[str, dict[str, object]]] = []
    controller = EmergencyController(
        PlaybackStub(),  # type: ignore[arg-type]
        EmergencyStateService(),
        lambda code, details: events.append((code, details)),
    )

    prepared = controller.prepare()
    activated = controller.activate()

    assert prepared.state == "PREPARED"
    assert activated.state == "PLAYING"
    assert controller.last_result == activated
    assert [code for code, _details in events] == ["EMERGENCY_PREPARE", "EMERGENCY_ACTIVATE"]
    assert events[-1][1]["deck_id"] == "A"
    assert events[-1][1]["track_id"] == 7


def test_controller_plays_typed_medium_as_one_audited_action() -> None:
    events: list[tuple[str, dict[str, object]]] = []
    playback = PlaybackStub()
    controller = EmergencyController(  # type: ignore[arg-type]
        playback,
        EmergencyStateService(),
        lambda code, details: events.append((code, details)),
    )

    result = controller.play_media(EmergencyMediaType.BREAK_MUSIC, loop=True)

    assert result.state == "PLAYING"
    assert playback.media_request == (EmergencyMediaType.BREAK_MUSIC, True)
    assert events == [
        (
            "EMERGENCY_MEDIA_PLAY",
            {
                "success": True,
                "state": "PLAYING",
                "deck_id": "A",
                "track_id": 7,
                "error_code": "",
                "message": "",
                "attempt": 0,
                "attempts_remaining": 0,
                "cue_in": 0.0,
                "effective_gain_db": 0.0,
                "clip_protection_enabled": False,
                "media_type": "BREAK_MUSIC",
                "loop": True,
            },
        )
    ]


def test_controller_immediate_replace_mutes_first_and_audits_strategy() -> None:
    events: list[tuple[str, dict[str, object]]] = []
    playback = PlaybackStub()
    controller = EmergencyController(  # type: ignore[arg-type]
        playback,
        EmergencyStateService(),
        lambda code, details: events.append((code, details)),
    )

    result = controller.immediate_replace("a")

    assert result.success
    assert playback.muted_deck_id == "A"
    assert playback.replaced_deck_id == "A"
    assert events[-1][0] == "EMERGENCY_IMMEDIATE_REPLACE"
    assert events[-1][1]["strategy"] == "IMMEDIATE_REPLACE"
    assert events[-1][1]["affected_deck_id"] == "A"


def test_parallel_emergency_action_is_rejected_without_second_playback_call() -> None:
    entered = Event()
    release = Event()

    class BlockingPlayback(PlaybackStub):
        def __init__(self) -> None:
            super().__init__()
            self.prepare_calls = 0

        def prepare_primary(self) -> EmergencyPlaybackResult:
            self.prepare_calls += 1
            entered.set()
            release.wait(timeout=2)
            return self.prepare_result

    playback = BlockingPlayback()
    controller = EmergencyController(  # type: ignore[arg-type]
        playback,
        EmergencyStateService(),
    )
    worker = Thread(target=controller.prepare)
    worker.start()
    assert entered.wait(timeout=1)

    busy = controller.prepare()

    assert not busy.success
    assert busy.error_code == "EMERGENCY_ACTION_IN_PROGRESS"
    assert playback.prepare_calls == 1
    release.set()
    worker.join(timeout=1)


class RecoveryStub:
    def __init__(self, *, allowed: bool = True) -> None:
        self.assessment = DeckRestartAssessment(
            allowed,
            "SHARED_INSTANCE_NOT_PRESERVED" if not allowed else "",
            "Gemeinsame VLC-Instanz" if not allowed else "",
        )
        self.recover_calls: list[str] = []

    def can_restart_deck_independently(self, deck_id: str) -> DeckRestartAssessment:
        return self.assessment

    def recover_deck(self, deck_id: str) -> AudioRecoveryResult:
        self.recover_calls.append(deck_id)
        return AudioRecoveryResult(True, "RECOVERED", deck_id)

    def recovery_active(self) -> bool:
        return False


def test_escalation_confirms_emergency_audio_before_isolated_recovery() -> None:
    calls: list[str] = []

    class OrderedPlayback(PlaybackStub):
        def prepare_primary(self) -> EmergencyPlaybackResult:
            calls.append("prepare")
            return EmergencyPlaybackResult(True, "PREPARED", "B", 7)

        def activate_prepared(self) -> EmergencyPlaybackResult:
            calls.append("activate")
            return EmergencyPlaybackResult(True, "PLAYING", "B", 7)

    class OrderedRecovery(RecoveryStub):
        def can_restart_deck_independently(self, deck_id: str) -> DeckRestartAssessment:
            calls.append("assess")
            return super().can_restart_deck_independently(deck_id)

        def recover_deck(self, deck_id: str) -> AudioRecoveryResult:
            calls.append("recover")
            return super().recover_deck(deck_id)

    recovery = OrderedRecovery()
    controller = EmergencyController(
        OrderedPlayback(),  # type: ignore[arg-type]
        EmergencyStateService(),
        recovery=recovery,  # type: ignore[arg-type]
    )

    result = controller.stabilize_failed_deck("A")

    assert result.success
    assert result.state == "RECOVERED"
    assert calls == ["prepare", "activate", "assess", "recover"]
    assert recovery.recover_calls == ["A"]


def test_unsafe_recovery_keeps_confirmed_emergency_audio_running() -> None:
    playback = PlaybackStub()
    playback.prepare_result = EmergencyPlaybackResult(True, "PREPARED", "B", 7)
    playback.activate_result = EmergencyPlaybackResult(True, "PLAYING", "B", 7)
    recovery = RecoveryStub(allowed=False)
    controller = EmergencyController(
        playback,  # type: ignore[arg-type]
        EmergencyStateService(),
        recovery=recovery,  # type: ignore[arg-type]
    )

    result = controller.stabilize_failed_deck("A")

    assert result.success
    assert result.state == "EMERGENCY_PLAYING_SINGLE_DECK"
    assert result.playback is not None and result.playback.state == "PLAYING"
    assert result.error_code == "SHARED_INSTANCE_NOT_PRESERVED"
    assert recovery.recover_calls == []


def test_failed_emergency_activation_prevents_recovery_attempt() -> None:
    playback = PlaybackStub()
    playback.prepare_result = EmergencyPlaybackResult(True, "PREPARED", "B", 7)
    playback.activate_result = EmergencyPlaybackResult(
        False, "FAILED", "B", 7, "EMERGENCY_START_FAILED"
    )
    recovery = RecoveryStub()
    controller = EmergencyController(
        playback,  # type: ignore[arg-type]
        EmergencyStateService(),
        recovery=recovery,  # type: ignore[arg-type]
    )

    result = controller.stabilize_failed_deck("A")

    assert not result.success
    assert result.state == "ACTIVATION_FAILED"
    assert recovery.recover_calls == []


def test_escalation_rejects_emergency_prepared_on_failed_deck() -> None:
    playback = PlaybackStub()
    playback.prepare_result = EmergencyPlaybackResult(True, "PREPARED", "A", 7)
    recovery = RecoveryStub()
    controller = EmergencyController(
        playback,  # type: ignore[arg-type]
        EmergencyStateService(),
        recovery=recovery,  # type: ignore[arg-type]
    )

    result = controller.stabilize_failed_deck("A")

    assert not result.success
    assert result.error_code == "EMERGENCY_USES_FAILED_DECK"
    assert recovery.recover_calls == []
