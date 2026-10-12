"""The handshake lines are JSON objects: tolerant of missing and unknown keys."""

import socket

from reverse_ipdb import protocol


def test_encode_decode_roundtrip_drops_none():
    line = protocol.encode({"cols": 120, "rows": 40, "cooldown": None})
    assert line.endswith(b"\n")
    assert protocol.decode(line) == {"cols": 120, "rows": 40}


def test_decode_tolerates_garbage_and_non_objects():
    assert protocol.decode(b"") == {}
    assert protocol.decode(b"80 24\n") == {}          # the pre-JSON size line
    assert protocol.decode(b"[1, 2]\n") == {}
    assert protocol.decode(b'{"future": 1}\n') == {"future": 1}   # unknown keys survive


def test_read_line_leaves_the_session_bytes_alone():
    a, b = socket.socketpair()
    a.sendall(b'{"cols": 1}\n\x1b[?2004h(ipdb) ')
    assert protocol.read_line(b) == b'{"cols": 1}\n'
    assert b.recv(4096) == b"\x1b[?2004h(ipdb) "
    a.close()
    b.close()


def test_read_line_hangup_is_none_and_silence_is_what_arrived():
    a, b = socket.socketpair()
    a.close()
    assert protocol.read_line(b) is None
    b.close()

    a, b = socket.socketpair()
    b.settimeout(0.05)
    a.sendall(b"par")
    assert protocol.read_line(b) == b"par"               # timed out mid-line
    a.close()
    b.close()
