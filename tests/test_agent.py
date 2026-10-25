"""The agent's side: line mode against real targets, through ``AgentListener``
(the MCP tools on top of it: ``test_mcp.py``).

- A session reads exactly one command's output per prompt, with no escape
  sequences, prompt or echo in it; stepping and breakpoints work; a running
  program reports "running" and Ctrl-C stops it; detach lets it run on.
- Several targets are open at once; a target without line mode is told to
  continue and listed as refused.
- A listener hang-up (the agent going away) releases every target.
"""

import socket

import pytest

from reverse_ipdb import protocol
from reverse_ipdb.agent import AgentListener, Status


@pytest.fixture
def listener():
    lst = AgentListener("127.0.0.1", 0)
    yield lst
    lst.close()


@pytest.fixture
def start(spawn_target, listener):
    return lambda mode="plain", n=3: spawn_target(listener.port, mode, n)


def test_one_command_one_reply_in_plain_text(listener, start, target_lines, ran_on):
    proc = start()
    session = listener.wait_for_session(timeout=20)
    assert session is not None
    stop = session.wait(10)
    assert stop.status is Status.PROMPT
    assert f"----> {target_lines.stop}" in stop.output     # where it stopped
    assert "\x1b" not in stop.output
    assert str(session.identity).endswith("thread=MainThread")

    assert session.send("p data") == ("{'answer': 42, 'n': 3}", Status.PROMPT)
    assert session.send("!x = 5") == ("", Status.PROMPT)
    assert session.send("p x * 2") == ("10", Status.PROMPT)
    assert session.send("p 'ipdb> ' + 'in the output'").output == "'ipdb> in the output'"
    step = session.send("n")
    assert step.status is Status.PROMPT and f"----> {target_lines.stop + 1}" in step.output
    assert session.send("p total").output == "6"
    assert "needs a terminal" in session.send("interact").output  # refused, still here
    assert session.detach() is Status.ENDED
    ran_on(proc)


def test_breakpoints_running_and_interrupt(listener, start, target_lines, ran_on):
    proc = start("spin")
    session = listener.wait_for_session(timeout=20)
    session.wait(10)
    assert "Breakpoint 1" in session.send(f"b {target_lines.ret}").output
    running = session.send("c", timeout=1)                # the spin takes 30s
    assert running.status is Status.RUNNING
    assert session.send("p 1").status is Status.RUNNING   # nothing sent meanwhile
    stopped = session.interrupt()
    assert stopped.status is Status.PROMPT and "Program interrupted" in stopped.output
    assert session.send("p total").output == "6"
    assert session.send("!end = 0") == ("", Status.PROMPT)   # stop spinning
    assert session.detach() is Status.ENDED
    ran_on(proc)


def test_many_targets_at_once_and_old_ones_refused(listener, start, ran_on):
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
        ran_on(proc, "RESULT")
