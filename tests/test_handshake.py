"""The banner/reply handshake: a ``continue`` reply means "no shell, run on".

``reverse_ipdb-serve --single`` answers every container beyond the one it is
busy with by ``continue``. The target must then hang up, release the shell
lock and resume — without ever importing IPython.
"""

import socket
import subprocess
import sys

from reverse_ipdb import pty_bridge


def _pair():
    a, b = socket.socketpair()
    return a, b


def test_reply_size_line():
    a, b = _pair()
    a.sendall(b"132 43\n")
    assert pty_bridge._read_reply(b) == (132, 43)
    a.close()
    b.close()


def test_reply_continue_means_no_shell():
    a, b = _pair()
    a.sendall(b"continue\n")
    assert pty_bridge._read_reply(b) is None
    a.close()
    b.close()


def test_reply_hangup_before_any_byte_means_no_shell():
    a, b = _pair()
    a.close()
    assert pty_bridge._read_reply(b) is None
    b.close()


def test_reply_silence_falls_back_to_default_size(monkeypatch):
    # A plain socat/nc listener never replies: default size after the timeout.
    monkeypatch.setattr(pty_bridge, "SIZE_TIMEOUT", 0.05)
    a, b = _pair()
    assert pty_bridge._read_reply(b) == (pty_bridge.DEFAULT_COLS, pty_bridge.DEFAULT_ROWS)
    a.close()
    b.close()


def test_continue_resumes_target_without_importing_ipython():
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(2)
    port = srv.getsockname()[1]
    src = (
        "import sys, reverse_ipdb; "
        f"reverse_ipdb.set_trace(host='127.0.0.1', port={port}); "
        f"reverse_ipdb.set_trace(host='127.0.0.1', port={port}); "   # lock was released
        "print('RESUMED', any(m.split('.')[0] in ('IPython', 'prompt_toolkit') "
        "for m in sys.modules))"
    )
    proc = subprocess.Popen([sys.executable, "-c", src],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        srv.settimeout(15)
        for _ in range(2):
            conn, _ = srv.accept()
            conn.settimeout(10)
            banner = bytearray()
            while not banner.endswith(b"\n"):
                banner += conn.recv(1)
            assert banner.startswith(b"*** reverse_ipdb/pty"), banner
            conn.sendall(b"continue\n")
            assert conn.recv(1) == b""     # target hung up, no pty output
            conn.close()
        stdout, stderr = proc.communicate(timeout=15)
    finally:
        srv.close()
        if proc.poll() is None:
            proc.kill()
    assert proc.returncode == 0, stderr
    assert stdout.strip() == "RESUMED False", stdout + stderr
