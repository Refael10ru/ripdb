"""End-to-end: with no listener, set_trace is a clean no-op.

The full pty + dot-completion + clean-exit path is covered by test_completion.py.
"""

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
