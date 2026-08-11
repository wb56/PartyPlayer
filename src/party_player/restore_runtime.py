"""Composition boundary for the complete restore runtime."""

from dataclasses import dataclass
from pathlib import Path

from party_player.backup_service import BackupService
from party_player.database.connection import Database
from party_player.restore_commit import RestoreCommitService
from party_player.restore_lifecycle import PersistenceParticipant, RestoreLifecycleAdapter
from party_player.restore_pipeline import AtomicRestorePipeline
from party_player.restore_safety import RestoreSafetyGate
from party_player.performance_monitor import PerformanceMonitor


@dataclass(frozen=True, slots=True)
class RestoreRuntime:
    available: bool
    reason: str
    pipeline: AtomicRestorePipeline | None = None
    lifecycle: RestoreLifecycleAdapter | None = None


def build_restore_runtime(
    active_database: Path,
    database: Database,
    participants: tuple[PersistenceParticipant | None, ...],
    *,
    expected_participant_count: int,
    safety_gate: RestoreSafetyGate | None = None,
    safety_retention_limit: int = 10,
    performance_monitor: PerformanceMonitor | None = None,
) -> RestoreRuntime:
    """Build no partial runtime when one required persistence owner is unsafe."""
    available = tuple(participant for participant in participants if participant is not None)
    if (
        len(participants) != expected_participant_count
        or len(available) != expected_participant_count
    ):
        return RestoreRuntime(
            False,
            "Restore ist blockiert: Mindestens ein Persistenz-Owner besitzt keinen sicheren Lifecycle-Participant.",
        )
    lifecycle = RestoreLifecycleAdapter(available)
    commit = RestoreCommitService(
        active_database,
        quiesce=lifecycle.quiesce,
        resume_after_rollback=lifecycle.resume_after_rollback,
    )
    return RestoreRuntime(
        True,
        "Restore-Runtime ist vollständig zusammengesetzt; eine UI-Freigabe ist noch nicht aktiv.",
        AtomicRestorePipeline(
            active_database,
            BackupService(
                database,
                safety_retention_limit=safety_retention_limit,
                performance_monitor=performance_monitor,
            ),
            commit,
            safety_gate=safety_gate,
            performance_monitor=performance_monitor,
        ),
        lifecycle,
    )
