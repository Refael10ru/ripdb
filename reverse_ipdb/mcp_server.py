"""reverse_ipdb as MCP tools: an AI agent debugs the programs that dial in.

Run by the agent's MCP client (Claude Code, Cursor, Codex, ...) over stdio::

    reverse_ipdb-mcp [--host 0.0.0.0] [--port 4444]

It listens where ``reverse_ipdb-serve`` would (``AgentListener``), so targets
need no change: ``reverse_ipdb.set_trace()`` dials it and, being new enough,
attaches in line mode. Needs the ``mcp`` extra (``reverse_ipdb[mcp]``).
Nothing is written to stdout here: it carries the MCP protocol.
"""

import argparse
import sys

from mcp.server import MCPServer

from reverse_ipdb.agent import AgentListener, Status

MAX_WAIT = 300.0  # seconds; one tool call never blocks longer

INSTRUCTIONS = """\
Debug live Python programs from inside their breakpoints. A program stops by
calling reverse_ipdb.set_trace() (or docker_set_trace() in a container); it
dials this server and waits, paused, until you detach. You get IPython's
debugger (ipdb): every command is one line, exactly as typed at an ipdb>
prompt.

Workflow:
1. wait_for_session — blocks until a program reaches a breakpoint; returns its
   session id and where it stopped. (If the user still has to start the
   program, ask them to, or start it yourself in the background.)
2. send_command(session_id, command) — returns that command's output. Useful:
   p expr / pp expr (print), ll (source around the current line), w (stack),
   u / d (move up / down the stack), a (arguments), !stmt (run a statement,
   e.g. !x = 1), b file:line or b func (breakpoint), n (next line), s (step
   in), r (return), unt (until), c (continue to the next breakpoint).
3. detach(session_id) — ALWAYS when done: the program's breakpoints go and it
   runs on. Never leave a program paused. q / quit / exit also just detach;
   they never kill the program.

Status after each call: "at the prompt" (send the next command), "running"
(n / s / c / a long command has not reached the next stop yet: call
wait_for_prompt, or interrupt to stop it at its next line), or "ended" (the
session is over; the program runs on). c with no breakpoint set detaches.

Rules: one line per command; chain debugger commands with ;; (p a;;p b) and
Python statements with ; (!a = 1; b = 2). interact and debug need
a terminal and are refused. Commands run code inside the user's real process:
read before you mutate state, and never run anything destructive. Long output
is cut in the middle; narrow it (p obj.attr, pp list(d)[:20]).
"""


def _clamp(timeout):
    return max(0.0, min(float(timeout), MAX_WAIT))


def _describe(session, reply, waited=None):
    head = f"[{session.id} · {session.identity} · {reply.status.value}]"
    text = f"{head}\n{reply.output}" if reply.output else head
    if reply.status is Status.RUNNING:
        text += (f"\n(still running{f' after {waited:g}s' if waited else ''}: "
                 "wait_for_prompt to keep waiting, or interrupt to stop it)")
    elif reply.status is Status.ENDED:
        text += "\n(session over: the program runs on)"
    return text


def build_server(host="0.0.0.0", port=4444):
    """The MCP server, and a ``close`` that hangs up every session (their
    programs run on). It starts listening for targets on its first tool call,
    so a port already in use surfaces there, as a tool error."""
    server = MCPServer("reverse_ipdb", instructions=INSTRUCTIONS)
    holder = []

    def listener():
        if not holder:
            try:
                holder.append(AgentListener(host, port))
            except OSError as exc:
                raise RuntimeError(f"cannot listen on {host}:{port}: {exc}") from exc
        return holder[0]

    def session(session_id):
        found = listener().session(session_id)
        if found is None:
            raise ValueError(f"no session {session_id!r}: see list_sessions")
        return found

    @server.tool()
    def wait_for_session(timeout_seconds: float = 60.0) -> str:
        """Wait for a program to reach a breakpoint (reverse_ipdb.set_trace()).
        Returns its session id and where it stopped. Each session is returned
        once; later calls return the next one to arrive."""
        found = listener().wait_for_session(_clamp(timeout_seconds))
        if found is None:
            return (f"no program reached a breakpoint within {timeout_seconds:g}s "
                    f"(listening on {host}:{port}; targets dial DEBUG_HOST:DEBUG_PORT)")
        return _describe(found, found.wait(0))

    @server.tool()
    def list_sessions() -> str:
        """The open debugging sessions, and targets that could not attach."""
        lst = listener()
        lines = [f"{s.id} · {s.identity} · {s.status.value}" for s in lst.open_sessions()]
        lines = lines or [f"no open sessions (listening on {host}:{port})"]
        lines += [f"refused: {who} — {why}" for who, why in list(lst.refused)]
        return "\n".join(lines)

    @server.tool()
    def send_command(session_id: str, command: str, timeout_seconds: float = 30.0) -> str:
        """Run one ipdb command line (p x, ll, w, n, s, c, b f:12, !x = 1, ...)
        in a session at its prompt; returns the output it printed."""
        found = session(session_id)
        if found.status is not Status.PROMPT:
            return _describe(found, found.wait(0)) + "\n(no command sent)"
        return _describe(found, found.send(command, _clamp(timeout_seconds)), timeout_seconds)

    @server.tool()
    def wait_for_prompt(session_id: str, timeout_seconds: float = 30.0) -> str:
        """Wait for a running session (after n, s, c, ...) to stop again;
        returns the output since the last call."""
        found = session(session_id)
        return _describe(found, found.wait(_clamp(timeout_seconds)), timeout_seconds)

    @server.tool()
    def interrupt(session_id: str) -> str:
        """Ctrl-C: stop a running program at the next line it runs, or
        interrupt a command that is still evaluating code."""
        found = session(session_id)
        return _describe(found, found.interrupt())

    @server.tool()
    def detach(session_id: str) -> str:
        """End the session: its breakpoints are removed and the program runs on.
        Always detach when done."""
        found = session(session_id)
        found.detach()
        return (f"[{found.id} · {found.identity} · {found.status.value}] "
                "detached: the program runs on")

    def close():
        if holder:
            holder[0].close()

    return server, close


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="reverse_ipdb-mcp",
        description="MCP server: let an AI agent debug programs that call "
                    "reverse_ipdb.set_trace().")
    parser.add_argument("--host", default="0.0.0.0",
                        help="interface targets dial (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=4444,
                        help="port targets dial (default: 4444)")
    args = parser.parse_args(argv)
    server, close = build_server(args.host, args.port)
    try:
        server.run("stdio")
    except KeyboardInterrupt:
        pass
    finally:
        close()  # every paused program runs on
    return 0


if __name__ == "__main__":
    sys.exit(main())
