# reverse_ipdb — reverse-connecting pdb

[![CI](https://github.com/Refael10ru/ripdb/actions/workflows/ci.yml/badge.svg)](https://github.com/Refael10ru/ripdb/actions/workflows/ci.yml)

Debugging a process that has no terminal — a container, a daemon, a CI worker — is
awkward: you can't attach a shell to something that isn't reading from one. `reverse_ipdb`
flips the direction. Instead of *you* attaching *in*, the target process **dials out**
to a listener you run and hands you an IPython shell (with completion) over the socket.

> Integrating or extending reverse_ipdb from another project/agent? See
> **[AGENTS.md](AGENTS.md)** for the public API, wire protocol, invariants, and
> the dev/CI workflow.

```
┌───────────────────────────┐      TCP connect() OUT      ┌───────────────────────────┐
│  TARGET PROCESS           │ ──────────────────────────► │  YOUR MACHINE             │
│  import reverse_ipdb      │                             │  reverse_ipdb-serve       │
│  reverse_ipdb.set_trace() │ ◄─── IPython over socket ──►│  (gives you the shell)    │
└───────────────────────────┘                             └───────────────────────────┘
```

If nobody is listening, `set_trace()` is a **no-op** and the program runs on untouched.

## Install

This project is managed with [uv](https://docs.astral.sh/uv/):

```sh
uv sync                        # create the venv and install deps (incl. dev tools)
uv run reverse_ipdb-serve      # run the listener
uv run python your_script.py   # run anything inside the environment
```

Prefer plain pip? `pip install -e .` still works (the dev tools live in the
`dev` dependency group: `uv sync` installs them, or `pip install pytest ruff
mypy`).

## Use

**In the target** (container/daemon/worker), point it at your machine and drop a
breakpoint in the code:

```python
import reverse_ipdb
reverse_ipdb.set_trace()                          # host/port from DEBUG_HOST / DEBUG_PORT
reverse_ipdb.set_trace(host="10.0.0.5", port=4444)  # or set them explicitly
```

`set_trace()` drops into the **IPython** debugger.
(`reverse_ipdb.set_trace_ipython()` is an explicit alias.)

Debugging from inside a container? Use `reverse_ipdb.docker_set_trace()` — the
same thing but with the default host set to `host.docker.internal`, which
routes to the host machine. On Docker Desktop that resolves automatically; on
Linux start the container with `--add-host=host.docker.internal:host-gateway`
(compose: `extra_hosts`).

You get **real tab/dot-completion**, history and colour: `set_trace()` runs
IPython's debugger over a pseudo-terminal, so `order.<TAB>` completes against
the live frame — all computed on the target and rendered to your terminal.
Use the bundled `reverse_ipdb-serve` (or `socat …,raw,echo=0`) as the listener so
your terminal is in raw mode. POSIX only — the package imports `termios`, so
`import reverse_ipdb` requires a Unix platform. See [docs/completion.md](docs/completion.md).

Configuration resolves **argument → environment variable → default**:

| Setting | Env var      | Default               |
|---------|--------------|-----------------------|
| host    | `DEBUG_HOST` | `host.docker.internal`|
| port    | `DEBUG_PORT` | `4444`                |

**On your machine**, catch the session. Either use the bundled listener:

```sh
uv run reverse_ipdb-serve               # serve the first breakpoint, then exit
uv run reverse_ipdb-serve --keep        # keep serving: after a detach, the next breakpoint
uv run reverse_ipdb-serve --keep --cooldown 5  # turned-away threads stay away 5s (default 1)
uv run reverse_ipdb-serve --keep --queue  # queue: everyone waits, walk them one at a time
uv run reverse_ipdb-serve --port 4444   # pick the port
# (or: python -m reverse_ipdb.serve, once the package is installed)
```

…or a plain socket tool, for a single session with no roster. `socat` in raw
mode gives the full experience. `escape=0x1d` makes Ctrl-] end the session,
as it does in the bundled listener:

```sh
socat STDIO,raw,echo=0,escape=0x1d TCP-LISTEN:4444,reuseaddr
```

`nc -l 4444` also works for a quick look. Commands, `detach`, and Ctrl-C all
behave: Ctrl-C ends nc and the target runs on. But nc is line-buffered and
echoes locally, so tab completion doesn't work, every line shows twice, the
target's JSON banner line is printed first, and Ctrl-C can't interrupt a
command on the target. With either tool, the target waits half a second for
the size reply that `reverse_ipdb-serve` would send, then uses 80×24.

### Many containers

This is what the reverse model is *for*. When your code runs across many
containers, point them all at the **one** listener and run it with `--keep`:

```sh
# every container (e.g. in its entrypoint / compose env)
DEBUG_HOST=<your-machine> DEBUG_PORT=4444   # reverse_ipdb.set_trace() reads these

# you, once
uv run reverse_ipdb-serve --keep
```

By default **the first container to hit a breakpoint gets the shell, and
nobody waits for it**. Every other one that hits a breakpoint meanwhile is told
to **continue** on the spot and runs on untouched, exactly as if no listener
were running. That thread then stays away for a cooldown (1 s, or
`--cooldown SECONDS`), so a hot loop doesn't pay a round trip per pass. After
you detach, the next breakpoint anywhere comes up:

```
*** reverse_ipdb listening on 0.0.0.0:4444 (no queue, 1s cooldown, keep-alive) — Ctrl-C to quit; while attached, Ctrl-] drops the session (twice: quit)
*** queued: web-7f9c pid=12 thread=MainThread  (1 waiting)
*** attached: web-7f9c pid=12 thread=MainThread
   … debug web-7f9c; meanwhile other breakpoints just continue, silently …
*** detached: web-7f9c pid=12 thread=MainThread
*** continued ×1201 while attached: worker-3a1 pid=9 (12 threads) ×1200, worker-4b0 pid=3 thread=MainThread
```

While you're attached the terminal belongs to that shell, so the listener
stays quiet; the containers it turned away meanwhile are summed up in one
line when you detach, grouped by process (a loop across many threads is one
entry, not one per thread).

**Want them to wait instead?** Add `--queue`. Then the listener accepts
**every** container that hits a breakpoint right away — none is ever refused —
and each stays paused at its `set_trace` until it's your turn. You get a live
roster and walk them one at a time:

```
*** reverse_ipdb listening on 0.0.0.0:4444 (queue, keep-alive) — Ctrl-C to quit; while attached, Ctrl-] drops the session (twice: quit)
*** queued: web-7f9c pid=12 thread=MainThread  (1 waiting)
*** queued: worker-3a1 pid=9 thread=Thread-2    (2 waiting)
*** attached: web-7f9c pid=12 thread=MainThread  (1 still waiting)
   … debug web-7f9c, then `detach` …
*** detached: web-7f9c pid=12 thread=MainThread
*** attached: worker-3a1 pid=9 thread=Thread-2
```

A session ends with `detach` (or `q`, Ctrl-D, or `c` with no breakpoints set).

While attached, **Ctrl-] is the listener's key** and every other key goes to
the target, Ctrl-C included:

- **Ctrl-]** drops the session (roster: `dropped:`). The target detaches and
  runs on, finishing any command it was in the middle of, and the next one
  comes up. It works even when the target is stuck: the terminal turns it into
  a signal for the listener, which nothing can block.
