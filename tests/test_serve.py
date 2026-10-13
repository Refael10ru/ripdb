"""Tests for the fan-in listener: banner parsing, no-container-refused, --no-queue."""

import contextlib
import fcntl
import os
import pty
import re
import select
import socket
import subprocess
import sys
import termios
import threading
import time

import pytest

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
        return "detached"

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


class _Listener:
    """``serve()`` in a thread, with pipes standing in for the terminal."""

    def __init__(self, monkeypatch, keep=True):
        self.port = _free_port()
        self.notes = []
        monkeypatch.setattr(serve_mod, "_note", self.notes.append)
        stdin_r, self.stdin_w = os.pipe()
        self.stdout_r, stdout_w = os.pipe()
        monkeypatch.setattr(sys, "stdin", os.fdopen(stdin_r, "rb", buffering=0))
        monkeypatch.setattr(sys, "stdout", os.fdopen(stdout_w, "wb", buffering=0))
        self.thread = threading.Thread(target=serve_mod.serve,
                                       args=("127.0.0.1", self.port, keep), daemon=True)
        self.thread.start()
        self.conns = []
        assert _wait(lambda: any("listening" in m for m in self.notes))

    def connect(self):
        c = socket.create_connection(("127.0.0.1", self.port), timeout=5)
        self.conns.append(c)
        return c

    def dial(self, name):
        c = self.connect()
        c.sendall(f"*** reverse_ipdb/pty {name} pid=1 thread=MainThread\n".encode())
        assert SIZE_LINE.match(_recv_line(c))
        return c

    def noted(self, text, timeout=5):
        return _wait(lambda: any(text in m for m in self.notes), timeout)

    def close(self):
        for c in self.conns:
            c.close()
        for fd in (self.stdin_w, self.stdout_r):
            with contextlib.suppress(OSError):
                os.close(fd)


def _released(c, timeout=3):
    """True once the listener hangs up on ``c`` (whatever it sent before)."""
    c.settimeout(timeout)
    try:
        while c.recv(65536):
            pass
    except TimeoutError:
        return False
    except OSError:
        return True  # reset counts as released too
    return True


def test_silent_client_holds_up_nobody(monkeypatch):
    """A connection that never sends a banner must not stall the queue; it is
    hung up after BANNER_TIMEOUT."""
    monkeypatch.setattr(serve_mod, "BANNER_TIMEOUT", 0.5)
    lst = _Listener(monkeypatch)
    try:
        silent = lst.connect()
        start = time.time()
        lst.dial("good")                                  # answered despite the silent one
        assert time.time() - start < 0.4
        assert lst.noted("queued: good")
        assert _released(silent)
        assert lst.noted("ignored: 127.0.0.1")
    finally:
        lst.close()


def test_target_not_reading_input_is_dropped(monkeypatch):
    """Keystrokes the target never reads must not wedge the listener: after
    SEND_TIMEOUT it drops that session and the next one comes up."""
    monkeypatch.setattr(serve_mod, "SEND_TIMEOUT", 0.5)
    lst = _Listener(monkeypatch)

    def flood():                                          # type far more than fits in buffers
        with contextlib.suppress(OSError):
            while True:
                os.write(lst.stdin_w, b"x" * 65536)

    try:
        stuck = socket.socket()
        stuck.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
        stuck.connect(("127.0.0.1", lst.port))
        lst.conns.append(stuck)
        stuck.sendall(b"*** reverse_ipdb/pty stuck pid=1 thread=MainThread\n")
        assert SIZE_LINE.match(_recv_line(stuck))
        nxt = lst.dial("next")
        assert lst.noted("attached: stuck")
        threading.Thread(target=flood, daemon=True).start()
        assert lst.noted("dropped (not reading input", timeout=20), lst.notes
        assert lst.noted("attached: next")
        assert _released(stuck, timeout=10)
        nxt.sendall(b"alive")
        assert b"alive" in os.read(lst.stdout_r, 65536)
    finally:
        lst.close()


