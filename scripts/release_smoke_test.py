"""Run the bounded smoke test for dist/DeckRelay."""

import argparse
from pathlib import Path
import subprocess
import sys

from party_player.release_smoke import run_release_smoke_test


def main(arguments: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("release_directory", nargs="?", type=Path, default=Path("dist/DeckRelay"))
    parser.add_argument("--timeout", type=float, default=8.0)
    args = parser.parse_args(arguments)
    project = Path(__file__).resolve().parents[1]
    checker = project / "scripts" / "check_release_artifact.py"

    def check_artifact(release_directory: Path) -> bool:
        completed = subprocess.run(  # noqa: S603 - fixed local checker and interpreter
            [sys.executable, str(checker), str(release_directory)], check=False
        )
        return completed.returncode == 0

    result = run_release_smoke_test(
        args.release_directory,
        artifact_checker=check_artifact,
        timeout_seconds=args.timeout,
    )
    print(f"{result.code.value}: {result.message}")
    if result.pid is not None:
        print(f"test_pid={result.pid}")
    return 0 if result.success else 1


if __name__ == "__main__":
    raise SystemExit(main())