- **A second Ctrl-] right after** (within a second), or **Ctrl-C at the idle
  roster**, quits the listener. Every container still waiting on it is released
  and runs on, and your terminal is restored.

The listener also never waits on a single container: one that connects without
a banner is hung up after 5 s (`ignored:`), and one that stops taking your
keystrokes for 5 s is dropped (`dropped (not reading input …)`).

You never connect *to* the containers — they come to you. (This is exactly
what forward debuggers like madbg can't do: there you'd chase N endpoints.)
The bundled `serve` identifies each container by its banner, keeps the
roster, and — for IPython (pty) sessions — puts your terminal in raw mode so
completion and special keys pass straight through.

## In the shell

It's the IPython debugger — `p`, `n`, `s`, `c`, `bt`, `l`, evaluate
expressions, **and Tab/dot-completion against the live frame** (`cfg.<TAB>`).
One difference from a normal debugger: **quitting detaches instead of killing
your program.**

| Command             | Effect                                             |
|---------------------|----------------------------------------------------|
| `detach` / `q` / Ctrl-D | remove breakpoints, close the socket, **let the program continue** |
| `c` (continue)      | resume; the session stays open for your breakpoints. With **no** breakpoints set it detaches, since nothing could bring the prompt back |
| Ctrl-C              | at the prompt: clear the line. During a command that runs code (`!time.sleep(60)`, `p slow()`): interrupt it, back to the prompt. While the program runs (after `n`, `c`...): stop at the next line it runs. It never raises into the program itself |
| Ctrl-]              | handled by the listener: drop this session (the target detaches and runs on); see above |

