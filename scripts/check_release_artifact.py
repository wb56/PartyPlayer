"""Fail a release build that embeds externally installed VLC or FFmpeg files."""

from pathlib import Path
import sys

from party_player.release_artifact import forbidden_release_files, format_forbidden_files


def main(arguments: list[str] | None = None) -> int:
    values = sys.argv[1:] if arguments is None else arguments
    if len(values) != 1:
        print("Usage: check_release_artifact.py <release-directory>")
        return 2
    try:
        forbidden = forbidden_release_files(Path(values[0]))
    except ValueError as exc:
        print(exc)
        return 2
    if forbidden:
        print("Release enthält verbotene VLC-/FFmpeg-Laufzeitdateien:")
        print(format_forbidden_files(forbidden))
        return 1
    print("Releaseprüfung bestanden: keine VLC-/FFmpeg-Laufzeitdateien enthalten.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
