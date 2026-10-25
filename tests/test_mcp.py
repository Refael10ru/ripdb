"""The MCP API (``reverse_ipdb-mcp``), as an agent's MCP client sees it.

Every tool is called through a real MCP client — in memory, and once over
stdio as an agent's client launches it — against real targets:

- the API itself: the six tools, their parameters and defaults, and the
  instructions an agent gets on connecting;
- the whole workflow: wait_for_session -> send_command -> detach, with the
  program running on afterwards;
- "running": a command that resumes the program, wait_for_prompt until the
  next stop, interrupt to stop it early, nothing sent meanwhile;
- several programs at once, each handed out once; an old target refused
  and listed; the server closing releases everyone;
- errors an agent can act on: an unknown session, a port in use, a target
  that went away, output too long, timeouts out of range;
- stdout carrying nothing but JSON-RPC.
"""

import asyncio
import json
import re
import socket
import subprocess
import sys

import pytest

mcp_client = pytest.importorskip("mcp.client")

from reverse_ipdb import agent, mcp_server, protocol  # noqa: E402 - after the mcp check

HEADER = re.compile(r"^\[(s\d+) · (.+) · (at the prompt|running|ended)\]")
SESSION_TOOLS = ("send_command", "wait_for_prompt", "interrupt", "detach")


def _free_port():
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    return port


class _Agent:
    """An MCP client connected to the server, as an agent uses it."""

    def __init__(self, client):
        self.client = client

    async def call(self, tool, **args):
        """The tool's text reply; it must not be an error."""
        result = await self.client.call_tool(tool, args)
        text = result.content[0].text
        assert not result.is_error, text
        return text

    async def fail(self, tool, **args):
        """The text of a tool error, which must be one."""
        result = await self.client.call_tool(tool, args)
        assert result.is_error, result.content[0].text
        return result.content[0].text

    async def session(self, timeout=20):
        """Wait for the next program to stop; its id, identity and reply."""
        text = await self.call("wait_for_session", timeout_seconds=timeout)
        match = HEADER.match(text)
        assert match, text
        return match[1], match[2], text


@pytest.fixture
def server():
    """A server on a free port: ``(port, run)``, where ``run(scenario)``
    connects an agent and runs ``await scenario(agent)``."""
    port = _free_port()
    srv, close = mcp_server.build_server("127.0.0.1", port)

    def run(scenario):
        async def main():
            async with mcp_client.Client(srv) as client:
                return await scenario(_Agent(client))
        return asyncio.run(main())

    yield port, run
    close()


def test_the_tools_parameters_and_instructions_are_the_api(server):
    _, run = server

    async def scenario(agent_):
        listed = (await agent_.client.list_tools()).tools
        return agent_.client.instructions, {t.name: t.input_schema for t in listed}

    instructions, schemas = run(scenario)
    params = {name: (sorted(s["properties"]), sorted(s.get("required", [])))
              for name, s in schemas.items()}
    assert params == {
        "wait_for_session": (["timeout_seconds"], []),
        "list_sessions": ([], []),
        "send_command": (["command", "session_id", "timeout_seconds"], ["command", "session_id"]),
        "wait_for_prompt": (["session_id", "timeout_seconds"], ["session_id"]),
        "interrupt": (["session_id"], ["session_id"]),
        "detach": (["session_id"], ["session_id"]),
    }
    assert schemas["wait_for_session"]["properties"]["timeout_seconds"]["default"] == 60
    assert schemas["send_command"]["properties"]["timeout_seconds"]["default"] == 30
    assert schemas["wait_for_prompt"]["properties"]["timeout_seconds"]["default"] == 30
    for must in ("wait_for_session", "send_command", "wait_for_prompt", "interrupt",
                 "ALWAYS when done", "running", ";;"):
        assert must in instructions, must


def test_listening_with_nothing_to_debug(server):
    port, run = server

    async def scenario(agent_):
        return (await agent_.call("list_sessions"),
                await agent_.call("wait_for_session", timeout_seconds=0.2))

    listed, waited = run(scenario)
    assert listed == f"no open sessions (listening on 127.0.0.1:{port})"
    assert waited.startswith("no program reached a breakpoint within 0.2s")


