"""Release artifact policy for externally installed media dependencies."""

from collections.abc import Iterable
from pathlib import Path


FORBIDDEN_DEPENDENCY_FILENAMES = frozenset(
    {"vlc.exe", "libvlc.dll", "libvlccore.dll", "ffmpeg.exe", "ffprobe.exe"}
)


def is_forbidden_dependency_path(path: str | Path) -> bool:
    """Return whether one artifact path embeds VLC or FFmpeg runtime content."""
    candidate = Path(path)
    name = candidate.name.casefold()
    if name in FORBIDDEN_DEPENDENCY_FILENAMES:
        return True
    parts = {part.casefold() for part in candidate.parts[:-1]}
    return "plugins" in parts and name.startswith("lib") and name.endswith("_plugin.dll")


def forbidden_release_files(root: Path) -> tuple[Path, ...]:
    """List forbidden files relative to one release root in stable order."""
    if not root.is_dir():
        raise ValueError(f"Releaseverzeichnis fehlt: {root}")
    matches = (
        path.relative_to(root)
        for path in root.rglob("*")
        if path.is_file() and is_forbidden_dependency_path(path.relative_to(root))
    )
    return tuple(sorted(matches, key=lambda path: str(path).casefold()))


def format_forbidden_files(paths: Iterable[Path], *, maximum: int = 20) -> str:
    """Format a bounded failure detail for build output."""
    materialized = tuple(paths)
    visible = materialized[: max(1, maximum)]
    lines = [str(path) for path in visible]
    remaining = len(materialized) - len(visible)
    if remaining:
        lines.append(f"… und {remaining} weitere Datei(en)")
    return "\n".join(lines)
