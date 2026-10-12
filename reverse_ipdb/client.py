"""Dialing out from the target process to the listener.

Resolution order for host/port: explicit argument > environment variable >
built-in default. Connecting fails open: if nobody is listening (or the host
is unreachable within the timeout), ``dial`` returns ``None`` and the caller
treats the breakpoint as a no-op.
"""

import os
import socket
import threading
import time

# The special name Docker maps to the host machine. Resolves automatically on
# Docker Desktop (macOS/Windows); on Linux the container must be started with
# --add-host=host.docker.internal:host-gateway (compose: extra_hosts).
DOCKER_HOST = "host.docker.internal"

DEFAULT_HOST = DOCKER_HOST
DEFAULT_PORT = 4444
CONNECT_TIMEOUT = 1.0
CONTINUE_COOLDOWN = 1.0  # s: a thread told to "continue" doesn't dial again for this long

# Only one thread/process gets the interactive shell at a time; others pass
# through. The lock is held for the whole session and released when it ends.
_shell_lock = threading.Lock()


class _ThreadState(threading.local):
    skip_until = 0.0  # monotonic time before which this thread does not dial


_local = _ThreadState()


def cool_down():
    """Told to ``continue`` (reverse_ipdb-serve --no-queue while it's busy):
    this thread stops dialing for ``CONTINUE_COOLDOWN`` seconds, so a hot loop
    doesn't pay a round trip per pass. Thread-local: other threads are unaffected.
    """
    _local.skip_until = time.monotonic() + CONTINUE_COOLDOWN


def resolve_target(host=None, port=None):
    """Resolve the (host, port) to dial, applying env vars and defaults."""
    host = host or os.environ.get("DEBUG_HOST", DEFAULT_HOST)
    port = int(port or os.environ.get("DEBUG_PORT", DEFAULT_PORT))
    return host, port


def banner():
    """The first line a target sends: a machine-readable tag + human identity.

    The ``reverse_ipdb/pty`` tag lets the listener recognise us and switch its
    terminal to raw pass-through.
    """
    host = socket.gethostname()
    thread = threading.current_thread().name
    return f"*** reverse_ipdb/pty {host} pid={os.getpid()} thread={thread}\n"


def dial(host=None, port=None):
    """Acquire the shell lock and open a socket to the listener.

    Returns ``(sock, release)`` or ``None``. ``None`` means nobody is listening
    (fail-open), another thread already holds the shell, or this thread was
    told to ``continue`` less than ``CONTINUE_COOLDOWN`` ago. ``release`` frees
    the lock and is idempotent; the caller calls it when the session ends.
    """
    if time.monotonic() < _local.skip_until:
        return None  # recently told to continue -> cheap no-op, no connection
    if not _shell_lock.acquire(blocking=False):
        return None  # another thread already has the shell

    released = False

    def release():
        nonlocal released
        if not released:
            released = True
            _shell_lock.release()

    try:
        host, port = resolve_target(host, port)
        try:
            sock = socket.create_connection((host, port), timeout=CONNECT_TIMEOUT)
        except OSError:
            release()
            return None  # nobody listening -> keep running
        sock.settimeout(None)
        return sock, release
    except BaseException:
        release()
        raise
