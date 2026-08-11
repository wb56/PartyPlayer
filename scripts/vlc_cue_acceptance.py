"""Run a muted real-VLC cue and two-deck acceptance check."""

import argparse
from pathlib import Path
from time import monotonic, sleep

from party_player.audio.vlc_backend import VlcAudioBackend


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("track_a", type=Path)
    parser.add_argument("track_b", type=Path)
    parser.add_argument("--cue-a", type=float, default=1.8)
    parser.add_argument("--cue-b", type=float, default=3.0)
    args = parser.parse_args()
    decks = [VlcAudioBackend(), VlcAudioBackend()]
    passed = True
    try:
        for deck, path, cue_in in zip(
            decks, (args.track_a, args.track_b), (args.cue_a, args.cue_b), strict=True
        ):
            deck.set_volume(0.0)
            deck.load(path)
            deck.seek(cue_in)
            deck.play()
            deadline = monotonic() + 5.0
            position = 0.0
            closest_error = float("inf")
            confirmed_position = 0.0
            playback_ok = False
            while monotonic() < deadline:
                sleep(0.05)
                position = deck.get_position()
                error = abs(position - cue_in)
                if error < closest_error:
                    closest_error = error
                    confirmed_position = position
                playback_ok = playback_ok or deck.is_playing()
                if closest_error <= 0.25 and playback_ok:
                    break
            seek_ok = closest_error <= 0.25
            passed = passed and seek_ok and playback_ok
            print(
                f"{path.name}: duration={deck.get_duration():.3f}s, "
                f"cue={cue_in:.3f}s, confirmed={confirmed_position:.3f}s, "
                f"error={closest_error:.3f}s, "
                f"seek_ok={seek_ok}, playing={playback_ok}"
            )
        two_decks = all(deck.is_playing() for deck in decks)
        passed = passed and two_decks
        print(f"two_decks_playing={two_decks}")
    finally:
        for deck in decks:
            deck.close()
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