def test_a_session_from_breakpoint_to_detach(server, spawn_target, target_lines, ran_on):
    port, run = server

    async def scenario(agent_):
        await agent_.call("list_sessions")                  # starts listening
        proc = spawn_target(port)
        sid, identity, stop = await agent_.session()
        assert identity.endswith("thread=MainThread") and "pid=" in identity
        assert stop.startswith(f"[{sid} · {identity} · at the prompt]\n")
        assert f"----> {target_lines.stop}" in stop

        async def cmd(command):
            text = await agent_.call("send_command", session_id=sid, command=command)
            head, _, output = text.partition("\n")
            assert head == f"[{sid} · {identity} · at the prompt]", text
            return output

        assert await cmd("p data") == "{'answer': 42, 'n': 3}"
        assert await cmd("!x = 5") == ""
        assert await cmd("p x * 2;;p 'ipdb> kept'") == "10\n'ipdb> kept'"
        assert f"----> {target_lines.stop + 1}" in await cmd("n")
        assert await cmd("p total") == "6"
        assert "needs a terminal" in await cmd("debug work(1)")
        assert await agent_.call("list_sessions") == f"{sid} · {identity} · at the prompt"

        detached = await agent_.call("detach", session_id=sid)
        assert detached == f"[{sid} · {identity} · ended] detached: the program runs on"
        assert "no open sessions" in await agent_.call("list_sessions")
        after = await agent_.call("send_command", session_id=sid, command="p 1")
        assert "· ended]" in after and "(no command sent)" in after   # known, just over
        assert "· ended]" in await agent_.call("detach", session_id=sid)  # idempotent
        return proc

    ran_on(run(scenario))


def test_running_until_the_next_breakpoint(server, spawn_target, target_lines, ran_on):
    port, run = server

    async def scenario(agent_):
        await agent_.call("list_sessions")
        proc = spawn_target(port, "slow")                   # 1.5s to the return line
        sid, identity, _ = await agent_.session()
        await agent_.call("send_command", session_id=sid, command=f"b {target_lines.ret}")
        running = await agent_.call("send_command", session_id=sid, command="c",
                                    timeout_seconds=0.3)
        assert "· running]" in running and "wait_for_prompt to keep waiting" in running
        busy = await agent_.call("send_command", session_id=sid, command="p 1")
        assert "· running]" in busy and busy.endswith("(no command sent)")
        assert await agent_.call("list_sessions") == f"{sid} · {identity} · running"
        stopped = await agent_.call("wait_for_prompt", session_id=sid, timeout_seconds=10)
        assert "· at the prompt]" in stopped
        assert re.search(rf"-> {target_lines.ret}\s+return total", stopped), stopped  # 1--> here
        await agent_.call("detach", session_id=sid)
        return proc

    ran_on(run(scenario))


def test_interrupt_stops_a_running_program(server, spawn_target, target_lines, ran_on):
    port, run = server

    async def scenario(agent_):
        await agent_.call("list_sessions")
        proc = spawn_target(port, "spin")                   # busy for 30s
        sid, _, _ = await agent_.session()
        armed = await agent_.call("send_command", session_id=sid,
                                  command=f"b {target_lines.ret}")  # c keeps the session
        assert "Breakpoint 1" in armed
        running = await agent_.call("send_command", session_id=sid, command="c",
                                    timeout_seconds=0.5)
        assert "· running]" in running
        waited = await agent_.call("wait_for_prompt", session_id=sid, timeout_seconds=0.3)
        assert "· running]" in waited and "after 0.3s" in waited
        stopped = await agent_.call("interrupt", session_id=sid)
        assert "· at the prompt]" in stopped and "Program interrupted" in stopped
        await agent_.call("send_command", session_id=sid, command="!end = 0")  # stop spinning
        await agent_.call("detach", session_id=sid)
        return proc

    ran_on(run(scenario))


def test_several_programs_old_ones_refused_and_closing_releases_all(
        spawn_target, ran_on):
    port = _free_port()
    srv, close = mcp_server.build_server("127.0.0.1", port)

    async def scenario():
        async with mcp_client.Client(srv) as client:
            agent_ = _Agent(client)
            await agent_.call("list_sessions")
            procs = [spawn_target(port, n=n) for n in (1, 2)]
            first, second = await agent_.session(), await agent_.session()
            assert first[0] != second[0]                    # each handed out once
            again = await agent_.call("wait_for_session", timeout_seconds=0.3)
            assert again.startswith("no program reached a breakpoint")
            answers = sorted([
                (await agent_.call("send_command", session_id=sid, command="p n"))
                .split("\n")[1] for sid, _, _ in (first, second)])
            assert answers == ["1", "2"]

            old = socket.create_connection(("127.0.0.1", port), timeout=5)
            old.sendall(protocol.encode({protocol.Field.KIND: protocol.PTY,
                                         protocol.Field.PID: 7}))
            assert protocol.decode(protocol.read_line(old)).get(protocol.Field.CONTINUE)
            old.close()
            listed = (await agent_.call("list_sessions")).splitlines()
            assert len([line for line in listed if "· at the prompt" in line]) == 2
            assert listed[-1].startswith("refused: pid=7 — no line mode")
            return procs

    procs = asyncio.run(scenario())
    close()                                                 # the agent's client exits
    for proc in procs:
        ran_on(proc, "RESULT")


