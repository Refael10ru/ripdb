"""The agent's side: line mode against real targets, through ``AgentListener``
and through the MCP tools.

- A session reads exactly one command's output per prompt, with no escape
  sequences, prompt or echo in it; stepping and breakpoints work; a running
  program reports "running" and Ctrl-C stops it; detach lets it run on.
- Several targets are open at once; a target without line mode is told to
  continue and listed as refused.
- A listener hang-up (the agent going away) releases every target.
"""

import asyncio
import socket
import subprocess
import sys
import textwrap

import pytest

from reverse_ipdb import protocol
from reverse_ipdb.agent import AgentListener, Status

TARGET = textwrap.dedent(
    """
    import sys, time
    import reverse_ipdb

    def work(n):
        data = {"answer": 42, "n": n}
        reverse_ipdb.set_trace(host="127.0.0.1", port=int(sys.argv[1]))
        total = n * 2           # STOP LINE
        if sys.argv[2] == "spin":
            end = time.time() + 30
            while time.time() < end:
                pass
        return total            # RETURN LINE

    print("RESULT", work(int(sys.argv[3]) if len(sys.argv) > 3 else 3), flush=True)
    """
)


def _line(marker):
    return next(n for n, text in enumerate(TARGET.splitlines(), 1) if marker in text)


STOP, RETURN = _line("STOP LINE"), _line("RETURN LINE")


@pytest.fixture
def listener():
    lst = AgentListener("127.0.0.1", 0)
    yield lst
    lst.close()


@pytest.fixture
def spawn(tmp_path):
    """Start a target that dials ``port``; killed at the end if still running."""
    script = tmp_path / "target.py"
    script.write_text(TARGET)
    procs = []

    def run(port, mode="plain", n=3):
        proc = subprocess.Popen([sys.executable, str(script), str(port), mode, str(n)],
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        procs.append(proc)
        return proc

    yield run
    for proc in procs:
        if proc.poll() is None:
            proc.kill()
        proc.communicate()


@pytest.fixture
def start(spawn, listener):
    return lambda mode="plain", n=3: spawn(listener.port, mode, n)


def _finished(proc):
    out, _ = proc.communicate(timeout=20)
    return out, proc.returncode


def test_one_command_one_reply_in_plain_text(listener, start):
    proc = start()
    session = listener.wait_for_session(timeout=20)
    assert session is not None
    stop = session.wait(10)
    assert stop.status is Status.PROMPT
    assert f"----> {STOP}" in stop.output and "\x1b" not in stop.output  # where it stopped
    assert str(session.identity).endswith("thread=MainThread")

    assert session.send("p data") == ("{'answer': 42, 'n': 3}", Status.PROMPT)
    assert session.send("!x = 5") == ("", Status.PROMPT)
    assert session.send("p x * 2") == ("10", Status.PROMPT)
    assert session.send("p 'ipdb> ' + 'in the output'").output == "'ipdb> in the output'"
    step = session.send("n")
    assert step.status is Status.PROMPT and f"----> {STOP + 1}" in step.output
    assert session.send("p total").output == "6"
    assert "needs a terminal" in session.send("interact").output  # refused, still here
    assert session.detach() is Status.ENDED
    out, rc = _finished(proc)
    assert "RESULT 6" in out and rc == 0, out


def test_breakpoints_running_and_interrupt(listener, start):
    proc = start("spin")
    session = listener.wait_for_session(timeout=20)
    session.wait(10)
    assert "Breakpoint 1" in session.send(f"b {RETURN}").output
    running = session.send("c", timeout=1)                # the spin takes 30s
    assert running.status is Status.RUNNING
    assert session.send("p 1").status is Status.RUNNING   # nothing sent meanwhile
    stopped = session.interrupt()
    assert stopped.status is Status.PROMPT and "Program interrupted" in stopped.output
    assert session.send("p total").output == "6"
    assert session.send("!end = 0") == ("", Status.PROMPT)   # stop spinning
    assert session.detach() is Status.ENDED
    out, rc = _finished(proc)
    assert "RESULT 6" in out and rc == 0, out


def test_many_targets_at_once_and_old_ones_refused(listener, start):
    procs = [start(n=n) for n in (1, 2, 3)]
    sessions = [listener.wait_for_session(timeout=20) for _ in procs]
    assert len({s.id for s in sessions}) == 3
    assert len(listener.open_sessions()) == 3
    answers = sorted(s.send("p n").output for s in sessions)
    assert answers == ["1", "2", "3"]

    old = socket.create_connection(("127.0.0.1", listener.port), timeout=5)
    old.sendall(protocol.encode({protocol.Field.KIND: protocol.PTY, protocol.Field.PID: 7}))
    reply = protocol.decode(protocol.read_line(old))
    assert reply.get(protocol.Field.CONTINUE) is True     # runs on, never paused
    old.close()
    assert listener.refused[-1][0].pid == "7"
    assert "no line mode" in listener.refused[-1][1]

    listener.close()                                      # the agent goes away
    for proc in procs:
        out, rc = _finished(proc)
        assert "RESULT" in out and rc == 0, out


def test_mcp_tools_drive_a_target(spawn):
    mcp_client = pytest.importorskip("mcp.client")
    from reverse_ipdb import mcp_server  # noqa: PLC0415 - needs the optional mcp

    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    server, close = mcp_server.build_server("127.0.0.1", port)

    async def drive():
        async with mcp_client.Client(server) as client:
            tools = {t.name for t in (await client.list_tools()).tools}
            assert tools == {"wait_for_session", "list_sessions", "send_command",
                             "wait_for_prompt", "interrupt", "detach"}

            async def call(name, **args):
                result = await client.call_tool(name, args)
                return result.content[0].text

            assert "no open sessions" in await call("list_sessions")  # now listening
            proc = spawn(port)
            stop = await call("wait_for_session", timeout_seconds=20)
            assert "at the prompt" in stop and f"----> {STOP}" in stop, stop
            sid = stop.split(" ")[0].lstrip("[")
            reply = await call("send_command", session_id=sid, command="p data")
            assert reply.endswith("{'answer': 42, 'n': 3}"), reply
            assert f"{sid} · " in await call("list_sessions")
            assert "detached" in await call("detach", session_id=sid)
            return proc

    try:
        proc = asyncio.run(drive())
    finally:
        close()
    out, rc = _finished(proc)
    assert "RESULT 6" in out and rc == 0, out


def test_mcp_server_runs_over_stdio(spawn):
    """The real entry point, as an MCP client launches it: nothing but the
    protocol on stdout, and a target reached through it runs on after."""
    mcp_client = pytest.importorskip("mcp.client")
    stdio = pytest.importorskip("mcp.client.stdio")

    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    params = stdio.StdioServerParameters(
        command=sys.executable,
        args=["-m", "reverse_ipdb.mcp_server", "--host", "127.0.0.1", "--port", str(port)])

    async def drive():
        async with mcp_client.Client(params) as client:
            assert "detach" in (client.instructions or "")      # the agent's instructions
            await client.call_tool("list_sessions", {})        # listening now
            proc = spawn(port)
            stop = (await client.call_tool("wait_for_session", {"timeout_seconds": 20}))
            sid = stop.content[0].text.split(" ")[0].lstrip("[")
            await client.call_tool("detach", {"session_id": sid})
            return proc

    proc = asyncio.run(drive())
    out, rc = _finished(proc)
    assert "RESULT 6" in out and rc == 0, out
