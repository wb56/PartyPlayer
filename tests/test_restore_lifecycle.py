from dataclasses import dataclass, field

from party_player.restore_lifecycle import (
    PersistenceParticipant,
    RestoreLifecycleAdapter,
    RestoreLifecyclePhase,
    RestoreLifecycleState,
)


@dataclass
class FakeParticipant:
    name: str
    events: list[str]
    fail_phase: str = ""
    drain_durations: list[float] = field(default_factory=list)

    def participant(self) -> PersistenceParticipant:
        return PersistenceParticipant(
            self.name,
            lambda: self._step("block"),
            self._drain,
            lambda: self._step("close"),
            lambda: self._step("resume"),
        )

    def _step(self, phase: str) -> bool:
        self.events.append(f"{self.name}.{phase}")
        return self.fail_phase != phase

    def _drain(self, remaining: float) -> bool:
        self.drain_durations.append(remaining)
        return self._step("drain")


def test_lifecycle_orders_block_drain_close_and_reverse_resume() -> None:
    events: list[str] = []
    first = FakeParticipant("first", events)
    second = FakeParticipant("second", events)
    lifecycle = RestoreLifecycleAdapter((first.participant(), second.participant()))

    assert lifecycle.quiesce()
    assert lifecycle.state is RestoreLifecycleState.QUIESCED
    assert lifecycle.quiesce()
    assert lifecycle.resume_after_rollback()

    assert events == [
        "first.block",
        "second.block",
        "first.drain",
        "second.drain",
        "first.close",
        "second.close",
        "second.resume",
        "first.resume",
    ]
    assert lifecycle.state.value == RestoreLifecycleState.ACTIVE.value


def test_drain_failure_resumes_every_blocked_participant_in_reverse_order() -> None:
    events: list[str] = []
    first = FakeParticipant("first", events, fail_phase="drain")
    second = FakeParticipant("second", events)
    lifecycle = RestoreLifecycleAdapter((first.participant(), second.participant()))

    assert not lifecycle.quiesce()

    assert lifecycle.last_result.phase is RestoreLifecyclePhase.DRAIN
    assert lifecycle.last_result.participant == "first"
    assert events[-2:] == ["second.resume", "first.resume"]
    assert lifecycle.state is RestoreLifecycleState.ACTIVE


def test_timeout_prevents_drain_callback_and_resumes() -> None:
    events: list[str] = []
    participant = FakeParticipant("worker", events)
    ticks = iter((0.0, 2.0))
    lifecycle = RestoreLifecycleAdapter(
        (participant.participant(),), timeout_seconds=1.0, clock=lambda: next(ticks)
    )

    assert not lifecycle.quiesce()

    assert "worker.drain" not in events
    assert events[-1] == "worker.resume"
    assert lifecycle.last_result.phase is RestoreLifecyclePhase.DRAIN


def test_close_failure_and_resume_failure_leave_stable_failed_state() -> None:
    events: list[str] = []
    participant = FakeParticipant("worker", events, fail_phase="close")
    lifecycle = RestoreLifecycleAdapter((participant.participant(),))

    assert not lifecycle.quiesce()
    assert lifecycle.state is RestoreLifecycleState.ACTIVE

    participant.fail_phase = ""
    assert lifecycle.quiesce()
    participant.fail_phase = "resume"
    assert not lifecycle.resume_after_rollback()
    assert lifecycle.state.value == RestoreLifecycleState.FAILED.value
    assert lifecycle.last_result.phase is RestoreLifecyclePhase.RESUME
