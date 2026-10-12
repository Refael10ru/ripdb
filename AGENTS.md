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
  `BdbQuit` into the program.
- **POSIX only.** A pty is required, so the package imports `termios` at the
  top level — `import reverse_ipdb` only works on a Unix platform.

## Listener (your machine)

```sh
uv run reverse_ipdb-serve --keep          # fan-in: serve one container, detach, next…
uv run reverse_ipdb-serve --keep --no-queue [SECONDS]  # don't queue: the others continue
python -m reverse_ipdb.serve --keep       # same, without uv
socat STDIO,raw,echo=0 TCP-LISTEN:4444,reuseaddr   # minimal, single session
```

`reverse_ipdb-serve` accepts every container immediately (none refused), shows a live
roster (`queued:` / `attached:` / `detached:`), and puts your terminal in raw
mode so completion works. With `--no-queue`, while one container
holds the slot (queued or attached), every other arrival is answered `continue`
and runs on as if no listener existed; those are reported in **one digest
line on detach** (`continued ×N while attached: …`), never mid-session — the
terminal is in raw mode and belongs to the remote shell then, so a loop
hitting its breakpoint must not spray lines over it. The reply also carries
a cooldown (`--no-queue SECONDS`, default 1): the turned-away thread doesn't
dial again for that long — tracked in a thread-local, so a hot loop across
many threads costs no round trip per pass and other threads are unaffected.
Use a **raw** listener — `nc` mangles completion (line-buffered + local echo).

## Wire protocol (keep stable across both sides)

Each handshake message is **one JSON object per line** (`protocol.py`:
`encode` / `decode` / `read_line`). **Every key is optional and unknown keys
are ignored** on both sides, so either side can add fields without breaking
the other — add keys, never repurpose or remove them.

1. Target connects and sends its banner:
   `{"reverse_ipdb": "pty", "host": "<host>", "pid": <pid>, "thread": "<name>"}`.
   The listener shows `<host> pid=<pid> thread=<name>` (whichever it got) as
   the session's identity.
2. The listener replies **at once** (from its acceptor, not when it later
   attaches) with one of:
   - `{"cols": <n>, "rows": <n>}` — attach; the target sets the pty winsize
     (80×24 for whatever is missing);
   - `{"continue": true, "cooldown": <seconds>}` — no shell: the target hangs
     up, releases the shell lock and runs on without importing IPython, and
     that thread doesn't dial again for `cooldown` seconds (0 if absent).
   The listener hanging up counts as `continue`. No reply within
   `SIZE_TIMEOUT` (a plain socat listener) → default size; a line that isn't a
   JSON object → same.
3. The stream is then a raw vt100 terminal both ways until the socket closes.

## Module map

```
reverse_ipdb/
  api.py         entry points (set_trace / set_trace_ipython); POSIX guard
  client.py      dial() + shell lock + banner() + continue cooldown; resolve_target()
  protocol.py    the JSON-object-per-line handshake: encode / decode / read_line
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
- `tests/test_protocol.py` — JSON-line encode/decode tolerance (missing and
  unknown keys, non-objects) and `read_line` not over-reading into the session.
- `tests/test_serve.py` — banner parsing, "many containers all queued, none
  refused" (each answered with the size line on arrival), and `--no-queue`
  (one holds the slot, the rest get `continue` silently, one digest line on
  detach, slot frees on detach).
- `tests/test_handshake.py` — the target's side of the reply line: size /
  `continue` / hang-up / silence, and a `continue`d target resumes without
  importing IPython and can dial again.
- `tests/test_api.py` — default routing to the pty bridge and aliasing.
- `tests/test_client.py` — fail-open, shell-lock acquire/release via `dial()`,
  and the per-thread `continue` cooldown.

## Gotchas

- **"Completion doesn't work" is almost always a stale install or a non-raw
  listener.** New build → the banner is a JSON line with `"reverse_ipdb": "pty"`
  and the roster prints `queued:`/`attached:`; old build → `*** reverse_ipdb/...`
  text banners (which a new listener shows as a bare `ip:port`) or `session from`.
  Fix: `git pull && uv sync`. And use `reverse_ipdb-serve` (or `socat …,raw,echo=0`).
- A completion **menu** (several candidates) stays open until dismissed; clear
  the line (Ctrl-U) before typing `detach`.
- Mid-session terminal **resize** isn't propagated — size is sent once at attach.
- IPython is a hard dependency (it brings prompt_toolkit); runtime deps = just
  IPython, everything else stdlib.
