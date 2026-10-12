# reverse_ipdb — reverse-connecting pdb

[![CI](https://github.com/Refael10ru/reverse_ipdb/actions/workflows/ci.yml/badge.svg)](https://github.com/Refael10ru/reverse_ipdb/actions/workflows/ci.yml)

Debugging a process that has no terminal — a container, a daemon, a CI worker — is
awkward: you can't attach a shell to something that isn't reading from one. `reverse_ipdb`
flips the direction. Instead of *you* attaching *in*, the target process **dials out**
to a listener you run and hands you an IPython shell (with completion) over the socket.

> Integrating or extending reverse_ipdb from another project/agent? See
> **[AGENTS.md](AGENTS.md)** for the public API, wire protocol, invariants, and
> the dev/CI workflow.

```
┌──────────────────────────┐        TCP connect() OUT        ┌──────────────────────────┐
│  TARGET PROCESS           │ ──────────────────────────────►│  YOUR MACHINE            │
│  import reverse_ipdb             │                                 │  python -m reverse_ipdb.serve   │
│  reverse_ipdb.set_trace()        │ ◄────── IPython over socket ───►│  (gives you the shell)   │
└──────────────────────────┘                                 └──────────────────────────┘
```

If nobody is listening, `set_trace()` is a **no-op** and the program runs on untouched.

## Install

This project is managed with [uv](https://docs.astral.sh/uv/):

```sh
uv sync                   # create the venv and install deps (incl. dev tools)
uv run reverse_ipdb-serve        # run the listener
uv run python -m reverse_ipdb ...# or run anything inside the environment
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
uv run reverse_ipdb-serve               # serve the first container, then exit
uv run reverse_ipdb-serve --keep        # fan-in: detach one, drop into the next waiting
uv run reverse_ipdb-serve --keep --single  # one at a time; everyone else just continues
uv run reverse_ipdb-serve --port 4444   # pick the port
# (or: python -m reverse_ipdb.serve, once the package is installed)
```

…or a plain `socat` (single session, no roster) — use raw mode so completion
and keys pass through:

```sh
socat STDIO,raw,echo=0 TCP-LISTEN:4444,reuseaddr
# nc works too but mangles completion (line-buffered, local echo)
```

### Many containers (fan-in)

This is what the reverse model is *for*. When your code runs across many
containers, point them all at the **one** listener and run it with `--keep`:

```sh
# every container (e.g. in its entrypoint / compose env)
DEBUG_HOST=<your-machine> DEBUG_PORT=4444   # reverse_ipdb.set_trace() reads these

# you, once
uv run reverse_ipdb-serve --keep
```

The listener accepts **every** container that hits a breakpoint right away —
none is ever refused — and each stays paused at its `set_trace` until it's
your turn. You get a live roster and walk them one at a time:

```
*** reverse_ipdb listening on 0.0.0.0:4444 (fan-in, keep-alive) — Ctrl-C to quit
*** queued: web-7f9c pid=12 thread=MainThread  (1 waiting)
*** queued: worker-3a1 pid=9 thread=Thread-2    (2 waiting)
*** attached: web-7f9c pid=12 thread=MainThread  (1 still waiting)
   … debug web-7f9c, then `detach` …
*** detached: web-7f9c pid=12 thread=MainThread
*** attached: worker-3a1 pid=9 thread=Thread-2
```

You never connect *to* the containers — they come to you. (This is exactly
what forward debuggers like madbg can't do: there you'd chase N endpoints.)

Don't want a queue? Add `--single`. Then only one container is yours at a
time; every other one that hits a breakpoint meanwhile is told to **continue**
on the spot and runs on untouched, exactly as if no listener were running:

```
*** reverse_ipdb listening on 0.0.0.0:4444 (single, keep-alive) — Ctrl-C to quit
*** queued: web-7f9c pid=12 thread=MainThread  (1 waiting)
*** attached: web-7f9c pid=12 thread=MainThread
*** continued: worker-3a1 pid=9 thread=Thread-2
   … `detach` frees the slot; the next container to arrive gets a shell …
```

The bundled `serve` identifies each container by its banner, keeps the fan-in
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
| `c` (continue)      | resume; the trace stays armed for later breakpoints |

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
  client.py      target side: resolve host/port, dial out, single-shell lock
  debugger.py    ReverseIPdb (IPython TerminalPdb) + detach-on-quit mixin
  pty_bridge.py  IPython-over-pty launcher so prompt_toolkit completion works
  serve.py       the fan-in listener: python -m reverse_ipdb.serve
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
