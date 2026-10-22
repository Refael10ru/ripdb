"""The wire: the handshake both ends speak, and the socket habits they share.

The handshake is one JSON object per line each way, every key optional. Both
sides fill in defaults for what's missing and ignore keys they don't know, so
either side can add fields without breaking the other: add keys, never
repurpose or remove them. After the reply the socket is a raw vt100 stream.

Target -> listener, the banner::

    {"reverse_ipdb": "pty", "host": "web-7f9c", "pid": 12, "thread": "MainThread"}

Listener -> target, the reply (sent at once, from the acceptor)::

    {"cols": 120, "rows": 40}              # attach: pty size (default 80x24)
    {"continue": true, "cooldown": 1.0}    # no shell; don't dial again for 1s
"""

import contextlib
import enum
import json
import socket

LINE_LIMIT = 4096  # a handshake line longer than this is not one of ours
PTY = "pty"        # the banner's Field.KIND: a raw pty stream follows


class Field(str, enum.Enum):
    """The handshake's keys. Members are ``str``, so they work as JSON keys
    and look up decoded dicts directly. Display them with ``.value``."""

    KIND = "reverse_ipdb"   # banner: what follows the handshake (PTY)
    HOST = "host"           # banner: the target's identity ...
    PID = "pid"
    THREAD = "thread"
    COLS = "cols"           # reply: attach, with this terminal size
    ROWS = "rows"
    CONTINUE = "continue"   # reply: no shell, run on ...
    COOLDOWN = "cooldown"   # ... and don't dial again for this many seconds


def encode(fields):
    """One line carrying ``fields`` (a dict keyed by ``Field``)."""
    return (json.dumps(fields) + "\n").encode("utf-8")


def decode(line):
    """The dict a line carries. Anything that isn't a JSON object (``None``
    from ``read_line`` included) decodes to ``{}``."""
    try:
        msg = json.loads(line)
    except (TypeError, ValueError):
        return {}
    return msg if isinstance(msg, dict) else {}


def read_line(sock):
    """One handshake line, byte-at-a-time so nothing past it is consumed —
    those bytes belong to the session that follows.

    ``None`` if the peer hung up or the socket failed; otherwise what arrived
    (possibly nothing, if the socket's timeout ran out first).
    """
    buf = bytearray()
    try:
        while not buf.endswith(b"\n") and len(buf) < LINE_LIMIT:
            chunk = sock.recv(1)
            if not chunk:
                return None
            buf += chunk
    except TimeoutError:
        pass
    except OSError:
        return None
    return bytes(buf)


def keep_alive(sock):
    """Probe an idle connection, so a peer that vanished without a FIN
    (killed, network gone) reads as a hang-up within about a minute."""
    with contextlib.suppress(OSError):
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
    for option, value in ((getattr(socket, "TCP_KEEPIDLE", None), 30),
                          (getattr(socket, "TCP_KEEPINTVL", None), 10),
                          (getattr(socket, "TCP_KEEPCNT", None), 3)):
        if option is not None:  # not every platform exposes the tunables
            with contextlib.suppress(OSError):
                sock.setsockopt(socket.IPPROTO_TCP, option, value)


def hang_up(sock):
    """End a connection so the peer sees it at once.

    ``shutdown`` before ``close``: while another thread is blocked in ``recv``
    on the socket, Linux sends no FIN on a bare ``close()``, and the peer would
    wait forever.
    """
    with contextlib.suppress(OSError):
        sock.shutdown(socket.SHUT_RDWR)
    with contextlib.suppress(OSError):
        sock.close()
