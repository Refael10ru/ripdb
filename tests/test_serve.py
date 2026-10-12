"""Tests for the fan-in listener: banner parsing, no-container-refused, --no-queue."""

import os
import re
import socket
import sys
import threading
import time

from reverse_ipdb import serve as serve_mod


def test_read_banner_strips_tag_without_overreading():
    a, b = socket.socketpair()
    # Banner line followed immediately by debugger output on the same stream.
    a.sendall(b"*** reverse_ipdb/pty host pid=1 thread=MainThread\n\x1b[?2004h(ipdb) ")
    ident = serve_mod._read_banner(b)
    assert ident == "host pid=1 thread=MainThread"
    # The bytes after the banner must still be readable (not consumed).
    rest = b.recv(4096)
    assert rest == b"\x1b[?2004h(ipdb) "
    a.close()
    b.close()


def test_read_banner_empty_on_immediate_close():
    a, b = socket.socketpair()
    a.close()
    assert serve_mod._read_banner(b) == ""
    b.close()


SIZE_LINE = re.compile(rb"^\d+ \d+\n$")


def _recv_line(c):
    buf = bytearray()
    while not buf.endswith(b"\n"):
        chunk = c.recv(1)
        if not chunk:
            break
        buf += chunk
    return bytes(buf)


def _wait(pred, timeout=5):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.02)
    return False


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_simultaneous_containers_are_all_queued_none_refused():
    """The core fan-in guarantee: many containers connecting at once all get
    accepted and queued; none is refused and dropped."""
    port = _free_port()
    queued = []

    # Capture roster notices instead of printing them.
    original_note = serve_mod._note
    serve_mod._note = lambda msg: queued.append(msg)

    # Run serve in keep mode in a background thread; we never drive a session,
    # we only assert the acceptor queues everyone.
    t = threading.Thread(target=serve_mod.serve, args=("127.0.0.1", port, True), daemon=True)
    t.start()
    try:
        # Wait for the listener to be up.
        deadline = time.time() + 5
        conns = []
        while time.time() < deadline and not any("listening" in m for m in queued):
            time.sleep(0.02)

        # Fire several "containers" at once.
        for i in range(5):
            c = socket.create_connection(("127.0.0.1", port), timeout=2)
            c.sendall(f"container-{i} pid={100 + i} thread=MainThread\n".encode())
            conns.append(c)

        # All five must show up as queued (none refused).
        deadline = time.time() + 5
        while time.time() < deadline:
            if sum("queued:" in m for m in queued) >= 5:
                break
            time.sleep(0.02)

        queued_lines = [m for m in queued if "queued:" in m]
        assert len(queued_lines) == 5, queued_lines
        for i in range(5):
            assert any(f"container-{i} pid={100 + i}" in m for m in queued_lines)
        # Each one was answered with the terminal size the moment it was queued
        # (not only when attached, which could be minutes later).
        for c in conns:
            c.settimeout(5)
            assert SIZE_LINE.match(_recv_line(c)), c
    finally:
        serve_mod._note = original_note
        for c in conns:
            c.close()


def test_no_queue_continues_everyone_while_busy():
    """--no-queue: one container holds the slot; the rest are told to continue,
    and the slot frees up on detach."""
    port = _free_port()
    notes = []
    original_note, original_pump = serve_mod._note, serve_mod._pump
    serve_mod._note = lambda msg: notes.append(msg)

    def hold_until_remote_closes(conn):   # stand-in for the raw tty bridge
        while conn.recv(65536):
            pass

    serve_mod._pump = hold_until_remote_closes
    t = threading.Thread(target=serve_mod.serve, args=("127.0.0.1", port, True, True),
                         daemon=True)
    t.start()
    conns = []
    try:
        assert _wait(lambda: any("listening" in m for m in notes))

        def dial(name):
            c = socket.create_connection(("127.0.0.1", port), timeout=5)
            c.sendall(f"*** reverse_ipdb/pty {name} pid=1 thread=MainThread\n".encode())
            conns.append(c)
            return c

        first = dial("first")
        assert SIZE_LINE.match(_recv_line(first))             # gets a shell
        assert _wait(lambda: any("attached: first" in m for m in notes))

        second = dial("second")
        assert _recv_line(second) == b"continue\n"            # told to run on
        assert second.recv(1) == b""                           # and hung up
        assert _wait(lambda: any("continued: second" in m for m in notes)), notes
        assert not any("queued: second" in m for m in notes)

        first.close()                                          # detach
        assert _wait(lambda: any("detached: first" in m for m in notes))

        third = dial("third")
        assert SIZE_LINE.match(_recv_line(third))             # slot is free again
        assert _wait(lambda: any("attached: third" in m for m in notes))
    finally:
        for c in conns:
            c.close()
        _wait(lambda: any("detached: third" in m for m in notes))
        serve_mod._note, serve_mod._pump = original_note, original_pump


def test_no_queue_flag_is_wired(monkeypatch):
    captured = {}
    monkeypatch.setattr(serve_mod, "serve",
                        lambda host, port, keep, no_queue: captured.update(
                            keep=keep, no_queue=no_queue))
    serve_mod.main(["--no-queue", "--keep"])
    assert captured == {"keep": True, "no_queue": True}


def test_ctrl_bracket_drops_the_attached_session(monkeypatch):
    """Ctrl-] never reaches the target: the listener closes that session itself
    and moves on to the next one queued — the way out of a stuck target."""
    port = _free_port()
    notes = []
    monkeypatch.setattr(serve_mod, "_note", lambda msg: notes.append(msg))
    stdin_r, stdin_w = os.pipe()
    stdout_r, stdout_w = os.pipe()
    monkeypatch.setattr(sys, "stdin", os.fdopen(stdin_r, "rb", buffering=0))
    monkeypatch.setattr(sys, "stdout", os.fdopen(stdout_w, "wb", buffering=0))
    t = threading.Thread(target=serve_mod.serve, args=("127.0.0.1", port, True), daemon=True)
    t.start()
    conns = []
    try:
        assert _wait(lambda: any("listening" in m for m in notes))

        def dial(name):
            c = socket.create_connection(("127.0.0.1", port), timeout=5)
            c.sendall(f"*** reverse_ipdb/pty {name} pid=1 thread=MainThread\n".encode())
            assert SIZE_LINE.match(_recv_line(c))
            conns.append(c)
            return c

        first, second = dial("first"), dial("second")
        assert _wait(lambda: any("attached: first" in m for m in notes))
        os.write(stdin_w, b"x" + serve_mod.DROP_KEY + b"ignored")
        first.settimeout(5)
        assert first.recv(1) == b"x"       # typed before Ctrl-] still went through
        assert first.recv(1) == b""        # then the listener hung up on it
        assert _wait(lambda: any("dropped: first" in m for m in notes)), notes
        assert _wait(lambda: any("attached: second" in m for m in notes)), notes
        second.sendall(b"hello")
        assert os.read(stdout_r, 5) == b"hello"
    finally:
        for c in conns:
            c.close()
        os.close(stdin_w)
        os.close(stdout_r)
