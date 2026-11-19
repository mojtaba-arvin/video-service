"""Bounded process-group supervision for FFmpeg subprocesses."""

import os
import selectors
import signal
import subprocess
import time

import psutil


class ProcessTreeTerminationError(RuntimeError):
    """Raised when one or more descendants survive forced termination."""


def _active_process_group_members(process_group_id):
    """Return non-zombie PIDs currently owned by a process group."""
    active = []
    for item in psutil.process_iter(attrs=["pid", "status"]):
        try:
            if item.info.get("status") == psutil.STATUS_ZOMBIE:
                continue
            if os.getpgid(item.info["pid"]) == process_group_id:
                active.append(item.info["pid"])
        except (psutil.NoSuchProcess, psutil.AccessDenied, ProcessLookupError, OSError):
            continue
    return active


def _terminate_process_group(process, grace_period):
    """Terminate a dedicated POSIX process group, including orphaned children."""
    process_group_id = process.pid
    try:
        os.killpg(process_group_id, signal.SIGTERM)
    except ProcessLookupError:
        if process.poll() is None:
            process.wait(timeout=grace_period)
        return

    deadline = time.monotonic() + grace_period
    active = _active_process_group_members(process_group_id)
    while active and time.monotonic() < deadline:
        time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))
        active = _active_process_group_members(process_group_id)

    if active:
        try:
            os.killpg(process_group_id, signal.SIGKILL)
        except ProcessLookupError:
            pass

        deadline = time.monotonic() + grace_period
        active = _active_process_group_members(process_group_id)
        while active and time.monotonic() < deadline:
            time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))
            active = _active_process_group_members(process_group_id)

    if active:
        raise ProcessTreeTerminationError(
            "Processes survived forced termination: {}".format(
                ", ".join(str(pid) for pid in sorted(active))
            )
        )

    if process.poll() is None:
        process.wait(timeout=grace_period)


def terminate_process_tree(process, grace_period=1.0):
    """Terminate a process and descendants with bounded graceful/forced waits.

    Process groups are used for subprocesses started by this module. This also
    handles descendants that outlive the original parent process. For external
    Popen objects, psutil snapshots the live descendant tree.

    Args:
        process: A subprocess.Popen object or a PID.
        grace_period: Maximum seconds for graceful and forced termination.

    Raises:
        ProcessTreeTerminationError: A descendant survives forced termination.
        psutil.AccessDenied: The worker lacks permission to stop a process.
    """
    if getattr(process, "_video_service_process_group", False):
        _terminate_process_group(process, grace_period)
        return

    pid = getattr(process, "pid", process)
    try:
        root = psutil.Process(int(pid))
    except psutil.NoSuchProcess:
        return

    try:
        descendants = root.children(recursive=True)
    except psutil.NoSuchProcess:
        return

    targets = descendants + [root]
    try:
        root.terminate()
    except psutil.NoSuchProcess:
        pass

    for child in reversed(descendants):
        try:
            child.terminate()
        except psutil.NoSuchProcess:
            pass

    _, alive = psutil.wait_procs(targets, timeout=grace_period)
    for proc in alive:
        try:
            proc.kill()
        except psutil.NoSuchProcess:
            pass

    _, alive = psutil.wait_procs(alive, timeout=grace_period)
    if alive:
        raise ProcessTreeTerminationError(
            "Processes survived forced termination: {}".format(
                ", ".join(str(proc.pid) for proc in sorted(alive, key=lambda p: p.pid))
            )
        )


class ProcessExecutionCancelled(RuntimeError):
    """Raised when a cooperative cancellation check requests process shutdown."""


def _sample_process_group_usage(process_group_id):
    """Sample aggregate CPU time and resident memory for a managed process group."""
    cpu_seconds = 0.0
    resident_bytes = 0
    for pid in _active_process_group_members(process_group_id):
        try:
            process = psutil.Process(pid)
            cpu = process.cpu_times()
            memory = process.memory_info()
            cpu_seconds += float(cpu.user + cpu.system)
            resident_bytes += int(memory.rss)
        except (psutil.NoSuchProcess, psutil.AccessDenied, ProcessLookupError, OSError):
            continue
    return cpu_seconds, resident_bytes


