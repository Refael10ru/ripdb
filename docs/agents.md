# AI agents

An AI coding agent (Claude Code, Cursor, Codex, Gemini CLI, … — anything that
speaks [MCP](https://modelcontextprotocol.io)) can drive the same debugger you
do: a program stops at `reverse_ipdb.set_trace()`, and the agent inspects it,
steps through it and lets it run on, one ipdb command at a time.

```
target (container, worker, …)            your machine
  reverse_ipdb.set_trace()  ── dials ──▶  reverse_ipdb-mcp  ◀── MCP (stdio) ──  the agent
```

Nothing changes on the target side: the same `set_trace()` dials whichever
listener is on its `DEBUG_HOST:DEBUG_PORT`. `reverse_ipdb-serve` hands it to
your terminal; `reverse_ipdb-mcp` hands it to the agent.

## Set it up

Install with the `mcp` extra and register the server with your agent. For
Claude Code:

```sh
# from GitHub
claude mcp add reverse-ipdb -- \
  uvx --from "reverse_ipdb[mcp] @ git+https://github.com/Refael10ru/reverse_ipdb" reverse_ipdb-mcp

# or from a local checkout
claude mcp add reverse-ipdb -- uv run --directory /path/to/reverse_ipdb --extra mcp reverse_ipdb-mcp
```

Any other MCP client takes the same command in its config, e.g. a project's
`.mcp.json`:

```json
{
  "mcpServers": {
    "reverse-ipdb": {
      "command": "uvx",
      "args": ["--from", "reverse_ipdb[mcp] @ git+https://github.com/Refael10ru/reverse_ipdb",
               "reverse_ipdb-mcp", "--port", "4444"]
    }
  }
}
```

The server listens on `--host` / `--port` (default `0.0.0.0:4444`, like
`reverse_ipdb-serve`) from its first tool call. To keep your own listener
too, give the agent another port (`--port 4445`) and point the targets you
want it to see there (`DEBUG_PORT=4445`).

For Claude Code there is also a skill — the workflow below as instructions
the agent loads when debugging comes up. Copy it into your project or your
home directory:

```sh
mkdir -p .claude/skills && cp -r /path/to/reverse_ipdb/skills/reverse-ipdb .claude/skills/
```

The MCP server sends the same instructions to every client when it connects,
so the skill is optional.

## What the agent gets

| tool | does |
|---|---|
| `wait_for_session(timeout_seconds=60)` | blocks until a program reaches a breakpoint; returns its session id and where it stopped |
| `send_command(session_id, command, timeout_seconds=30)` | runs one ipdb command line (`p x`, `ll`, `w`, `n`, `b f:12`, `!x = 1`, …) and returns its output |
| `wait_for_prompt(session_id, timeout_seconds=30)` | after `n` / `s` / `c`, waits for the program to stop again |
| `interrupt(session_id)` | Ctrl-C: stops the running program at its next line, or interrupts a long command |
| `detach(session_id)` | removes the session's breakpoints and lets the program run on |
| `list_sessions()` | the open sessions, and targets that could not attach (too old) |

Every reply starts with `[s1 · <host> pid=<pid> thread=<name> · <status>]`,
where status is `at the prompt`, `running` (send nothing; wait or interrupt)
or `ended`. Unlike the terminal listener there is no queue: every target that
dials in gets its own session at once (up to 32), so the agent can compare
several containers side by side.

A typical exchange:

```
wait_for_session()           [s1 · web-7f9c pid=12 thread=MainThread · at the prompt]
                             > /app/orders.py(41)total()
                             ----> 41     return sum(line.price for line in order.lines)
send_command(s1, "p len(order.lines), order.currency")
                             (3, 'EUR')
send_command(s1, "pp [l.price for l in order.lines]")
                             [Decimal('9.90'), None, Decimal('4.50')]
detach(s1)                   detached: the program runs on
```

## How it works

The human listener drives a full-screen prompt_toolkit terminal: cursor
moves, colour, a completion menu, all redrawn as you type. That's hard to
read reliably for a program. So the agent's listener asks the target for
**line mode** (one more key in the handshake, see `protocol.py`):

- the banner says the target can do it (`"line": true`), and the reply asks
  for it (`{"cols": 200, "rows": 50, "line": true}`);
- the target then skips prompt_toolkit: no colour, no echo, the pty in
  canonical mode, so each command arrives as one line;
- each prompt is wrapped in **OSC 133** marks (`ESC ] 133 ; A BEL` … `ESC ] 133
  ; B BEL`), the "semantic prompt" sequences terminals use for shell
  integration. Everything before the mark is the previous command's output,
  and after it the target is waiting. No guessing from timing or from text
  that might also be in the program's output.

Everything else is the ordinary session: the same `ReverseIPdb`, the same
guarantees (fail-open, detach never kills, a hang-up releases the target,
nothing outlives the session). When the agent's MCP client exits, the server
hangs up every session and each program runs on.

A target from before line mode can't be driven this way. The agent's listener
tells it to `continue` (it runs on, never paused) and lists it under
`list_sessions` as refused. Update reverse_ipdb on that target.

Without MCP, the same thing is a Python API (`reverse_ipdb.agent`):

```python
from reverse_ipdb.agent import AgentListener

listener = AgentListener(port=4444)
session = listener.wait_for_session(timeout=60)
print(session.wait().output)               # where it stopped
print(session.send("p data").output)
session.detach()
listener.close()
```

## Limits

- One line per command. Chain debugger commands with `;;` (`p a;;p b`), join
  Python statements with `;` (`!a = 1; b = 2`).
- `interact` and `debug` need a terminal; in line mode they are refused.
- No completion: the agent asks the object instead (`p dir(x)`).
- Output longer than 20,000 characters is cut in the middle.
- Ctrl-C has the same limits as in a terminal: in a worker thread it lands
  only once a blocking call (`time.sleep`, a socket read) returns.

## Security

The agent runs code inside your real process, with no authentication, just
as you do from `reverse_ipdb-serve`: only on trusted networks, and only with
an agent whose commands you'd let run there. The MCP tools give it no shell
on your machine, only the debugger prompt of the programs that dial in.

## Existing tools, and why not them

- **[mcp-pdb](https://pypi.org/project/mcp-pdb/)** gives an agent pdb on a
  script it starts itself, or connects *in* to an rpdb that listens. Our
  targets dial *out* (containers you can't reach in), and speak IPython over
  a pty. Its tool shape (`send_pdb_command`) is what `send_command` follows.
- **tmux MCP servers** ([tmux-mcp](https://glama.ai/mcp/servers/fr1sk/tmux-mcp),
  [shell-use](https://glama.ai/mcp/servers/kyuheon-kr/shell-use)) could run
  `reverse_ipdb-serve` in a pane and send keys. That works with no code, but
  the agent then reads a redrawn terminal screen and must guess when a
  command has finished. Line mode's prompt marks answer that exactly.
- **DAP servers** ([mcp-debugger](https://github.com/debugmcpdev/mcp-debugger),
  [dap-mcp](https://glama.ai/mcp/servers/KashunCheng/dap_mcp)) drive debugpy:
  the program has to be started under, or listen for, the debugger. That
  doesn't fit a breakpoint in a container that dials out.