def test_errors_an_agent_can_act_on(server, spawn_target):
    port, run = server

    async def scenario(agent_):
        for tool in SESSION_TOOLS:
            args = {"command": "p 1"} if tool == "send_command" else {}
            error = await agent_.fail(tool, session_id="s99", **args)
            assert error == f"Error executing tool {tool}: no session 's99': see list_sessions"

        await agent_.call("list_sessions")
        proc = spawn_target(port)
        sid, _, _ = await agent_.session()
        cut = await agent_.call("send_command", session_id=sid, command="p 'x' * 50000")
        assert "characters cut" in cut and len(cut) < agent.MAX_OUTPUT + 200
        proc.kill()                                         # the target goes away
        proc.wait()
        gone = await agent_.call("send_command", session_id=sid, command="p 1",
                                 timeout_seconds=10)
        assert "· ended]" in gone and "(session over: the program runs on)" in gone
        assert "no open sessions" in await agent_.call("list_sessions")
        return proc

    run(scenario)


def test_a_port_in_use_is_reported_to_the_agent():
    taken = socket.create_server(("127.0.0.1", 0))
    port = taken.getsockname()[1]
    srv, close = mcp_server.build_server("127.0.0.1", port)

    async def scenario():
        async with mcp_client.Client(srv) as client:
            return await _Agent(client).fail("list_sessions")

    try:
        error = asyncio.run(scenario())
    finally:
        close()
        taken.close()
    assert f"cannot listen on 127.0.0.1:{port}" in error


def test_timeouts_are_bounded():
    assert mcp_server._clamp(-5) == 0.0
    assert mcp_server._clamp(1e9) == mcp_server.MAX_WAIT
    assert mcp_server._clamp("2.5") == 2.5


def test_the_server_runs_over_stdio(spawn_target, ran_on):
    """The real entry point, as an agent's MCP client launches it: nothing
    but the protocol on stdout, and a program reached through it runs on."""
    stdio = pytest.importorskip("mcp.client.stdio")
    port = _free_port()
    params = stdio.StdioServerParameters(
        command=sys.executable,
        args=["-m", "reverse_ipdb.mcp_server", "--host", "127.0.0.1", "--port", str(port)])

    async def scenario():
        async with mcp_client.Client(params) as client:
            agent_ = _Agent(client)
            assert "detach" in (client.instructions or "")
            await agent_.call("list_sessions")
            proc = spawn_target(port)
            sid, _, _ = await agent_.session()
            reply = await agent_.call("send_command", session_id=sid, command="p data")
            assert reply.endswith("{'answer': 42, 'n': 3}")
            await agent_.call("detach", session_id=sid)
            return proc

    ran_on(asyncio.run(scenario()))


def test_stdout_carries_only_the_protocol():
    """Every line the server writes on stdout is a JSON-RPC message: MCP
    clients read stdout as the protocol, and some reject anything else."""
    port = _free_port()
    proc = subprocess.Popen(
        [sys.executable, "-m", "reverse_ipdb.mcp_server", "--host", "127.0.0.1",
         "--port", str(port)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    requests = [
        {"method": "initialize", "params": {"protocolVersion": "2025-06-18",
                                            "capabilities": {},
                                            "clientInfo": {"name": "test", "version": "0"}}},
        {"method": "tools/list"},
        {"method": "tools/call", "params": {"name": "list_sessions", "arguments": {}}},
        {"method": "tools/call", "params": {"name": "send_command",
                                            "arguments": {"session_id": "s1", "command": "p 1"}}},
    ]
    replies = []
    try:
        for n, request in enumerate(requests, 1):
            proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": n, **request}) + "\n")
            if n == 1:
                proc.stdin.write(json.dumps({"jsonrpc": "2.0",
                                             "method": "notifications/initialized"}) + "\n")
            proc.stdin.flush()
            while True:                                   # its reply, and anything before
                line = proc.stdout.readline()
                assert line, proc.stderr.read()
                message = json.loads(line)                # not JSON: fails here
                assert message["jsonrpc"] == "2.0", line
                if message.get("id") == n:
                    replies.append(message)
                    break
    finally:
        proc.stdin.close()
        proc.wait(timeout=10)
    texts = [r["result"]["content"][0]["text"] for r in replies[2:]]
    assert texts[0] == f"no open sessions (listening on 127.0.0.1:{port})"
    assert "no session 's1'" in texts[1] and replies[3]["result"]["isError"] is True
    for listed in replies[1]["result"]["tools"]:          # read as written, unindented
        assert "\n " not in listed["description"], listed["description"]
