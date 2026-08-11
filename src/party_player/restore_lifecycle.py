"""Ordered persistence lifecycle gate for an atomic database restore."""

from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from threading import Lock
from time import monotonic


class RestoreLifecycleState(str, Enum):
    ACTIVE = "ACTIVE"
    QUIESCING = "QUIESCING"
    QUIESCED = "QUIESCED"
    RESUMING = "RESUMING"
    FAILED = "FAILED"


class RestoreLifecyclePhase(str, Enum):
    NONE = ""
    BLOCK_NEW_WORK = "BLOCK_NEW_WORK"
    DRAIN = "DRAIN"
    CLOSE_CONNECTIONS = "CLOSE_CONNECTIONS"
    RESUME = "RESUME"


@dataclass(frozen=True, slots=True)
class PersistenceParticipant:
    name: str
    block_new_work: Callable[[], bool]
    drain: Callable[[float], bool]
    close_connections: Callable[[], bool]
    resume: Callable[[], bool]


@dataclass(frozen=True, slots=True)
class RestoreLifecycleResult:
    success: bool
    state: RestoreLifecycleState
    phase: RestoreLifecyclePhase
    message: str
    participant: str = ""


class RestoreLifecycleAdapter:
    """Coordinate persistence owners without reaching across thread boundaries."""

    def __init__(
        self,
        participants: tuple[PersistenceParticipant, ...],
        *,
        timeout_seconds: float = 10.0,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        if not participants:
            raise ValueError("Mindestens ein Persistenzteilnehmer ist erforderlich")
        names = tuple(participant.name for participant in participants)
        if any(not name.strip() for name in names) or len(set(names)) != len(names):
            raise ValueError("Persistenzteilnehmer benötigen eindeutige Namen")
        self._participants = participants
        self._timeout_seconds = max(0.1, timeout_seconds)
        self._clock = clock
        self._state = RestoreLifecycleState.ACTIVE
        self._lock = Lock()
        self._last_result = RestoreLifecycleResult(
            True, self._state, RestoreLifecyclePhase.NONE, "Persistenz ist aktiv."
        )

    @property
    def state(self) -> RestoreLifecycleState:
        with self._lock:
            return self._state

    @property
    def last_result(self) -> RestoreLifecycleResult:
        with self._lock:
            return self._last_result

    def quiesce(self) -> bool:
        with self._lock:
            if self._state is RestoreLifecycleState.QUIESCED:
                return True
            if self._state is not RestoreLifecycleState.ACTIVE:
                return False
            self._state = RestoreLifecycleState.QUIESCING
            blocked: list[PersistenceParticipant] = []
            deadline = self._clock() + self._timeout_seconds
            for participant in self._participants:
                if not self._call(participant.block_new_work):
                    self._fail_and_resume(
                        participant, RestoreLifecyclePhase.BLOCK_NEW_WORK, blocked
                    )
                    return False
                blocked.append(participant)
            for participant in self._participants:
                remaining = max(0.0, deadline - self._clock())
                if remaining <= 0.0 or not self._call(participant.drain, remaining):
                    self._fail_and_resume(participant, RestoreLifecyclePhase.DRAIN, blocked)
                    return False
            for participant in self._participants:
                if not self._call(participant.close_connections):
                    self._fail_and_resume(
                        participant, RestoreLifecyclePhase.CLOSE_CONNECTIONS, blocked
                    )
                    return False
            self._state = RestoreLifecycleState.QUIESCED
            self._last_result = RestoreLifecycleResult(
                True,
                self._state,
                RestoreLifecyclePhase.NONE,
                "Persistenzjobs sind geleert und Verbindungen geschlossen.",
            )
            return True

    def resume_after_rollback(self) -> bool:
        with self._lock:
            if self._state is RestoreLifecycleState.ACTIVE:
                return True
            if self._state is not RestoreLifecycleState.QUIESCED:
                return False
            self._state = RestoreLifecycleState.RESUMING
            for participant in reversed(self._participants):
                if not self._call(participant.resume):
                    self._state = RestoreLifecycleState.FAILED
                    self._last_result = RestoreLifecycleResult(
                        False,
                        self._state,
                        RestoreLifecyclePhase.RESUME,
                        "Persistenz konnte nach Rollback nicht vollständig fortgesetzt werden.",
                        participant.name,
                    )
                    return False
            self._state = RestoreLifecycleState.ACTIVE
            self._last_result = RestoreLifecycleResult(
                True,
                self._state,
                RestoreLifecyclePhase.NONE,
                "Persistenz wurde nach Rollback fortgesetzt.",
            )
            return True

    def _fail_and_resume(
        self,
        failed: PersistenceParticipant,
        phase: RestoreLifecyclePhase,
        blocked: list[PersistenceParticipant],
    ) -> None:
        resume_ok = all(self._call(participant.resume) for participant in reversed(blocked))
        self._state = RestoreLifecycleState.ACTIVE if resume_ok else RestoreLifecycleState.FAILED
        self._last_result = RestoreLifecycleResult(
            False,
            self._state,
            phase,
            "Persistenz-Lifecycle-Gate wurde sicher abgebrochen.",
            failed.name,
        )

    @staticmethod
    def _call(callback: Callable[..., bool], *args: float) -> bool:
        try:
            return bool(callback(*args))
        except Exception:
            return False
