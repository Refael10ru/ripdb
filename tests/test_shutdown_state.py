"""A child that is *born* in interpreter-shutdown state can still debug.

A process forked while its parent was tearing down inherits
``threading._SHUTTING_DOWN = True`` (``threading._after_fork`` never resets
it). From then on the first ``concurrent.futures.thread`` import — which
IPython needs — raises ``RuntimeError: can't register atexit after shutdown``.
We can't change how such a process was created, so ``pty_bridge`` tolerates
that state while importing the debugger. This test models the child by setting
the flag first thing, then exercises the full session: banner, prompt, detach,
program resumes and exits cleanly.
"""

import re
import socket
import subprocess
import sys
import textwrap
import time

TARGET = textwrap.dedent(
    """
    import threading
    threading._SHUTTING_DOWN = True   # what a child forked during parent teardown inherits
    import reverse_ipdb
    reverse_ipdb.set_trace(host="127.0.0.1", port={port})
    print("RESUMED")
    """
)

ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b[=>]|\x1b\][^\x07]*\x07")


def _recv_until(sock, needle, timeout=15):
    sock.settimeout(0.3)
    buf = bytearray()
    end = time.time() + timeout
    while time.time() < end:
        try:
            chunk = sock.recv(65536)
        except socket.timeout:
            continue
        except OSError:
            break
        if not chunk:
            break
        buf += chunk
        if needle in ANSI.sub("", buf.decode("utf-8", "replace")):
            return True
    return False


def test_debugs_in_a_child_born_shutting_down():
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]

    proc = subprocess.Popen(
        [sys.executable, "-c", TARGET.format(port=port)],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    try:
        srv.settimeout(15)
        conn, _ = srv.accept()

        banner = bytearray()
        conn.settimeout(10)
        while not banner.endswith(b"\n"):
            b = conn.recv(1)
            if not b:
                break
            banner += b
        assert banner.decode().startswith("*** reverse_ipdb/pty"), banner

        conn.sendall(b"80 24\n")
        # Reaching the prompt at all is the fix: without it the IPython import
        # dies with "can't register atexit after shutdown" before any prompt.
        assert _recv_until(conn, "ipdb>"), "never saw the ipdb> prompt"

        conn.sendall(b"\x15")       # Ctrl-U: clear the line
        time.sleep(0.3)
        conn.sendall(b"detach\r")

        stdout, _ = proc.communicate(timeout=15)
        assert "RESUMED" in stdout, stdout
        assert proc.returncode == 0, f"exit={proc.returncode}\n{stdout}"
    finally:
        srv.close()
        if proc.poll() is None:
            proc.kill()
