# AGENTS.md — integrating & extending reverse_ipdb

Orientation for an agent (or human) picking this up. For usage prose see
[README.md](README.md); for the completion internals see
[docs/completion.md](docs/completion.md) and [docs/api-layers.md](docs/api-layers.md).

## What it is

A **reverse** debugger: the process being debugged dials **out** to a listener
you run, and you get an IPython shell (with tab/dot-completion) over the
socket. Built for code spread across many containers that you can't connect
*in* to — they all dial one listener and you walk them from a roster.

## Public API (target side)

```python
import reverse_ipdb
reverse_ipdb.set_trace()          # IPython over a pty -> completion/history/colour
reverse_ipdb.set_trace_ipython()  # explicit alias of set_trace()
reverse_ipdb.docker_set_trace()   # like set_trace, default host = host.docker.internal
```

All take keyword-only `host` / `port`, resolved **arg → env → default**
(`docker_set_trace`'s host default is `host.docker.internal`; on Linux start
the container with `--add-host=host.docker.internal:host-gateway`):

| arg    | env          | default                | meaning                         |
|--------|--------------|------------------------|---------------------------------|
| `host` | `DEBUG_HOST` | `host.docker.internal` | listener host to dial           |
| `port` | `DEBUG_PORT` | `4444`                 | listener port                   |
| `frame`| —            | caller's frame         | where to stop; **`set_trace` / `set_trace_ipython` only**, rarely passed |

Guarantees callers rely on — **do not break these**:

- **Fail-open.** No listener reachable within ~1s → the call is a no-op and the
  program runs on untouched. Never raise into the debugged program. That holds
  after dialing too: if the deferred IPython import fails (a broken venv, say),
  `pty_bridge._abort` prints the traceback to stderr *and* to the listener,
  closes the socket, releases the lock, and the program runs on.
- **One shell at a time.** A module-level lock means the first thread/process to
  reach a breakpoint gets the shell; concurrent arrivals no-op. Released when
  the session's socket/pty closes.
- **Detach, don't kill.** `q` / `quit` / `exit` / EOF / `detach` remove
  breakpoints, tear down the connection, and `continue` — they never raise
  `BdbQuit` into the program. `c` with no breakpoints set detaches too
  (nothing could bring the prompt back, so holding the listener would only
  stall the queue).
- **Hang up for real.** Teardown `shutdown()`s the socket before closing it:
  a bare `close()` while the socket->pty pump is blocked in `recv` sends no
  FIN on Linux, so a target that keeps running after detach left the
  listener (and everything queued behind it) stuck. `_close_socket` owns this;
  `tests/test_session.py` pins it.
- **A listener hang-up releases the target.** Closing fds under a blocked
  prompt never wakes it, so when the socket dies the bridge calls
  `ReverseIPdb.hang_up()`: it ends the running prompt with `EOFError` (via the
  prompt's own event loop) or, if none is up, the next one as soon as it
  starts — the debugger takes that as `detach`. Until then the pty->socket
  pump keeps reading and discarding, so the debugger's writes (a command's
  output, the stack at the next breakpoint) neither block nor fail into the
  program. Only the output pump closes the master, once the slave is closed.
- **POSIX only.** A pty is required, so the package imports `termios` at the
  top level — `import reverse_ipdb` only works on a Unix platform.

## Listener (your machine)

```sh
uv run reverse_ipdb-serve --keep          # fan-in: serve one container, detach, next…
uv run reverse_ipdb-serve --keep --no-queue # don't queue: the others are told to continue
python -m reverse_ipdb.serve --keep       # same, without uv
socat STDIO,raw,echo=0 TCP-LISTEN:4444,reuseaddr   # minimal, single session
```

`reverse_ipdb-serve` accepts every container immediately (none refused), shows a live
roster (`queued:` / `attached:` / `detached:`), and puts your terminal in raw
mode so completion works. With `--no-queue`, while one container
holds the slot (queued or attached), every other arrival is answered `continue`
(roster: `continued:`) and runs on as if no listener existed. Use a **raw**
listener — `nc` mangles completion (line-buffered + local echo).

The listener must **never become unresponsive**:

- **Ctrl-C is the listener's.** Raw mode keeps `ISIG` on (with `VQUIT` /
  `VSUSP` disabled), so Ctrl-C is a SIGINT here and never reaches the target.
  While attached it drops the session (`dropped:`); a second one within
  `QUIT_WINDOW`, or one at the idle roster, quits. Ctrl-] drops too.
- **No socket can block it.** Banners are read by one short-lived thread per
  connection, bounded by `BANNER_TIMEOUT` (silent clients: `ignored:`), and
  keystrokes are sent with `SEND_TIMEOUT` (a target not reading is dropped).
- **Nothing is left paused.** Every accepted connection is tracked until
  released; on drop, stall or exit each one is hung up (`shutdown` + `close`),
  and `accept()` is woken with `listener.shutdown()` — `close()` alone
  doesn't wake it on Linux.

## Wire protocol (keep stable across both sides)

1. Target connects and sends one banner line:
   `*** reverse_ipdb/pty <host> pid=<pid> thread=<name>\n`. The listener strips the
   `reverse_ipdb/pty` tag and shows the rest as the session's identity.
