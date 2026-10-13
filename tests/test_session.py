"""End-to-end session lifecycle against a real target subprocess.

- Leaving the shell must reach the listener as a hang-up *at once*, even
  while the program keeps running — otherwise a fan-in listener stays on the
  finished session and the next queued container never comes up.
- ``c`` with no breakpoints detaches (nothing could bring the prompt back).
- Ctrl-C from the listener interrupts a command evaluating code (main thread
  or not) or stops the running program at its next line — and never raises
  into the program's own code.

These tests drive a target subprocess over a raw socket, exactly as
``reverse_ipdb-serve`` does.
"""

import contextlib
import os
import socket
import subprocess
import sys
import textwrap
import time

import pytest
from test_completion import ANSI, _recv_until

from reverse_ipdb import protocol

TARGET = textwrap.dedent(
    """
    import threading, time
    import reverse_ipdb

    def busy(seconds):          # pure-Python loop: no blocking call to break out of
        end = time.time() + seconds
        while time.time() < end:
            pass
        return "busy done"

    def main():
        reverse_ipdb.set_trace(host="127.0.0.1", port={port})
        print("RESUMED", flush=True)
        {between}
        reverse_ipdb.set_trace(host="127.0.0.1", port={port})
        print("RESUMED AGAIN", flush=True)

    {run}
    """
)


def _expect(conn, *needles, timeout=10):
    """Read until every needle has appeared, in order, in the ANSI-stripped
    stream (one buffer, so a needle in the same chunk as the last is seen)."""
    conn.settimeout(0.3)
    buf = bytearray()
    end = time.time() + timeout
    while time.time() < end:
        try:
            chunk = conn.recv(65536)
        except TimeoutError:
            continue
        except OSError:
            break
        if not chunk:
            break
        buf += chunk
        text, pos = ANSI.sub("", buf.decode("utf-8", "replace")), 0
        for needle in needles:
            pos = text.find(needle, pos)
            if pos < 0:
                break
            pos += len(needle)
        else:
            return text
    raise AssertionError(f"never saw {needles!r} in order; got:\n{bytes(buf)!r}")


def _listener():
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(2)
    srv.settimeout(15)
    return srv


