"""Bounded shell-free process execution for dependency probes."""

from collections.abc import Sequence
from dataclasses import dataclass
import os
from pathlib import Path
import signal
import subprocess
from time import monotonic
import unicodedata

from party_player.system_dependencies import (
    DEPENDENCY_MAXIMUM_OUTPUT_BYTES,
    DEPENDENCY_PROBE_TIMEOUT_SECONDS,
)


@dataclass(frozen=True, slots=True)
class ExternalProcessResult:
    command: tuple[str, ...]
    return_code: int | None
    stdout: str
    stderr: str
    duration_seconds: float
    timed_out: bool = False
    output_truncated: bool = False
    error: str = ""

    @property
    def succeeded(self) -> bool:
        return not self.timed_out and not self.error and self.return_code == 0


def diagnostic_output_excerpt(
    output: str,
    *,
    maximum_lines: int = 3,
    maximum_characters: int = 600,
) -> str:
    """Return bounded printable diagnostic lines suitable for reports and logs."""
    line_limit = max(1, maximum_lines)
    character_limit = max(32, maximum_characters)
    lines: list[str] = []
    for raw_line in output.splitlines():
        printable = "".join(
            character
            for character in raw_line
            if character == "\t" or not unicodedata.category(character).startswith("C")
        ).strip()
        if printable:
            lines.append(printable)
        if len(lines) >= line_limit:
            break
    excerpt = " | ".join(lines)
    if len(excerpt) > character_limit:
        excerpt = excerpt[: character_limit - 1].rstrip() + "…"
    return excerpt


def process_start_error_detail(error: OSError | ValueError) -> str:
    """Describe a launch failure without copying executable paths from the OS text."""
    error_number = getattr(error, "winerror", None) or getattr(error, "errno", None)
    suffix = f" (code={error_number})" if error_number is not None else ""
    return f"{type(error).__name__}{suffix}: Prozess konnte nicht gestartet werden"


class ExternalProcessRunner:
    """Run one explicit argument vector with hard time and output bounds."""

    def __init__(
        self,
        *,
        default_timeout_seconds: float = DEPENDENCY_PROBE_TIMEOUT_SECONDS,
        maximum_output_bytes: int = DEPENDENCY_MAXIMUM_OUTPUT_BYTES,
    ) -> None:
        self._default_timeout = max(0.05, float(default_timeout_seconds))
        self._maximum_output = max(256, int(maximum_output_bytes))

    def run(
        self,
        arguments: Sequence[str | Path],
        *,
        timeout_seconds: float | None = None,
    ) -> ExternalProcessResult:
        command = tuple(str(argument) for argument in arguments)
        if not command or not command[0].strip():
            raise ValueError("Ein ausführbares Programm muss angegeben werden")
        timeout = (
            self._default_timeout if timeout_seconds is None else max(0.05, float(timeout_seconds))
        )
        started = monotonic()
        creation_flags = 0
        start_new_session = os.name != "nt"
        if os.name == "nt":
            creation_flags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
        try:
            process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=False,
                creationflags=creation_flags,
                start_new_session=start_new_session,
            )
        except (OSError, ValueError) as exc:
            return ExternalProcessResult(
                command,
                None,
                "",
                "",
                monotonic() - started,
                error=process_start_error_detail(exc),
            )
        try:
            stdout, stderr = process.communicate(timeout=timeout)
            timed_out = False
        except subprocess.TimeoutExpired:
            timed_out = True
            self._terminate_process_tree(process)
            stdout, stderr = process.communicate()
        stdout_text, stdout_truncated = self._decode_bounded(stdout)
        stderr_text, stderr_truncated = self._decode_bounded(stderr)
        return ExternalProcessResult(
            command,
            process.returncode,
            stdout_text,
            stderr_text,
            monotonic() - started,
            timed_out,
            stdout_truncated or stderr_truncated,
        )

    def _decode_bounded(self, value: bytes) -> tuple[str, bool]:
        truncated = len(value) > self._maximum_output
        bounded = value[: self._maximum_output]
        return bounded.decode("utf-8", errors="replace"), truncated

    @staticmethod
    def _terminate_process_tree(process: subprocess.Popen[bytes]) -> None:
        if process.poll() is not None:
            return
        if os.name != "nt":
            try:
                kill_process_group = getattr(os, "killpg")
                kill_process_group(process.pid, getattr(signal, "SIGKILL", 9))
            except (OSError, ProcessLookupError):
                process.kill()
            return
        try:
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                shell=False,
                timeout=2.0,
                creationflags=subprocess.CREATE_NO_WINDOW,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            process.kill()
            return
        try:
            process.wait(timeout=0.25)
        except subprocess.TimeoutExpired:
            process.kill()