def _start_managed_process(command, stdin, stdout, stderr):
    process = subprocess.Popen(
        command,
        stdin=stdin,
        stdout=stdout,
        stderr=stderr,
        start_new_session=True,
        bufsize=0,
    )
    # Popen has no public process-group field. Mark only processes created with
    # start_new_session=True so the supervisor can safely address their PGID.
    process._video_service_process_group = True
    return process


def run_cancellable_process(
        command,
        timeout_seconds,
        should_cancel=None,
        poll_interval=0.2,
        terminate_grace_period=1.0,
        on_usage=None):
    """Run a command with a deadline and cooperative process-group cancellation.

    Both stdout and stderr are drained while polling. Dedicated process groups
    ensure that descendants are terminated even if the root process has exited
    but a child still holds an inherited pipe open. When requested, usage
    callbacks receive sampled process-tree CPU, peak RSS and elapsed execution
    time; elapsed time is distinct from the enclosing durable attempt duration.

    Raises:
        ProcessExecutionCancelled: when should_cancel returns True.
        subprocess.TimeoutExpired: when the execution deadline is reached.
        subprocess.CalledProcessError: when the command exits non-zero.
        ProcessTreeTerminationError: when a descendant survives forced shutdown.
    """
    if timeout_seconds is None or timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be greater than zero")
    if poll_interval <= 0:
        raise ValueError("poll_interval must be greater than zero")

    process = _start_managed_process(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    started_at = time.monotonic()
    terminated = False
    peak_memory_bytes = 0
    sampled_cpu_seconds = 0.0

    def emit_usage():
        if on_usage is not None:
            on_usage({
                "cpu_seconds": sampled_cpu_seconds,
                "peak_memory_bytes": peak_memory_bytes or None,
                "memory_duration_ms": max(
                    0, int((time.monotonic() - started_at) * 1000)
                ),
            })

    def capture_usage():
        nonlocal peak_memory_bytes, sampled_cpu_seconds
        try:
            root = psutil.Process(process.pid)
            processes = [root] + root.children(recursive=True)
            cpu_seconds = 0.0
            current_rss = 0
            for child in processes:
                try:
                    cpu = child.cpu_times()
                    cpu_seconds += float(cpu.user + cpu.system)
                    current_rss += int(child.memory_info().rss)
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue
            sampled_cpu_seconds = max(sampled_cpu_seconds, cpu_seconds)
            peak_memory_bytes = max(peak_memory_bytes, current_rss)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
        emit_usage()

    try:
        while True:
            capture_usage()
            if should_cancel is not None and should_cancel():
                terminate_process_tree(
                    process, grace_period=terminate_grace_period
                )
                terminated = True
                process.communicate()
                emit_usage()
                raise ProcessExecutionCancelled("FFmpeg execution was cancelled")

            remaining = timeout_seconds - (time.monotonic() - started_at)
            if remaining <= 0:
                terminate_process_tree(
                    process, grace_period=terminate_grace_period
                )
                terminated = True
                stdout, stderr = process.communicate()
                emit_usage()
                raise subprocess.TimeoutExpired(
                    command,
                    timeout_seconds,
                    output=stdout,
                    stderr=stderr,
                )

            try:
                stdout, stderr = process.communicate(
                    timeout=min(poll_interval, remaining)
                )
                capture_usage()
                break
            except subprocess.TimeoutExpired:
                continue

        if process.returncode:
            raise subprocess.CalledProcessError(
                process.returncode,
                command,
                output=stdout,
                stderr=stderr,
            )
        return stdout, stderr

    except BaseException:
        if not terminated:
            terminate_process_tree(
                process, grace_period=terminate_grace_period
            )
        if process.poll() is None:
            try:
                process.communicate(timeout=terminate_grace_period)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate()
        raise


def run_streaming_process(
        command,
        timeout_seconds,
        on_line,
        should_cancel=None,
        poll_interval=0.2,
        terminate_grace_period=1.0,
        max_line_bytes=65536,
        on_usage=None):
    """Run a command while delivering stdout/stderr lines as they arrive.

    The pipe is read with selectors/os.read rather than blocking readline(), so
    deadlines and cancellation are checked even when FFmpeg stops emitting
    output. Both CR and LF delimit progress records, matching FFmpeg console
    output. The process runs in a dedicated session so orphaned descendants are
    still covered by timeout/cancellation.

    Raises:
        ProcessExecutionCancelled: when should_cancel returns True.
        subprocess.TimeoutExpired: when the execution deadline is reached.
        subprocess.CalledProcessError: when the command exits non-zero.
        ProcessTreeTerminationError: when a descendant survives forced shutdown.
    """
    if timeout_seconds is None or timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be greater than zero")
    if poll_interval <= 0:
        raise ValueError("poll_interval must be greater than zero")
    if max_line_bytes <= 0:
        raise ValueError("max_line_bytes must be greater than zero")

    process = _start_managed_process(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    pending = bytearray()
    output_tail = bytearray()
    started_at = time.monotonic()
    eof = False
    terminated = False
    sampled_cpu_seconds = 0.0
    peak_memory_bytes = 0

    def capture_usage():
        nonlocal sampled_cpu_seconds, peak_memory_bytes
        cpu_seconds, resident_bytes = _sample_process_group_usage(process.pid)
        sampled_cpu_seconds = max(sampled_cpu_seconds, cpu_seconds)
        peak_memory_bytes = max(peak_memory_bytes, resident_bytes)
        if on_usage is not None:
            on_usage({
                "cpu_seconds": sampled_cpu_seconds,
                "peak_memory_bytes": peak_memory_bytes or None,
                "memory_duration_ms": max(
                    0, int((time.monotonic() - started_at) * 1000)
                ),
            })

    def deliver_line(raw_line):
        if raw_line:
            on_line(raw_line.decode("utf-8", errors="replace"), process)

    try:
        while True:
            capture_usage()
            if should_cancel is not None and should_cancel():
                terminate_process_tree(process, grace_period=terminate_grace_period)
                terminated = True
                raise ProcessExecutionCancelled("FFmpeg execution was cancelled")

            remaining = timeout_seconds - (time.monotonic() - started_at)
            if remaining <= 0:
                terminate_process_tree(process, grace_period=terminate_grace_period)
                terminated = True
                raise subprocess.TimeoutExpired(
                    command,
                    timeout_seconds,
                    output=bytes(output_tail),
                )

            events = selector.select(timeout=min(poll_interval, remaining))
            capture_usage()
            for key, _ in events:
                chunk = os.read(key.fileobj.fileno(), 65536)
                if not chunk:
                    selector.unregister(key.fileobj)
                    eof = True
                    continue

                output_tail.extend(chunk)
                if len(output_tail) > 65536:
                    del output_tail[:-65536]

                pending.extend(chunk)
                while True:
                    positions = [
                        position for position in (
                            pending.find(b"\n"),
                            pending.find(b"\r"),
                        ) if position >= 0
                    ]
                    if not positions:
                        break
                    end = min(positions)
                    line = bytes(pending[:end])
                    del pending[:end + 1]
                    deliver_line(line)

                if len(pending) > max_line_bytes:
                    deliver_line(bytes(pending))
                    pending.clear()

            if process.poll() is not None and eof:
                if pending:
                    deliver_line(bytes(pending))
                    pending.clear()
                capture_usage()
                return_code = process.wait()
                if return_code:
                    raise subprocess.CalledProcessError(
                        return_code,
                        command,
                        output=bytes(output_tail),
                    )
                return return_code

    except BaseException:
        if not terminated:
            terminate_process_tree(process, grace_period=terminate_grace_period)
        if process.poll() is None:
            process.wait(timeout=terminate_grace_period)
        raise

    finally:
        selector.close()
        if process.stdout is not None:
            process.stdout.close()
