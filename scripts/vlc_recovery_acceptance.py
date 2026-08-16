"""Run muted real-VLC isolated and global audio-recovery acceptance checks."""

import argparse
from pathlib import Path
from time import monotonic, sleep

from party_player.audio.vlc_backend import VlcAudioBackend
from party_player.audio_recovery import AudioRecoveryService
from party_player.deck_controller import DeckController
from party_player.emergency_state import EmergencyStateService
from party_player.models import Track


def _wait_for_playback(deck: DeckController, timeout: float = 5.0) -> bool:
    deadline = monotonic() + timeout
    while monotonic() < deadline:
        if deck.backend.is_playing() and deck.backend.get_position() > 0.0:
            return True
        sleep(0.05)
    return False


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("track_a", type=Path)
    parser.add_argument("track_b", type=Path)
    parser.add_argument("--isolated-cycles", type=int, default=3)
    parser.add_argument("--global-cycles", type=int, default=10)
    args = parser.parse_args()

    deck_a = DeckController("A", VlcAudioBackend(worker_name="acceptance-a"))
    deck_b = DeckController("B", VlcAudioBackend(worker_name="acceptance-b"))
    decks = (deck_a, deck_b)
    state = EmergencyStateService()
    for deck in decks:
        deck.set_emergency_muted(True)
    try:
        deck_a.load(Track(1, str(args.track_a), args.track_a.stem, "", "", 0.0))
        deck_b.load(Track(2, str(args.track_b), args.track_b.stem, "", "", 0.0))
        deck_a.play()
        deck_b.play()
        initial_playback = all(_wait_for_playback(deck) for deck in decks)
        service = AudioRecoveryService(
            state,
            deck_a,
            deck_b,
            {
                "A": lambda: VlcAudioBackend(worker_name="acceptance-replacement-a"),
                "B": lambda: VlcAudioBackend(worker_name="acceptance-replacement-b"),
            },
            independent_players=True,
            preserves_shared_instance=True,
            preserves_output_device=True,
            playback_confirmation_seconds=2.0,
        )
        isolated_results = []
        healthy_deck_preserved = True
        for _cycle in range(max(0, args.isolated_cycles)):
            deck_b_before = deck_b.backend.get_position()
            isolated_results.append(service.recover_deck("A"))
            sleep(0.25)
            healthy_deck_preserved = healthy_deck_preserved and (
                deck_b.backend.is_playing() and deck_b.backend.get_position() > deck_b_before
            )
        isolated_passed = all(
            result.success and result.playback_confirmed for result in isolated_results
        )
        isolated_muted = deck_a.transition_muted

        global_results = [
            service.recover_all_backends() for _cycle in range(max(0, args.global_cycles))
        ]
        global_passed = all(result.success for result in global_results)
        global_muted = deck_a.emergency_muted and deck_b.emergency_muted
        print(f"initial_two_decks_playing={initial_playback}")
        print(
            f"isolated_recovery_cycles={len(isolated_results)} "
            f"passed={isolated_passed} muted={isolated_muted}"
        )
        print(f"healthy_deck_preserved={healthy_deck_preserved}")
        print(
            f"global_recovery_cycles={len(global_results)} "
            f"passed={global_passed} muted={global_muted}"
        )
        passed = (
            initial_playback
            and isolated_passed
            and isolated_muted
            and healthy_deck_preserved
            and global_passed
            and global_muted
        )
        return 0 if passed else 1
    finally:
        for deck in decks:
            deck.backend.close()


if __name__ == "__main__":
    raise SystemExit(main())
