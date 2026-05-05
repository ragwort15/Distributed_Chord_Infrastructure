"""
Phase 4: subprocess-based task execution helper.

Runs a SCRIPT (script body written to a temp file, then bash) or BINARY
(invoked directly) under a wall-clock timeout. Captures stdout, stderr,
exit code, duration, and a `timed_out` flag.

The temp file is deleted in a finally block, even if execution times out
or raises. No sandboxing — Phase 4 assumes trusted scripts/binaries.
"""

import os
import subprocess
import tempfile
import time
from dataclasses import dataclass


@dataclass
class ExecutionResult:
    exit_code: int
    stdout: str
    stderr: str
    duration_ms: int
    timed_out: bool

    def to_dict(self) -> dict:
        return {
            "exit_code": self.exit_code,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "duration_ms": self.duration_ms,
            "timed_out": self.timed_out,
        }


def run_task(task_type: str, path: str, script: str, timeout_s: int = 60) -> ExecutionResult:
    """
    Execute a task and capture its output.

    SCRIPT: writes ``script`` to a temp file (``.sh``), chmod 755, runs ``bash <tmp>``.
    BINARY: invokes ``path`` directly. The binary must exist on disk and be executable.

    Returns an ExecutionResult — never raises for the underlying program's failure;
    only raises for genuinely-bad inputs (unknown task_type, missing script body
    when task_type=SCRIPT and script is empty AND path is empty).
    """
    tmppath = None

    if task_type == "SCRIPT":
        body = script or ""
        if not body and path:
            # Allow `path` to point at an existing script file when `script` is empty.
            cmd = ["bash", path]
        else:
            with tempfile.NamedTemporaryFile(mode="w", suffix=".sh", delete=False) as f:
                f.write(body)
                tmppath = f.name
            os.chmod(tmppath, 0o755)
            cmd = ["bash", tmppath]
    elif task_type == "BINARY":
        if not path:
            raise ValueError("BINARY task requires non-empty path")
        cmd = [path]
    else:
        raise ValueError(f"Unknown task_type: {task_type!r}")

    start = time.time()
    try:
        proc = subprocess.run(
            cmd, capture_output=True, timeout=timeout_s, text=True,
        )
        duration_ms = int((time.time() - start) * 1000)
        return ExecutionResult(
            exit_code=proc.returncode,
            stdout=proc.stdout or "",
            stderr=proc.stderr or "",
            duration_ms=duration_ms,
            timed_out=False,
        )
    except subprocess.TimeoutExpired as exc:
        duration_ms = int((time.time() - start) * 1000)
        stdout = exc.stdout if isinstance(exc.stdout, str) else ""
        stderr = (exc.stderr if isinstance(exc.stderr, str) else "") + \
                 f"\n[TIMEOUT after {timeout_s}s]"
        return ExecutionResult(
            exit_code=-1,
            stdout=stdout,
            stderr=stderr,
            duration_ms=duration_ms,
            timed_out=True,
        )
    except FileNotFoundError as exc:
        duration_ms = int((time.time() - start) * 1000)
        return ExecutionResult(
            exit_code=-1,
            stdout="",
            stderr=f"executable not found: {exc}",
            duration_ms=duration_ms,
            timed_out=False,
        )
    finally:
        if tmppath:
            try:
                os.unlink(tmppath)
            except OSError:
                pass
