"""Shared by the agent tests (``test_agent.py``, ``test_mcp.py``): a target
program that stops at a breakpoint and dials a given port, in line mode when
the listener asks for it."""

import subprocess
import sys
import textwrap
import types

import pytest

AGENT_TARGET = textwrap.dedent(
    """
    import sys, time
    import reverse_ipdb

    def work(n):
        data = {"answer": 42, "n": n}
        reverse_ipdb.set_trace(host="127.0.0.1", port=int(sys.argv[1]))
        total = n * 2           # STOP LINE
        if sys.argv[2] == "spin":       # busy for 30s: only Ctrl-C stops it early
            end = time.time() + 30
            while time.time() < end:
                pass
        elif sys.argv[2] == "slow":     # reaches the return line after 1.5s
            time.sleep(1.5)
        return total            # RETURN LINE

    print("RESULT", work(int(sys.argv[3])), flush=True)
    """
)


def _line(marker):
    return next(n for n, text in enumerate(AGENT_TARGET.splitlines(), 1) if marker in text)


@pytest.fixture
def target_lines():
    """Line numbers in the target: where it stops, and its return line."""
    return types.SimpleNamespace(stop=_line("STOP LINE"), ret=_line("RETURN LINE"))


@pytest.fixture
def spawn_target(tmp_path):
    """Start a target dialing ``port`` (mode: plain / spin / slow; it prints
    ``RESULT <2n>`` once it runs on). Killed at the end if still running."""
    script = tmp_path / "agent_target.py"
    script.write_text(AGENT_TARGET)
    procs = []

    def spawn(port, mode="plain", n=3):
        proc = subprocess.Popen([sys.executable, str(script), str(port), mode, str(n)],
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        procs.append(proc)
        return proc

    yield spawn
    for proc in procs:
        if proc.poll() is None:
            proc.kill()
        proc.communicate()


def finished(proc):
    """The target's output and exit code, once it has run to the end."""
    out, _ = proc.communicate(timeout=20)
    return out, proc.returncode


@pytest.fixture
def ran_on():
    """Assert a target ran on to the end: printed its result, exited 0."""
    def check(proc, result="RESULT 6"):
        out, rc = finished(proc)
        assert result in out and rc == 0, out
    return check