@pytest.fixture
def target(tmp_path):
    """Start the TARGET program (from a file, so line breakpoints resolve).
    CPR is off: with no terminal answering cursor-position requests,
    prompt_toolkit would otherwise hold every prompt for its 2s CPR timeout."""
    procs = []

    def start(port, between="pass", run="main()"):
        script = tmp_path / "target.py"
        script.write_text(TARGET.format(port=port, between=between, run=run))
        env = dict(os.environ, PROMPT_TOOLKIT_NO_CPR="1")
        proc = subprocess.Popen(
            [sys.executable, str(script)], env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
        procs.append(proc)
        return proc

    yield start
    for proc in procs:
        if proc.poll() is None:
            proc.kill()


def _attach(srv):
    """Accept one dial, answer the size line, wait for the prompt."""
    conn, _ = srv.accept()
    conn.settimeout(10)
    while not conn.recv(1).endswith(b"\n"):
        pass
    conn.sendall(protocol.encode({"cols": 80, "rows": 24}))
    _, got_prompt = _recv_until(conn, "ipdb>")
    assert got_prompt, "never saw the ipdb> prompt"
    return conn


def _finish(proc, srv):
    srv.close()
    try:
        stdout, _ = proc.communicate(timeout=15)
    except subprocess.TimeoutExpired:
        proc.kill()
        raise
    return stdout, proc.returncode


def _hung_up_within(conn, seconds):
    """True once the target closes the connection, False if still open."""
    conn.settimeout(seconds)
    try:
        while conn.recv(65536):
            pass
    except TimeoutError:
        return False
    return True


def test_detach_hangs_up_while_the_program_keeps_running(target):
    """The queue-stall regression: the program sleeps on after ``detach``, so
    process exit can't close the socket for it; the bridge must hang up."""
    srv = _listener()
    proc = target(srv.getsockname()[1], between="time.sleep(30)")
    try:
        conn = _attach(srv)
        conn.sendall(b"detach\r")
        assert _hung_up_within(conn, 3), "listener never saw the detach"
        assert proc.poll() is None   # still running: it's the bridge that hung up
    finally:
        srv.close()


def test_continue_without_breakpoints_detaches_and_can_dial_again(target):
    """``c`` with nothing armed closes the socket (so a fan-in listener moves
    on) and releases the shell lock: the next set_trace dials again."""
    srv = _listener()
    proc = target(srv.getsockname()[1])
    try:
        conn = _attach(srv)
        conn.sendall(b"c\r")
        _expect(conn, "no breakpoints set")
        conn.settimeout(5)
        rest = b""
        while True:
            chunk = conn.recv(4096)
            if not chunk:
                break
            rest += chunk
        assert "ipdb>" not in ANSI.sub("", rest.decode("utf-8", "replace"))
        conn.close()
        conn2 = _attach(srv)                 # the second set_trace dialed again
        conn2.sendall(b"detach\r")
        stdout, rc = _finish(proc, srv)
        assert stdout.index("RESUMED") < stdout.index("RESUMED AGAIN") and rc == 0, stdout
    finally:
        srv.close()


def test_continue_with_breakpoints_keeps_the_session(target):
    srv = _listener()
    proc = target(srv.getsockname()[1])
    try:
        conn = _attach(srv)
        line = next(n for n, text in enumerate(TARGET.splitlines(), 1)
                    if "RESUMED AGAIN" in text)
        conn.sendall(f"b {line}\r".encode())  # the print("RESUMED AGAIN") line
        _expect(conn, "Breakpoint 1", "ipdb>")
        conn.sendall(b"c\r")
        _expect(conn, str(line), "RESUMED AGAIN", "ipdb>")   # stopped there: source + prompt
        conn.sendall(b"detach\r")
        stdout, rc = _finish(proc, srv)
        assert "RESUMED AGAIN" in stdout and rc == 0, stdout
    finally:
        srv.close()


def test_listener_hang_up_at_the_prompt_releases_the_target(target):
    """The listener drops or quits while the target sits at its prompt: the
    target thread must not stay paused — it detaches and runs on at once."""
    srv = _listener()
    proc = target(srv.getsockname()[1])
    try:
        conn = _attach(srv)
        start = time.time()
        conn.shutdown(socket.SHUT_RDWR)
        conn.close()
        conn2 = _attach(srv)                 # it ran on to its second set_trace
        assert time.time() - start < 5
        conn2.shutdown(socket.SHUT_RDWR)
        conn2.close()
        stdout, rc = _finish(proc, srv)
        assert "RESUMED AGAIN" in stdout and rc == 0, stdout
    finally:
        srv.close()


def test_listener_hang_up_mid_command_releases_after_it(target):
    """Dropped while a command runs: the command finishes — its output going
    nowhere, neither blocking nor failing — then the target detaches instead
    of prompting a listener that's gone. Nothing raised into the program."""
    srv = _listener()
    proc = target(srv.getsockname()[1])
    try:
        conn = _attach(srv)
        conn.sendall(b"!time.sleep(1); print('x' * 200000)\r")   # output after the drop
        time.sleep(0.3)
        conn.shutdown(socket.SHUT_RDWR)
        conn.close()
        _attach(srv).close()                 # reached the second set_trace
        stdout, rc = _finish(proc, srv)
        assert "RESUMED AGAIN" in stdout and rc == 0, stdout
        assert "Traceback" not in stdout, stdout
    finally:
        srv.close()


def test_listener_hang_up_while_running_detaches_at_next_stop(target):
    """Dropped after ``c`` with a breakpoint armed: reaching the breakpoint
    must not pause the program for a listener that's gone."""
    srv = _listener()
    proc = target(srv.getsockname()[1], between="time.sleep(0.5)")
    try:
        conn = _attach(srv)
        line = next(n for n, text in enumerate(TARGET.splitlines(), 1)
                    if "RESUMED AGAIN" in text)
        conn.sendall(f"b {line}\r".encode())
        _expect(conn, "Breakpoint 1", "ipdb>")
        conn.sendall(b"c\r")
        time.sleep(0.2)
        conn.shutdown(socket.SHUT_RDWR)
        conn.close()
        srv.settimeout(1.5)                  # the second set_trace dials: answer silently
        with contextlib.suppress(TimeoutError):
            srv.accept()[0].close()
        stdout, rc = _finish(proc, srv)
        assert "RESUMED AGAIN" in stdout and rc == 0, stdout
    finally:
        srv.close()


def test_ctrl_c_interrupts_a_blocking_command(target):
    """``!time.sleep(30)`` then Ctrl-C: back at the prompt long before 30s."""
    srv = _listener()
    proc = target(srv.getsockname()[1])
    try:
        conn = _attach(srv)
        conn.sendall(b"!time.sleep(30)\r")
        time.sleep(0.5)
        start = time.time()
        conn.sendall(b"\x03")
        _expect(conn, "KeyboardInterrupt", "ipdb>")
        assert time.time() - start < 10
        conn.sendall(b"detach\r")
        stdout, rc = _finish(proc, srv)
        assert "RESUMED AGAIN" in stdout and rc == 0, stdout
    finally:
        srv.close()


def test_ctrl_c_interrupts_p_in_a_worker_thread(target):
    """Off the main thread the interrupt is an async exception: a pure-Python
    loop (here inside ``p``) stops at once, with no "pending" note."""
    srv = _listener()
    proc = target(srv.getsockname()[1],
                  run="t = threading.Thread(target=main); t.start(); t.join()")
    try:
        conn = _attach(srv)
        conn.sendall(b"p busy(30)\r")
        time.sleep(0.5)
        conn.sendall(b"\x03")
        text = _expect(conn, "KeyboardInterrupt", "ipdb>")
        time.sleep(1.5)                      # past the "pending" note's delay
        conn.sendall(b"p 6 * 7\r")          # (an empty line would repeat busy(30))
        text += _expect(conn, "42", "ipdb>")
        assert "pending" not in text
        conn.sendall(b"detach\r")
        stdout, rc = _finish(proc, srv)
        assert "RESUMED AGAIN" in stdout and rc == 0, stdout
    finally:
        srv.close()


def test_ctrl_c_in_a_blocking_call_off_the_main_thread_says_it_is_pending(target):
    srv = _listener()
    proc = target(srv.getsockname()[1],
                  run="t = threading.Thread(target=main); t.start(); t.join()")
    try:
        conn = _attach(srv)
        conn.sendall(b"!time.sleep(4)\r")
        time.sleep(0.5)
        conn.sendall(b"\x03")
        _expect(conn, "KeyboardInterrupt pending", "KeyboardInterrupt", "ipdb>")
        conn.sendall(b"detach\r")
        stdout, rc = _finish(proc, srv)
        assert "RESUMED AGAIN" in stdout and rc == 0, stdout
    finally:
        srv.close()


def test_ctrl_c_while_running_stops_at_the_next_line(target):
    """After ``c`` (breakpoint armed far ahead) the program loops for 5s;
    Ctrl-C stops it at its next line, without raising into it. Repeated
    presses change nothing."""
    srv = _listener()
    proc = target(srv.getsockname()[1],
                  between="end = time.time() + 5\n    while time.time() < end: pass")
    try:
        conn = _attach(srv)
        line = next(n for n, text in enumerate(TARGET.splitlines(), 1)
                    if "RESUMED AGAIN" in text) + 1   # the inserted loop adds a line
        conn.sendall(f"b {line}\r".encode())
        _expect(conn, "Breakpoint 1", "ipdb>")
        conn.sendall(b"c\r")
        time.sleep(1)
        start = time.time()
        conn.sendall(b"\x03")
        time.sleep(0.05)
        conn.sendall(b"\x03")
        _expect(conn, "Program interrupted", "while time.time() < end", "ipdb>")
        assert time.time() - start < 3
        conn.sendall(b"detach\r")
        stdout, rc = _finish(proc, srv)
        assert "RESUMED AGAIN" in stdout and rc == 0, stdout
        assert "Traceback" not in stdout, stdout
    finally:
        srv.close()