Ctrl-C reaches the target as a keystroke, not as a signal to the process, so
the program's own SIGINT handler is never involved. Outside the main thread,
Python can only interrupt between bytecodes: a pure-Python loop stops at once,
but a blocking call (`time.sleep`, a socket read) is interrupted only when it
returns. The shell says so when that happens, and Ctrl-] still drops the
session meanwhile.

This is deliberate: a stray `q` over a flaky debugging link should never raise
`BdbQuit` into production code.

## Design notes

- **Fail-open.** Can't connect within ~1s → `set_trace()` returns and the program
  is unaffected. A connection that dies mid-session never raises into the debugged
  program: writes are dropped, reads return EOF (which `pdb` treats as detach).
- **One shell at a time.** The first thread to hit a breakpoint takes the shell;
  concurrent arrivals pass straight through. The lock is held for the whole session
  and released when the socket closes.
- **No surprises in the target.** It doesn't hijack the program's Ctrl-C handler
  (`nosigint`) and doesn't read a stray `~/.pdbrc` from inside the container
  (`readrc=False`).
- **Leaves nothing behind.** A breakpoint that gets no shell builds no
  debugger. After a session, the stopped frame and its locals are freed at
  once, along with the pty, threads and sockets. The debugger object goes at
  the next garbage collection. What each
  allocation is and when it is freed: [docs/lifecycles.md](docs/lifecycles.md).
- **Leaving is immediate, from either end.** The moment you detach, the target
  hangs up (a `shutdown`, not just a `close`, so the listener sees it even while
  the program keeps running) and the listener moves on to the next breakpoint,
  or the next queued container with `--queue`. When the
  listener hangs up instead (a drop, or quitting), the target leaves its prompt
  as if you'd typed Ctrl-D and runs on; it never stays paused for a listener
  that's gone. TCP keepalive on both ends means a container that vanishes
  outright frees its place within about a minute.
- **Completion over the wire.** The IPython debugger runs on a pty so
  prompt_toolkit engages; completion is computed on the target against the
  live frame and rendered to your terminal. POSIX targets only — elsewhere
  `set_trace()` is a no-op.
- **Small surface.** IPython (which brings prompt_toolkit) is the only runtime
  dependency; everything else is stdlib.

## Security

`set_trace()` opens a debugger shell to whoever connects on the port — and that
shell can run arbitrary code in the target process. There is **no authentication**.
Only enable it on trusted networks (loopback, a private debug interface, an SSH
tunnel), never on a public port. Leaving a `reverse_ipdb.set_trace()` enabled on an
exposed port is equivalent to leaving a remote code-execution endpoint open.

For how these modules stack into layers and which guarantee each one owns, see
[docs/api-layers.md](docs/api-layers.md).

## Package layout

```
reverse_ipdb/
  __init__.py    public API re-exports (set_trace, set_trace_ipython, docker_set_trace)
  api.py         the entry points
  client.py      target side: resolve host/port, dial out, handshake, single-shell lock, cooldown
  debugger.py    ReverseIPdb (IPython TerminalPdb) + detach-on-quit mixin
  pty_bridge.py  IPython-over-pty launcher so prompt_toolkit completion works
  protocol.py    the handshake wire format, shared by target and listener
  serve.py       the listener: python -m reverse_ipdb.serve
examples/        runnable scripts for trying it locally (see examples/README.md)
tests/           pytest suite (unit + end-to-end over a real socket)
```

## Examples

See [examples/](examples/) for runnable scripts — a basic breakpoint, the
single-shell lock across threads, and attach/detach against a long-running
daemon. Start a listener (`uv run reverse_ipdb-serve`) then, e.g., `uv run python
examples/basic.py`.

## Tests

```sh
uv run pytest          # tests
uv run ruff check .    # lint
uv run mypy            # type-check
```