2. The listener replies **at once** (from its acceptor, not when it later
   attaches) with one line: `"<cols> <rows>\n"` — the target sets the pty
   winsize — or `"continue\n"` — the target hangs up, releases the shell lock
   and runs on without importing IPython. The listener hanging up counts as
   `continue`. No reply within `SIZE_TIMEOUT` (a plain socat listener) →
   default 80×24.
3. The stream is then a raw vt100 terminal both ways until the socket closes.
   Both ends turn on TCP keepalive (same cadence on each side), so a peer
   that vanishes without a FIN reads as a hang-up within about a minute.

## Module map

```
reverse_ipdb/
  api.py         entry points (set_trace / set_trace_ipython); POSIX guard
  client.py      dial() + shell lock + banner(); resolve_target()
  pty_bridge.py  open pty, run TerminalPdb on it, pump <-> socket, teardown
  debugger.py    ReverseIPdb (TerminalPdb) + DetachMixin
  serve.py       fan-in listener: accept-all + roster + raw pty pass-through (--no-queue)
```

Completion mechanism, in one line: IPython's `TerminalPdb.cmdloop` rebinds its
`IPCompleter` to the live frame before each prompt, and we run that prompt on a
pty so TAB reaches `prompt_toolkit`. See `docs/completion.md`.

## Dev workflow

```sh
uv sync                 # venv + deps + dev tools (pytest/ruff/mypy)
uv run ruff check .     # lint   (also auto-run in CI)
uv run mypy             # types
uv run pytest -q        # tests
```

CI (`.github/workflows/ci.yml`) runs all three on push/PR for Python 3.11–3.12.
Keep the gate green.

Ruff enforces **PLC0415** (imports at the top of the file). The one deliberate
exception is in `pty_bridge.run`, marked `# noqa: PLC0415`: IPython /
prompt_toolkit / `debugger` are imported there, **after** a listener is
reached, not at module top. This is load-bearing — importing IPython at
`import reverse_ipdb` time pulls in `concurrent.futures` and raises
`can't register atexit after shutdown` when the import lands in a finalizing /
multiprocessing interpreter, and it would also break the fail-open cheap no-op.
Don't hoist those. (`import reverse_ipdb` still requires POSIX — `pty_bridge`
imports `termios` at module top.) Ruff also enforces **TID252** (no relative
imports) — use absolute `reverse_ipdb.*` imports.

Those deferred imports run inside `pty_bridge._tolerate_shutdown_state()`. A
child **forked while its parent was tearing down** inherits
`threading._SHUTTING_DOWN = True` (`_after_fork` never resets it), after which
the first `concurrent.futures.thread` import raises
`can't register atexit after shutdown` — and we can't change how such a child
was created. While importing, that context registers threading atexits exactly
as `_register_atexit` does minus the shutdown check (the handler is still
appended and still runs at the child's exit), then restores the original.
`tests/test_shutdown_state.py` pins it. (Pure interpreter finalization is a
different, unfixable case: then threads can't be created at all.)

**`hasattr` is banned in the library** (prefer EAFP / try-except). Ruff has no
native check for it, so `tests/test_style.py` enforces it by AST-scanning
`reverse_ipdb/*.py`; add banned builtins to its `BANNED_CALLS` set.

## Regression anchors (what the tests pin)

- `tests/test_completion.py` — **echo-proof** dot-completion: an attribute name
  assembled at runtime (`zqzzmark`, absent from the source) must appear only
  *after* TAB, proving the completer evaluated the live object; and the target
  must exit 0 after detach (no leaked prompt_toolkit threads). If you touch
  `pty_bridge.py` or the protocol, this is the test that matters.
- `tests/test_serve.py` — banner parsing, "many containers all queued, none
  refused" (each answered with the size line on arrival), `--no-queue`
  (one holds the slot, the rest get `continue`, slot frees on detach), Ctrl-]
  dropping the attached session, a silent client holding up nobody, a target
  not reading input being dropped, exit releasing everyone still waiting, and
  — under a real pty — Ctrl-C dropping, a second Ctrl-C quitting, and the
  terminal restored.
- `tests/test_handshake.py` — the target's side of the reply line: size /
  `continue` / hang-up / silence, and a `continue`d target resumes without
  importing IPython and can dial again.
- `tests/test_session.py` — session lifecycle against a real target: detach
  hangs up **while the program keeps running** (the queue-stall regression),
  `c` without breakpoints detaches, and a **listener** hang-up releases the
  target — at its prompt, mid-command (output still flowing) and between
  breakpoints.
- `tests/test_api.py` — default routing to the pty bridge and aliasing.
- `tests/test_client.py` — fail-open and shell-lock acquire/release via `dial()`.

## Gotchas

- **"Completion doesn't work" is almost always a stale install or a non-raw
  listener.** New build → banner shows `reverse_ipdb/pty` and the roster prints
  `queued:`/`attached:`; old build → bannerless `reverse_ipdb/...` and `session from`.
  Fix: `git pull && uv sync`. And use `reverse_ipdb-serve` (or `socat …,raw,echo=0`).
- A completion **menu** (several candidates) stays open until dismissed; clear
  the line (Ctrl-U) before typing `detach`.
- Mid-session terminal **resize** isn't propagated — size is sent once at attach.
- There is no way to interrupt a long command on the target from the listener:
  Ctrl-C drops the session instead, and the target runs on once the command
  finishes. Clear the input line with Ctrl-U.
- IPython is a hard dependency (it brings prompt_toolkit); runtime deps = just
  IPython, everything else stdlib.
