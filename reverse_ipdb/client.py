"""Dialing out from the target process to the listener.

Resolution order for host/port: explicit argument > environment variable >
built-in default. Connecting fails open: if nobody is listening (or the host
is unreachable within the timeout), ``dial`` returns ``None`` and the caller
treats the breakpoint as a no-op.
"""

import contextlib
import os
import socket
import threading

# The special name Docker maps to the host machine. Resolves automatically on
# Docker Desktop (macOS/Windows); on Linux the container must be started with
# --add-host=host.docker.internal:host-gateway (compose: extra_hosts).
DOCKER_HOST = "host.docker.internal"

DEFAULT_HOST = DOCKER_HOST
DEFAULT_PORT = 4444
CONNECT_TIMEOUT = 1.0

# Only one thread/process gets the interactive shell at a time; others pass
# through. The lock is held for the whole session and released when it ends.
_shell_lock = threading.Lock()


def enable_keepalive(sock):
    """Probe an idle connection so a peer that vanished (container killed,
    network gone) reads as a hang-up within about a minute instead of never.
    """
    with contextlib.suppress(OSError):
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
    for name, value in (("TCP_KEEPIDLE", 30), ("TCP_KEEPINTVL", 10), ("TCP_KEEPCNT", 3)):
        try:
            option = getattr(socket, name)
        except AttributeError:
            continue  # not exposed on this platform: the OS default cadence then
        with contextlib.suppress(OSError):
            sock.setsockopt(socket.IPPROTO_TCP, option, value)


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

    Returns ``(sock, release)`` or ``None``. ``None`` means either nobody is
    listening (fail-open) or another thread already holds the shell. ``release``
    frees the lock and is idempotent; the caller calls it when the session ends.
    """
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
        enable_keepalive(sock)
        return sock, release
    except BaseException:
        release()
        raise
