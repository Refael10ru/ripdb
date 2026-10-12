"""The wire handshake: one JSON object per line, every key optional.

Both sides ``decode`` with defaults for what's missing and ignore keys they
don't know, so either side can add fields later without breaking the other.
Once the handshake is over the socket is a raw vt100 stream.

Target -> listener, the banner::

    {"reverse_ipdb": "pty", "host": "web-7f9c", "pid": 12, "thread": "MainThread"}

Listener -> target, the reply (sent at once, from the acceptor)::

    {"cols": 120, "rows": 40}              # attach: pty size (default 80x24)
    {"continue": true, "cooldown": 1.0}    # no shell; don't dial again for 1s
"""

import json

LINE_LIMIT = 4096  # a handshake line longer than this is not one of ours


def encode(fields):
    """One line carrying ``fields``; entries whose value is ``None`` are left out."""
    present = {k: v for k, v in fields.items() if v is not None}
    return (json.dumps(present) + "\n").encode("utf-8")


def decode(line):
    """The dict a line carries. Anything that isn't a JSON object decodes to ``{}``."""
    try:
        msg = json.loads(line)
    except ValueError:
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
