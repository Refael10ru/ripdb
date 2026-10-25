"""The wire: the handshake both ends speak, and the socket habits they share.

The handshake is one JSON object per line each way, every key optional. Both
sides fill in defaults for what's missing and ignore keys they don't know, so
either side can add fields without breaking the other: add keys, never
repurpose or remove them. After the reply the socket is a raw vt100 stream.

Target -> listener, the banner::

    {"reverse_ipdb": "pty", "host": "web-7f9c", "pid": 12, "thread": "MainThread",
     "line": true}                         # it can do line mode (below)

Listener -> target, the reply (sent at once, from the acceptor)::

    {"cols": 120, "rows": 40}              # attach: pty size (default 80x24)
    {"cols": 120, "rows": 40, "line": true}  # attach in line mode
    {"continue": true, "cooldown": 1.0}    # no shell; don't dial again for 1s

Line mode is for programs, not people (``reverse_ipdb.agent``): no
prompt_toolkit, no echo, no colour — plain lines in, plain text out, and each
prompt wrapped in ``PROMPT_START`` / ``PROMPT_END`` so the reader knows when
a command's output is complete.
"""

import contextlib
import enum
import json
import socket
from typing import NamedTuple

LINE_LIMIT = 4096  # a handshake line longer than this is not one of ours
PTY = "pty"        # the banner's Field.KIND: a raw pty stream follows

# Line mode's prompt, marked with OSC 133 — the "semantic prompt" sequences of
# terminal shell integration (FinalTerm, iTerm2, VS Code, WezTerm): invisible
# on a terminal, unambiguous to a reader. Output before PROMPT_START belongs to
# the previous command; after PROMPT_END the target is waiting for a line.
PROMPT_START = "\x1b]133;A\x07"
PROMPT_END = "\x1b]133;B\x07"
LINE_PROMPT = f"{PROMPT_START}ipdb> {PROMPT_END}"


class Field(str, enum.Enum):
    """The handshake's keys. Members are ``str``, so they work as JSON keys
    and look up decoded dicts directly. Display them with ``.value``."""

    KIND = "reverse_ipdb"   # banner: what follows the handshake (PTY)
    HOST = "host"           # banner: the target's identity ...
    PID = "pid"
    THREAD = "thread"
    COLS = "cols"           # reply: attach, with this terminal size
    ROWS = "rows"
    LINE = "line"           # banner: line mode supported; reply: use it
    CONTINUE = "continue"   # reply: no shell, run on ...
    COOLDOWN = "cooldown"   # ... and don't dial again for this many seconds


class Identity(NamedTuple):
    """Who a container is, from its banner: shown as ``<host> pid=.. thread=..``
    with whichever parts it sent."""

    host: object = None
    pid: object = None
    thread: object = None

    @classmethod
    def from_banner(cls, msg):
        """Whatever JSON the banner carries, as strings (hashable: the digest
        counts by identity)."""
        return cls(*(None if msg.get(key) is None else str(msg[key])
                     for key in (Field.HOST, Field.PID, Field.THREAD)))

    @property
    def process(self):
        return self._replace(thread=None)

    def __str__(self):
        parts = [] if self.host is None else [str(self.host)]
        parts += [f"{key.value}={value}" for key, value in
                  ((Field.PID, self.pid), (Field.THREAD, self.thread)) if value is not None]
        return " ".join(parts)


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
