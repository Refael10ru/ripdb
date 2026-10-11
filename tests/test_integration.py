"""End-to-end: with no listener, set_trace is a clean no-op.

The full pty + dot-completion + clean-exit path is covered by test_completion.py.
"""

import contextlib
import socket
import subprocess
import sys


def test_no_listener_is_noop():
    # Port 1 refuses immediately; set_trace must be a no-op and the program exits 0.
    src = "import reverse_ipdb; reverse_ipdb.set_trace(host='127.0.0.1', port=1); print('OK')"
    out = subprocess.run(
        [sys.executable, "-c", src],
        capture_output=True, text=True, timeout=10,
    )
    assert out.returncode == 0
    assert "OK" in out.stdout


def test_import_does_not_pull_in_ipython():
    # `import reverse_ipdb` must stay light: importing IPython/prompt_toolkit
    # eagerly drags in concurrent.futures, which raises "can't register atexit
    # after shutdown" when the import lands in a finalizing interpreter.
    src = (
        "import reverse_ipdb, sys; "
        "print(any(m == 'IPython' or m.startswith('IPython.') "
        "or m == 'prompt_toolkit' or m.startswith('prompt_toolkit.') "
        "for m in sys.modules))"
    )
    out = subprocess.run(
        [sys.executable, "-c", src],
        capture_output=True, text=True, timeout=30,
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "False", out.stdout + out.stderr


def test_broken_debugger_env_fails_open():
    # If the deferred IPython import blows up (e.g. mismatched pygments files
    # in the venv), set_trace must report it and let the program run on. The
    # socket and the shell lock must be handed back: a second set_trace in the
    # same process dials again instead of silently no-op'ing.
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(2)
    port = srv.getsockname()[1]
    src = (
        "import sys; sys.modules['IPython'] = None; "   # makes `import IPython...` raise
        "import reverse_ipdb; "
        f"reverse_ipdb.set_trace(host='127.0.0.1', port={port}); "
        f"reverse_ipdb.set_trace(host='127.0.0.1', port={port}); "
        "print('RESUMED')"
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", src],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        srv.settimeout(15)
        received = []
        for _ in range(2):
            conn, _ = srv.accept()
            conn.settimeout(10)
            buf = bytearray()
            with contextlib.suppress(OSError):
                while chunk := conn.recv(65536):
                    buf += chunk
            conn.close()
            received.append(buf.decode("utf-8", "replace"))
        stdout, stderr = proc.communicate(timeout=15)
    finally:
        srv.close()
        if proc.poll() is None:
            proc.kill()

    assert proc.returncode == 0, stderr
    assert "RESUMED" in stdout
    assert "debugger import failed" in stderr and "Error: No module named" in stderr
    for text in received:                       # the listener sees why, both times
        assert text.startswith("*** reverse_ipdb/pty"), text      # banner first
        assert "*** reverse_ipdb: debugger import failed" in text, text
        assert "Error: No module named" in text
