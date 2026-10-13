"""Run-time lifecycle check: nothing the debugger allocates outlives its use.

One target process goes through every way a breakpoint can end and reports,
after each, its threads, file descriptors, traced memory, live debugger
objects, and whether the debugged frame's locals were freed:

- no listener at all (the fail-open no-op), many times;
- a listener that answers ``continue`` (the default mode's turn-away), many times;
- full sessions — detach, ``c`` without breakpoints, and listener hang-ups at
  the prompt, mid-command and while the program runs.

``tests/test_lifecycle_static.py`` is the static half: it lists where each of
these resources is released.
"""

import json
import os
import socket
import subprocess
import sys
import textwrap
import time

from test_completion import _recv_until

from reverse_ipdb import protocol

TARGET = textwrap.dedent(
    """
    import gc, json, os, sys, threading, time, tracemalloc, weakref
    import reverse_ipdb

    PORT, DEAD_PORT, TURNED_AWAY, SESSIONS = (int(a) for a in sys.argv[1:5])
    SESSION_THREADS = ("pump", "ThreadPoolExecutor", "Timer")

    def fds():
        return len(os.listdir("/proc/self/fd"))

    def live(name):
        return sum(type(o).__name__ == name for o in gc.get_objects())

    def settle():
        # a session's own threads finish just after the program resumes
        end = time.time() + 5
        while time.time() < end and any(
                k in t.name for t in threading.enumerate() for k in SESSION_THREADS):
            time.sleep(0.02)

    def traced():
        gc.collect()
        return tracemalloc.get_traced_memory()[0]

    def report(**fields):
        print(json.dumps(fields), flush=True)

    class Payload:                       # what a stopped frame could pin
        def __init__(self):
            self.blob = bytearray(1 << 20)

    def never_called():
        return "never"                   # NEVER_LINE

    markers = []

    def work():
        payload = Payload()
        markers.append(weakref.ref(payload))
        reverse_ipdb.set_trace(host="127.0.0.1", port=PORT)
        return len(payload.blob)

    def phase(name, call, n):
        for _ in range(10):
            call()                       # warm-up: imports, caches
        m0, f0, t0 = traced(), fds(), threading.active_count()
        for _ in range(n):
            call()
        report(phase=name, growth=traced() - m0, fds=fds() - f0,
               threads=threading.active_count() - t0)

    tracemalloc.start()
    phase("no_listener", lambda: reverse_ipdb.set_trace(host="127.0.0.1", port=DEAD_PORT),
          300)
    phase("turned_away", lambda: reverse_ipdb.set_trace(host="127.0.0.1", port=PORT),
          TURNED_AWAY)
    for i in range(SESSIONS):
        work()
        settle()
        freed_without_gc = markers[-1]() is None
        mem = traced()
        report(phase="session", i=i, fds=fds(), threads=sorted(t.name for t in
               threading.enumerate()), traced=mem, freed_without_gc=freed_without_gc,
               payloads_alive=sum(m() is not None for m in markers),
               debuggers=live("ReverseIPdb"), prompts=live("PromptSession"),
               loops=live("_UnixSelectorEventLoop"))
    """
)

MODES = ["detach", "hangup_prompt", "c_nobreaks", "hangup_midcmd", "hangup_running"]
TURNED_AWAY = 150
SESSIONS = 2 * len(MODES)


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _drain_until_closed(conn):
    conn.settimeout(5)
    while conn.recv(65536):
        pass


def _drive(srv, script_path):
    never = next(n for n, line in enumerate(script_path.read_text().splitlines(), 1)
                 if "NEVER_LINE" in line)
    for _ in range(10 + TURNED_AWAY):                     # the turned-away phase
        conn, _ = srv.accept()
        protocol.read_line(conn)
        conn.sendall(protocol.encode({"continue": True}))
        conn.close()
    for i in range(SESSIONS):
        conn, _ = srv.accept()
        conn.settimeout(10)
        protocol.read_line(conn)
        conn.sendall(protocol.encode({"cols": 80, "rows": 24}))
        assert _recv_until(conn, "ipdb>")[1]
        conn.sendall(b"p len(payload.blob)\r")
        assert _recv_until(conn, "1048576")[1]
        mode = MODES[i % len(MODES)]
        if mode == "detach":
            conn.sendall(b"detach\r")
            _drain_until_closed(conn)
        elif mode == "c_nobreaks":
            conn.sendall(b"c\r")
            _drain_until_closed(conn)
        elif mode == "hangup_midcmd":
            conn.sendall(b"!__import__('time').sleep(0.3)\r")
            time.sleep(0.1)
        elif mode == "hangup_running":
            conn.sendall(f"b {never}\r".encode())
            assert _recv_until(conn, "Breakpoint")[1]
            conn.sendall(b"c\r")
            time.sleep(0.2)
        conn.shutdown(socket.SHUT_RDWR)                   # the listener hangs up
        conn.close()