def test_exit_releases_every_waiting_container(monkeypatch):
    """When the listener stops (here: one-shot after the first session), every
    container it still holds is hung up so it runs on — queued ones and ones
    still mid-handshake — and the port stops accepting."""
    lst = _Listener(monkeypatch, keep=False)
    try:
        first = lst.dial("first")
        queued = lst.dial("queued")
        handshaking = lst.connect()                       # no banner yet (BANNER_TIMEOUT is 5s)
        assert lst.noted("attached: first")
        time.sleep(0.2)
        first.close()                                     # detach -> one-shot serve() returns
        lst.thread.join(timeout=5)
        assert not lst.thread.is_alive()
        assert _released(queued, timeout=2)
        assert _released(handshaking, timeout=2)
        with pytest.raises(OSError):
            socket.create_connection(("127.0.0.1", lst.port), timeout=2)
    finally:
        lst.close()


LISTENER_UNDER_PTY = """
import sys
from reverse_ipdb import serve
sys.exit(serve.main(["--host", "127.0.0.1", "--port", sys.argv[1], "--keep"]))
"""


def test_ctrl_bracket_is_the_listeners_key_on_a_terminal():
    """On a real terminal: Ctrl-C and Ctrl-\\ are just keys for the target.
    Ctrl-] never reaches it: it drops the attached session, a second one right
    after quits, releasing whoever is still queued and restoring the terminal.
    At the idle roster Ctrl-C quits."""
    port = _free_port()
    master, slave = pty.openpty()
    # Like a shell would: the pty is the listener's controlling terminal, so
    # its interrupt key becomes a SIGINT for it (own session, slave as ctty).
    proc = subprocess.Popen([sys.executable, "-c", LISTENER_UNDER_PTY, str(port)],
                            stdin=slave, stdout=slave, stderr=slave, close_fds=True,
                            start_new_session=True,
                            preexec_fn=lambda: fcntl.ioctl(0, termios.TIOCSCTTY, 0))
    os.close(slave)
    out = bytearray()

    def read_until(text, timeout=5):
        end = time.time() + timeout
        while time.time() < end:
            if select.select([master], [], [], 0.1)[0]:
                try:
                    out.extend(os.read(master, 65536))
                except OSError:
                    break
            if text.encode() in out:
                return True
        return False

    def dial(name):
        c = socket.create_connection(("127.0.0.1", port), timeout=5)
        c.sendall(f"*** reverse_ipdb/pty {name} pid=1 thread=MainThread\n".encode())
        assert SIZE_LINE.match(_recv_line(c))
        conns.append(c)
        return c

    conns = []
    try:
        assert read_until("listening")
        a, b, c = dial("aaa"), dial("bbb"), dial("ccc")
        assert read_until("attached: aaa")
        time.sleep(0.3)
        os.write(master, b"\x03\x1c")                     # Ctrl-C, Ctrl-\\: keys for the target
        a.settimeout(3)
        got = b""
        while len(got) < 2:
            got += a.recv(65536)
        assert got == b"\x03\x1c"
        os.write(master, b"\x1d")                          # Ctrl-]: drop
        assert _released(a)
        assert read_until("dropped: aaa") and read_until("attached: bbb")

        time.sleep(serve_mod.QUIT_WINDOW + 0.3)            # a lone Ctrl-]: only drops
        os.write(master, b"\x1d")
        assert _released(b) and read_until("attached: ccc")
        time.sleep(0.3)
        os.write(master, b"\x1d")                          # second Ctrl-] in a row: quit
        assert proc.wait(timeout=5) == 0
        assert _released(c)
        assert termios.tcgetattr(master)[3] & termios.ICANON   # terminal back to cooked
    finally:
        for conn in conns:
            conn.close()
        if proc.poll() is None:
            proc.kill()
        os.close(master)


def test_ctrl_c_at_the_idle_roster_quits():
    port = _free_port()
    master, slave = pty.openpty()
    proc = subprocess.Popen([sys.executable, "-c", LISTENER_UNDER_PTY, str(port)],
                            stdin=slave, stdout=slave, stderr=slave, close_fds=True,
                            start_new_session=True,
                            preexec_fn=lambda: fcntl.ioctl(0, termios.TIOCSCTTY, 0))
    os.close(slave)
    try:
        end, out = time.time() + 5, b""
        while b"listening" not in out and time.time() < end:
            if select.select([master], [], [], 0.1)[0]:
                out += os.read(master, 65536)
        os.write(master, b"\x03")
        assert proc.wait(timeout=5) == 0
    finally:
        if proc.poll() is None:
            proc.kill()
        os.close(master)
