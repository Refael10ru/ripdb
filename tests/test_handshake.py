"""The banner/reply handshake: a ``continue`` reply means "no shell, run on".

``reverse_ipdb-serve`` (by default; not with ``--queue``) answers every
container beyond the one it is busy with by ``continue``. The target must then
hang up, release the shell lock and resume — without ever importing IPython.
"""

import socket
import subprocess
import sys
import textwrap
import time

import pytest

from reverse_ipdb import client, protocol


def _pair():
    a, b = socket.socketpair()
    return a, b


@pytest.fixture(autouse=True)
def _no_cooldown_leaks():
    client._local.skip_until = 0.0
    yield
    client._local.skip_until = 0.0


def _reply(a, **fields):
    a.sendall(protocol.encode(fields))


def test_reply_size():
    a, b = _pair()
    _reply(a, cols=132, rows=43)
    assert client.handshake(b) == (132, 43)
    a.close()
    b.close()


def test_reply_tolerates_missing_and_unknown_keys():
    a, b = _pair()
    _reply(a, cols=100, future="ignored")              # no rows -> default rows
    assert client.handshake(b) == (100, client.DEFAULT_ROWS)
    a.close()
    b.close()
    a, b = _pair()
    a.sendall(b"80 24\n")                              # not a JSON object -> defaults
    assert client.handshake(b) == (client.DEFAULT_COLS, client.DEFAULT_ROWS)
    a.close()
    b.close()


def test_reply_continue_means_no_shell_and_no_cooldown_unless_given():
    a, b = _pair()
    _reply(a, **{"continue": True})
    assert client.handshake(b) is None
    assert client._local.skip_until == 0.0
    a.close()
    b.close()


def test_reply_continue_with_cooldown_cools_this_thread_down():
    a, b = _pair()
    _reply(a, **{"continue": True, "cooldown": 2.5})
    before = time.monotonic()
    assert client.handshake(b) is None
    assert before + 2.4 < client._local.skip_until <= time.monotonic() + 2.5
    a.close()
    b.close()


def test_reply_hangup_means_no_shell():
    a, b = _pair()
    a.close()
    assert client.handshake(b) is None
    b.close()


def test_reply_silence_falls_back_to_default_size(monkeypatch):
    # A plain socat/nc listener never replies: default size after the timeout.
    monkeypatch.setattr(client, "REPLY_TIMEOUT", 0.05)
    a, b = _pair()
    assert client.handshake(b) == (client.DEFAULT_COLS, client.DEFAULT_ROWS)
    a.close()
    b.close()


def test_continue_resumes_target_without_importing_ipython():
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(2)
    port = srv.getsockname()[1]
    src = textwrap.dedent(f"""
        import sys, threading, reverse_ipdb
        bp = lambda: reverse_ipdb.set_trace(host='127.0.0.1', port={port})
        bp()                                   # main thread: told to continue
        t = threading.Thread(target=bp); t.start(); t.join()   # other thread: dials too
        bp()                                   # main thread again, cooling down: no dial
        print('RESUMED', any(m.split('.')[0] in ('IPython', 'prompt_toolkit')
                             for m in sys.modules))
        """)
    proc = subprocess.Popen([sys.executable, "-c", src],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        srv.settimeout(15)
        for _ in range(2):
            conn, _ = srv.accept()
            conn.settimeout(10)
            banner = protocol.decode(protocol.read_line(conn))
            assert banner["reverse_ipdb"] == "pty", banner
            _reply(conn, **{"continue": True, "cooldown": 1})
            assert conn.recv(1) == b""     # target hung up, no pty output
            conn.close()
        srv.settimeout(1)
        with pytest.raises(TimeoutError):   # the third breakpoint never dialed
            srv.accept()
        stdout, stderr = proc.communicate(timeout=15)
    finally:
        srv.close()
        if proc.poll() is None:
            proc.kill()
    assert proc.returncode == 0, stderr
    assert stdout.strip() == "RESUMED False", stdout + stderr
