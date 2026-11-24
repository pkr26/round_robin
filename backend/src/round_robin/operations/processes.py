"""Bounded graceful shutdown for process groups created by our supervisors."""
import math
import os
import signal
import subprocess
import time


def _group_exists(group_id):
    try:
        os.killpg(group_id, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Darwin may return EPERM while an exited group still has zombies.
        # Its disappearance must be proved after reaping, not assumed.
        return True
    return True


def _wait_groups(processes, deadline, poll_interval):
    while True:
        for process in processes:
            process.poll()
        alive = [process for process in processes if _group_exists(process.pid)]
        remaining = deadline - time.monotonic()
        if not alive or remaining <= 0:
            return alive
        time.sleep(min(poll_interval, remaining))


def terminate_owned(processes, *, grace_seconds=10.0, poll_interval=0.05):
    """Terminate isolated owned groups, including children of exited launchers.

    Every group receives the same graceful shutdown interval. After its deadline
    remaining groups are killed and launchers share a one-second bounded reap.
    Callers must have launched each process with ``start_new_session=True``.
    """
    if not math.isfinite(grace_seconds) or grace_seconds < 0:
        raise ValueError("Shutdown grace must be finite and nonnegative")
    if not math.isfinite(poll_interval) or poll_interval <= 0:
        raise ValueError("Shutdown polling must be finite and positive")
    processes = list(processes)
    deadline = time.monotonic() + grace_seconds
    for process in processes:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        except PermissionError:
            # Reaping may clear a zombie-only group during the grace period.
            pass
    alive = _wait_groups(processes, deadline, poll_interval)
    denied = []
    for process in alive:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except PermissionError as error:
            denied.append((process, error))
    failures = []
    reap_deadline = time.monotonic() + 1
    for process in processes:
        try:
            process.wait(timeout=max(0, reap_deadline - time.monotonic()))
        except subprocess.TimeoutExpired as error:
            failures.append(error)
    unresolved = _wait_groups([process for process, _ in denied], reap_deadline, poll_interval)
    failures.extend(error for process, error in denied if process in unresolved)
    if failures:
        raise failures[0]