def _run_target(tmp_path):
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(8)
    srv.settimeout(20)
    script = tmp_path / "target.py"
    script.write_text(TARGET)
    proc = subprocess.Popen(
        [sys.executable, str(script), str(srv.getsockname()[1]), str(_free_port()),
         str(TURNED_AWAY), str(SESSIONS)],
        env=dict(os.environ, PROMPT_TOOLKIT_NO_CPR="1"),
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    try:
        _drive(srv, script)
        out, _ = proc.communicate(timeout=60)
    finally:
        srv.close()
        if proc.poll() is None:
            proc.kill()
    assert proc.returncode == 0, out
    return [json.loads(line) for line in out.splitlines() if line.startswith("{")]


def test_nothing_outlives_a_breakpoint(tmp_path):
    rows = _run_target(tmp_path)
    phases = {r["phase"]: r for r in rows if r["phase"] != "session"}
    sessions = [r for r in rows if r["phase"] == "session"]
    assert len(sessions) == SESSIONS, rows

    # No listener / turned away: no debugger is ever built; nothing is kept.
    for name in ("no_listener", "turned_away"):
        p = phases[name]
        assert p["fds"] == 0 and p["threads"] == 0, p
        assert p["growth"] < 16 * 1024, p                  # bytes over hundreds of calls

    # Sessions: after each one, the debugger and everything it touched is gone.
    first = sessions[0]                                    # IPython's own singleton lives on
    for r in sessions:
        assert r["debuggers"] == 0 and r["prompts"] == 0, r
        assert r["payloads_alive"] == 0, r                 # the stopped frame's locals
        assert r["fds"] == first["fds"], r
        assert r["threads"] == first["threads"], r
    assert first["threads"] == ["IPythonHistorySavingThread", "MainThread"], first
    # Freed by reference counting, without waiting for the cycle collector.
    assert all(r["freed_without_gc"] for r in sessions), \
        [(MODES[r["i"] % len(MODES)], r["freed_without_gc"]) for r in sessions]
    # What remains per session is prompt_toolkit's memo of every filter it ever
    # combined (a few KiB per PromptSession, upstream); nothing near the 1 MiB
    # local each stopped frame held.
    per_session = (sessions[-1]["traced"] - sessions[1]["traced"]) / (SESSIONS - 2)
    assert per_session < 32 * 1024, [r["traced"] for r in sessions]


BLOCKED_TARGET = textwrap.dedent(
    """
    import os, sys, threading, time
    import reverse_ipdb

    PORT = int(sys.argv[1])

    def never_called():
        return "never"                   # NEVER_LINE

    def report():
        time.sleep(1.5)                  # the listener hangs up ~0.5s in
        names = sorted(t.name for t in threading.enumerate())
        print("THREADS", ",".join(names), "FDS", len(os.listdir("/proc/self/fd")),
              flush=True)

    reverse_ipdb.set_trace(host="127.0.0.1", port=PORT)   # warm-up session: detached
    base = len(os.listdir("/proc/self/fd"))
    print("BASE", base, flush=True)
    threading.Thread(target=report, name="reporter", daemon=True).start()
    reverse_ipdb.set_trace(host="127.0.0.1", port=PORT)
    time.sleep(3)                        # blocked in C: no trace event for 3s
    print("DONE", flush=True)
    """
)


def test_hang_up_while_blocked_releases_the_session_at_once(tmp_path):
    """The listener hangs up while the program runs and never traces another
    line (blocked here; a thread that ended is the same): the pty, the pumps
    and the prompt thread must go now, not at some next stop that may never
    come."""
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(2)
    srv.settimeout(20)
    script = tmp_path / "blocked.py"
    script.write_text(BLOCKED_TARGET)
    never = next(n for n, line in enumerate(BLOCKED_TARGET.splitlines(), 1)
                 if "NEVER_LINE" in line)
    proc = subprocess.Popen([sys.executable, str(script), str(srv.getsockname()[1])],
                            env=dict(os.environ, PROMPT_TOOLKIT_NO_CPR="1"),
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        for step in ("warm-up", "blocked"):
            conn, _ = srv.accept()
            conn.settimeout(10)
            protocol.read_line(conn)
            conn.sendall(protocol.encode({"cols": 80, "rows": 24}))
            assert _recv_until(conn, "ipdb>")[1]
            if step == "warm-up":
                conn.sendall(b"detach\r")
                _drain_until_closed(conn)
                continue
            conn.sendall(f"b {never}\r".encode())
            assert _recv_until(conn, "Breakpoint")[1]
            conn.sendall(b"c\r")                          # runs into time.sleep(3)
            time.sleep(0.5)
            conn.shutdown(socket.SHUT_RDWR)
            conn.close()
        out, _ = proc.communicate(timeout=20)
    finally:
        srv.close()
        if proc.poll() is None:
            proc.kill()
    assert proc.returncode == 0 and "DONE" in out, out
    base = int(out.split("BASE ")[1].split()[0])
    line = next(ln for ln in out.splitlines() if ln.startswith("THREADS"))
    threads = line.split()[1].split(",")
    assert threads == ["IPythonHistorySavingThread", "MainThread", "reporter"], line
    assert int(line.split("FDS ")[1]) == base, (line, base)
