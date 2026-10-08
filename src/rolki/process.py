from __future__ import annotations

import asyncio
import os
import signal
from pathlib import Path

from .errors import PermanentError, TransientError


async def run_process(
    args: list[str], *, timeout: float, cwd: Path | None = None, error_types: dict | None = None
) -> str:
    """No shell. Bounded output; kill the entire child group on timeout/cancellation."""
    try:
        process = await asyncio.create_subprocess_exec(
            *map(str, args),
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
            env={**os.environ, "OMP_NUM_THREADS": "2", "OPENBLAS_NUM_THREADS": "2"},
        )
    except FileNotFoundError as exc:
        raise PermanentError(f"Brak programu: {Path(args[0]).name}.") from exc
    stdout = bytearray()

    async def drain(stream, keep=False):
        while chunk := await stream.read(8192):
            if keep:
                stdout.extend(chunk)
                if len(stdout) > 8 * 1024**2:
                    raise PermanentError("Program zwrócił zbyt dużo danych.")

    async def collect():
        await asyncio.gather(drain(process.stdout, True), drain(process.stderr))
        return await process.wait()

    async def finish_stopped():
        # wait() alone can deadlock if a cancelled reader left a PIPE buffer full.
        await asyncio.gather(drain(process.stdout), drain(process.stderr))
        await process.wait()

    try:
        returncode = await asyncio.wait_for(collect(), timeout)
    except BaseException as exc:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            await asyncio.wait_for(finish_stopped(), 5)
        except TimeoutError:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await asyncio.wait_for(finish_stopped(), 5)
        if isinstance(exc, TimeoutError):
            raise TransientError(f"Przekroczono czas wykonania {Path(args[0]).name}.") from exc
        raise
    if returncode:
        if error_types and returncode in error_types:
            raise error_types[returncode](stdout.decode("utf-8", errors="replace").strip()[:400])
        raise PermanentError(f"{Path(args[0]).name} zakończył się błędem (kod {returncode}).")
    return stdout.decode("utf-8", errors="replace")


async def retry_network(operation, attempts=3):
    for attempt in range(attempts):
        try:
            return await operation()
        except TransientError as exc:
            if attempt + 1 == attempts:
                raise
            await asyncio.sleep(max(2**attempt, exc.retry_after))
