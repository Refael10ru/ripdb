"""Tests for the fan-in listener: banner parsing, no-container-refused, --no-queue."""

import collections
import socket
import threading
import time

from reverse_ipdb import protocol
from reverse_ipdb import serve as serve_mod


def _banner(**fields):
    return protocol.encode({"reverse_ipdb": "pty", **fields})


def test_read_banner_builds_identity_without_overreading():
    a, b = socket.socketpair()
    # Banner line followed immediately by debugger output on the same stream.
    a.sendall(_banner(host="host", pid=1, thread="MainThread") + b"\x1b[?2004h(ipdb) ")
    assert serve_mod._read_banner(b) == "host pid=1 thread=MainThread"
    # The bytes after the banner must still be readable (not consumed).
    assert b.recv(4096) == b"\x1b[?2004h(ipdb) "
    a.close()
    b.close()


def test_read_banner_tolerates_missing_and_unknown_keys():
    a, b = socket.socketpair()
    a.sendall(_banner(pid=7, future="ignored"))
    assert serve_mod._read_banner(b) == "pid=7"
    a.close()
    b.close()


def test_read_banner_empty_on_immediate_close_or_garbage():
    a, b = socket.socketpair()
    a.close()
    assert serve_mod._read_banner(b) == ""
    b.close()
    a, b = socket.socketpair()
    a.sendall(b"*** reverse_ipdb/pty host pid=1 thread=MainThread\n")   # pre-JSON banner
    assert serve_mod._read_banner(b) == ""
    a.close()
    b.close()


def _is_size(line):
    msg = protocol.decode(line)
    return isinstance(msg.get("cols"), int) and isinstance(msg.get("rows"), int)


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
            c.sendall(_banner(host=f"container-{i}", pid=100 + i, thread="MainThread"))
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
            assert _is_size(_recv_line(c)), c
    finally:
        serve_mod._note = original_note
        for c in conns:
            c.close()


def test_no_queue_continues_everyone_while_busy():
    """--no-queue: one container holds the slot; the rest are told to continue
    (reported as one digest line on detach), and the slot frees up on detach."""
    port = _free_port()
    notes = []
    original_note, original_pump = serve_mod._note, serve_mod._pump
    serve_mod._note = lambda msg: notes.append(msg)

    def hold_until_remote_closes(conn):   # stand-in for the raw tty bridge
        while conn.recv(65536):
            pass

    serve_mod._pump = hold_until_remote_closes
    t = threading.Thread(target=serve_mod.serve, args=("127.0.0.1", port, True, 2.5),
                         daemon=True)
    t.start()
    conns = []
    try:
        assert _wait(lambda: any("listening" in m for m in notes))

        def dial(name):
            c = socket.create_connection(("127.0.0.1", port), timeout=5)
            c.sendall(_banner(host=name, pid=1, thread="MainThread"))
            conns.append(c)
            return c

        first = dial("first")
        assert _is_size(_recv_line(first))                    # gets a shell
        assert _wait(lambda: any("attached: first" in m for m in notes))

        for _ in range(3):                                     # a loop hitting its breakpoint
            second = dial("second")
            assert protocol.decode(_recv_line(second)) == {"continue": True, "cooldown": 2.5}
            assert second.recv(1) == b""                       # and hung up
        assert not any("continued" in m or "queued: second" in m for m in notes), notes

        first.close()                                          # detach
        assert _wait(lambda: any("detached: first" in m for m in notes))
        # The turned-away arrivals are reported once, after the session, not
        # sprayed over the raw-mode shell while it was attached.
        assert _wait(lambda: any(
            m == "continued ×3 while attached: second pid=1 thread=MainThread ×3"
            for m in notes)), notes

        third = dial("third")
        assert _is_size(_recv_line(third))                    # slot is free again
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
    assert captured == {"keep": True, "no_queue": 1.0}      # default cooldown
    serve_mod.main(["--no-queue", "0.5"])
    assert captured == {"keep": False, "no_queue": 0.5}
    serve_mod.main([])
    assert captured == {"keep": False, "no_queue": None}    # fan-in


def test_continued_digest_groups_by_container(capsys):
    counts = collections.Counter()
    serve_mod._note_continued(counts)                 # nothing to say
    assert capsys.readouterr().err == ""

    counts.update(["a"] * 5 + ["b"] * 2 + ["c", "d", "e"])
    serve_mod._note_continued(counts)
    assert capsys.readouterr().err == "*** continued ×10 while attached: a ×5, b ×2, c, +2 more\n"
    assert not counts                                 # drained


def test_continued_digest_groups_threads_by_process(capsys):
    # Many threads of one process looping through the breakpoint are one entry.
    counts = collections.Counter()
    for t in range(12):
        counts[f"worker-3a1 pid=9 thread=Thread-{t}"] += 100
    counts["web-7f9c pid=12 thread=MainThread"] += 1
    serve_mod._note_continued(counts)
    assert capsys.readouterr().err == (
        "*** continued ×1201 while attached: "
        "worker-3a1 pid=9 (12 threads) ×1200, web-7f9c pid=12 thread=MainThread\n"
    )
