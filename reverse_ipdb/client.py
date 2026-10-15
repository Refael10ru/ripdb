"""Layer 2 — the session: decide whether a debugging session happens at all.

``attach`` dials out, does the handshake, and only if the listener wants us
hands the live socket down to the pty bridge. Resolution order for host/port:
explicit argument > environment variable > built-in default. Everything fails
open: nobody listening, a shell already active in this process, a listener
that says ``continue``, a cooling-down thread -> the breakpoint is a no-op.
"""

import contextlib
import os
import socket
import threading
import time
from collections.abc import Callable
from types import FrameType

from reverse_ipdb import protocol, pty_bridge

# The special name Docker maps to the host machine. Resolves automatically on
# Docker Desktop (macOS/Windows); on Linux the container must be started with
# --add-host=host.docker.internal:host-gateway (compose: extra_hosts).
DOCKER_HOST = "host.docker.internal"

DEFAULT_HOST = DOCKER_HOST
DEFAULT_PORT = 4444
CONNECT_TIMEOUT = 1.0
REPLY_TIMEOUT = 0.5  # how long to wait for the listener's reply line
DEFAULT_COLS, DEFAULT_ROWS = 80, 24

# Only one thread/process gets the interactive shell at a time; others pass
# through. The lock is held for the whole session and released when it ends.
_shell_lock = threading.Lock()


class _ThreadState(threading.local):
    skip_until: float = 0.0  # monotonic time before which this thread does not dial


_local = _ThreadState()


def cool_down(seconds: float) -> None:
    """Told to ``continue`` for ``seconds`` (reverse_ipdb-serve, by default, while
    it's busy): this thread stops dialing for that long, so a hot loop doesn't
    pay a round trip per pass. Thread-local: other threads are unaffected.
    """
    _local.skip_until = time.monotonic() + seconds


def enable_keepalive(sock: socket.socket) -> None:
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


def resolve_target(host: str | None = None, port: int | None = None) -> tuple[str, int]:
    """Resolve the (host, port) to dial, applying env vars and defaults."""
    if not host:
        host = os.environ.get("DEBUG_HOST", DEFAULT_HOST)
    if not port:
        port = int(os.environ.get("DEBUG_PORT", DEFAULT_PORT))
    return host, port


def banner() -> bytes:
    """The first line a target sends: a JSON object saying who we are.

    ``reverse_ipdb: pty`` tells the listener what follows is a raw pty stream;
    ``host``/``pid``/``thread`` are the identity it shows in its roster.
    """
    return protocol.encode({
        "reverse_ipdb": "pty",
        "host": socket.gethostname(),
        "pid": os.getpid(),
        "thread": threading.current_thread().name,
    })


def dial(host: str | None = None, port: int | None = None,
         ) -> tuple[socket.socket, Callable[[], None]] | None:
    """Acquire the shell lock and open a socket to the listener.

    Returns ``(sock, release)`` or ``None``. ``None`` means nobody is listening
    (fail-open), another thread already holds the shell, or this thread is
    still cooling down after a ``continue``. ``release`` closes the socket and
    frees the lock, idempotently; whoever ends the session calls it. It hangs
    up for real (``shutdown`` first) even while a pump is blocked reading.
    """
    if time.monotonic() < _local.skip_until:
        return None  # recently told to continue -> cheap no-op, no connection
    if not _shell_lock.acquire(blocking=False):
        return None  # another thread already has the shell

    try:
        host, port = resolve_target(host, port)
        sock = socket.create_connection((host, port), timeout=CONNECT_TIMEOUT)
    except OSError:
        _shell_lock.release()
        return None  # nobody listening -> keep running
    except BaseException:
        _shell_lock.release()
        raise
    sock.settimeout(None)
    enable_keepalive(sock)
    released = False

    def release() -> None:
        nonlocal released
        if not released:
            released = True
            # shutdown, not just close: while another thread is blocked in
            # recv on this socket, Linux sends no FIN on close() — the
            # listener would never learn we left, and keep the session (and
            # everyone queued behind it) waiting.
            with contextlib.suppress(OSError):
                sock.shutdown(socket.SHUT_RDWR)
            with contextlib.suppress(OSError):
                sock.close()
            _shell_lock.release()

    return sock, release


def handshake(sock: socket.socket) -> tuple[int, int] | None:
    """Send our banner and read the listener's reply (see ``protocol``).

    ``{"cols": .., "rows": ..}`` -> that terminal size, defaults for what's
    missing. ``{"continue": true}`` -> ``None``: no shell for us — and with
    ``"cooldown": <seconds>`` this thread doesn't dial again for that long.
    The listener hanging up -> ``None`` too. A plain socat/nc listener never
    replies, so after ``REPLY_TIMEOUT`` we fall back to the default size.
    """
    sock.settimeout(REPLY_TIMEOUT)
    try:
        sock.sendall(banner())
    except OSError:
        return None
    line = protocol.read_line(sock)
    if line is None:
        return None
    sock.settimeout(None)
    reply = protocol.decode(line)
    if reply.get("continue"):
        if reply.get("cooldown"):
            with contextlib.suppress(TypeError, ValueError):
                cool_down(float(reply["cooldown"]))
        return None
    try:
        return int(reply.get("cols", DEFAULT_COLS)), int(reply.get("rows", DEFAULT_ROWS))
    except (TypeError, ValueError):
        return DEFAULT_COLS, DEFAULT_ROWS


def attach(frame: FrameType, host: str | None = None, port: int | None = None) -> None:
    """Dial out and, if the listener wants us, debug ``frame`` over the socket.

    Layer 2's whole job: dial, handshake, and hand a live socket *down* to the
    pty bridge. Any "no" along the way (nobody listening, shell busy, cooling
    down, told to continue, listener gone) is a silent no-op.
    """
    dialed = dial(host, port)
    if dialed is None:
        return
    sock, release = dialed
    size = handshake(sock)
    if size is None:
        release()
        return
    pty_bridge.run(frame, sock, release, *size)
