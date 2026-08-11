"""Fixed on-disk retention for generated diagnostic artifacts."""

from pathlib import Path


def retain_latest(directory: Path, pattern: str, maximum_files: int = 500) -> None:
    """Delete oldest matching artifacts beyond the configured fixed limit."""
    files = sorted(directory.glob(pattern), key=lambda item: item.name)
    for obsolete in files[: max(0, len(files) - max(1, maximum_files))]:
        try:
            obsolete.unlink()
        except OSError:
            # Retention failure must not prevent the current report from being used.
            continue
