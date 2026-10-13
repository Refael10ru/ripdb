"""End-to-end: IPython-over-pty gives real dot-completion, and the target
process exits cleanly after detach (no leaked prompt_toolkit threads)."""

import json
import re
import socket
import subprocess
import sys
import textwrap
import time

from reverse_ipdb import protocol

# The completed attribute name ("zqzzmark") is assembled at runtime from
# single characters, so that contiguous string appears NOWHERE in this source.
# If it shows up after we type "obj.zqzz" + TAB, the only possible source is
# the completer evaluating the live object -- it cannot be a source echo.
TARGET = textwrap.dedent(
    """
    import reverse_ipdb
    class Thing: pass
    obj = Thing()
    setattr(obj, "".join(["z", "q", "z", "z", "m", "a", "r", "k"]), 42)
    def main():
        reverse_ipdb.set_trace(host="127.0.0.1", port={port})   # pty + IPython (default)
        print("RESUMED")
    main()
    """
)

ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b[=>]|\x1b\][^\x07]*\x07")


def _recv_until(sock, needle, timeout=15):
    """Accumulate from sock until `needle` appears in the ANSI-stripped text."""
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
            return buf.decode("utf-8", "replace"), True
    return buf.decode("utf-8", "replace"), False


def test_dot_completion_and_clean_exit():
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

        # 1) banner must announce pty mode
        conn.settimeout(10)
        banner = json.loads(protocol.read_line(conn))
        assert banner["reverse_ipdb"] == "pty", banner

        # 2) send our terminal size, then wait for the prompt to come up
        conn.sendall(protocol.encode({"cols": 80, "rows": 24}))
        before, got_prompt = _recv_until(conn, "ipdb>")
        assert got_prompt, "never saw the ipdb> prompt"
        # Echo-proof: the completed string must NOT already be present (e.g. from
        # the debugger echoing source), or step 3 would prove nothing.
        assert "zqzzmark" not in ANSI.sub("", before), "leaked before TAB"

        # 3) type a dotted prefix + TAB; completion must evaluate the live object
        #    and supply the "mark" suffix that is nowhere in the source.
        conn.sendall(b"obj.zqzz\t")
        out, completed = _recv_until(conn, "zqzzmark")
        assert completed, f"TAB did not dot-complete obj.zqzz*; saw:\n{out!r}"

        # 4) clear the line and detach, letting the program continue
        conn.sendall(b"\x15")        # Ctrl-U: kill the current input
        time.sleep(0.3)
        conn.sendall(b"detach\r")

        # 5) the program must resume AND the process must exit cleanly
        stdout, _ = proc.communicate(timeout=15)
        assert "RESUMED" in stdout, stdout
        assert proc.returncode == 0, f"exit={proc.returncode}\n{stdout}"
    finally:
        srv.close()
        if proc.poll() is None:
            proc.kill()
