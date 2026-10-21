# AGENTS.md — integrating & extending reverse_ipdb

Orientation for an agent (or human) picking this up. For usage prose see
[README.md](README.md); for the completion internals see
[docs/completion.md](docs/completion.md) and [docs/api-layers.md](docs/api-layers.md); for
what is allocated and when it is freed, [docs/lifecycles.md](docs/lifecycles.md).

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
  closes the socket, releases the lock, and the program runs on. And inside a
  stop: whatever breaks under `interaction` (the prompt machinery, a command,
  IPython itself) is caught by `ReverseIPdb.interaction`, reported on stderr
  and to the listener, and turned into a detach — it never travels up through
  `trace_dispatch` into the debugged code. `tests/test_release.py` pins it.
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
  listener (and everything queued behind it) stuck. The `release()` that
  `client.dial` returns owns this;
  `tests/test_session.py` pins it.
- **A listener hang-up releases the target.** Closing fds under a blocked
  prompt never wakes it, so when the socket dies the bridge calls
  `ReverseIPdb.hang_up()`. It ends the running prompt with `EOFError` (via
  the prompt's own event loop), or the next one as soon as it starts (after
  a running command); the debugger takes that as `detach`. **No new stop
  begins after a hang-up**: `interaction` checks under the same lock. If the
  debugger is idle — not in a stop and not inside `trace_dispatch`, where pdb
  may print `--Return--` before a stop — the bridge retires the pty and the
  prompt thread at once. Otherwise the pty->socket pump keeps reading and
  discarding until the debugged thread lets go, so the debugger's writes
  neither block nor fail into the program. Only the output pump closes the
  master, once the slave is closed.
- **Ctrl-C without signals, never into the program.** The pty is nobody's
  controlling terminal, so a `\x03` from the listener raises no SIGINT; the
  socket->pty pump hands it to `ReverseIPdb.interrupt()`. At the prompt it
  passes on (prompt_toolkit clears the line). During a command that evaluates
  code — `default` (`!x`, bare statements), `p`, `pp`, marked with
  `_evaluates_code` — it raises `KeyboardInterrupt` in the debugging thread
  (real SIGINT for a main thread with Python's default handler, else an async
  exception); those commands never resume the program, so it can only land in
  them or in pdb's prompt loop, which catches it. While the program runs it
  only calls `set_step()`. Other debugger commands ignore it. Never install a
  SIGINT handler in the target, and never raise into the program's own code.
- **Nothing outlives a session.** After detach or any hang-up, the stopped
  frame and its locals are freed at once, by reference counting. The
  debugger object itself sits in IPython/prompt_toolkit reference cycles
  (about 820 KiB) and goes at the next GC pass. `ReverseIPdb._release`
  unhooks every frame on the stack (bdb leaves `botframe` hooked), drops
  frame refs and the completer's namespaces, clears only its own breakpoints
  (bdb's registry is process-wide), and closes IPython's unused `pt_loop`. End-of-file becomes `"EOF"` inside the
  prompt thread, so no traceback carries frames across threads. A hang-up that
  finds the debugger idle retires the pty and threads at once. Every allocation
  is listed with its release in `tests/test_lifecycle_static.py`; see
  `docs/lifecycles.md`.
- **POSIX only.** A pty is required, so the package imports `termios` at the
  top level — `import reverse_ipdb` only works on a Unix platform.

## Listener (your machine)

```sh
uv run reverse_ipdb-serve --keep          # one shell at a time, the others continue (default)
uv run reverse_ipdb-serve --keep --cooldown 5  # …and stay away 5s before dialing again (default 1)
uv run reverse_ipdb-serve --keep --queue  # queue: everyone waits, walk them in arrival order
python -m reverse_ipdb.serve --keep       # same, without uv
socat STDIO,raw,echo=0,escape=0x1d TCP-LISTEN:4444,reuseaddr   # minimal, one session; Ctrl-] ends it
```

`reverse_ipdb-serve` accepts every container immediately, shows a live roster
(`queued:` / `attached:` / `detached:`), and puts your terminal in raw mode so
completion works. Use a **raw** listener. `nc -l` still gets a working prompt:
commands and `detach` work, and Ctrl-C ends nc and releases the target. But nc
is line-buffered with local echo, so there is no completion, lines show
twice, and Ctrl-C can't interrupt the target.

**Default — no queue.** While one container holds the slot (about to attach,
or attached), every other arrival is answered `continue` and runs on as if no
listener existed (`serve(cooldown=1.0)`). Two things keep a hot loop harmless:

- **The listener stays silent while attached** — the terminal is in raw mode
  and belongs to the remote shell then — and prints one digest on detach
  (`continued ×N while attached: <host> pid=<pid> (<k> threads) ×n, …`).
- **The turned-away thread cools down** for `--cooldown` seconds (default 1)
  before it dials again, tracked in a thread-local on the target so other
  threads are unaffected.

**`--queue`** (`serve(cooldown=None)`): every arrival is answered with the
terminal size and queued, none refused; each stays paused at its breakpoint
until you reach it. `--queue` and `--cooldown` are mutually exclusive.

The listener must **never become unresponsive**:

- **Ctrl-] is the listener's.** Raw mode keeps `ISIG` on with `VINTR` set to
  Ctrl-] (and `VQUIT` / `VSUSP` disabled), so Ctrl-] is a SIGINT here — it
  breaks the listener out of anything — and never reaches the target. While
  attached it drops the session (`dropped:`); a second one within
  `QUIT_WINDOW` quits. Ctrl-C at the idle roster quits as usual. Every other
  key, Ctrl-C included, goes to the target.
- **No socket can block it.** Banners are read by one short-lived thread per
  connection, bounded by `BANNER_TIMEOUT` (silent clients: `ignored:`), and
  keystrokes are sent with `SEND_TIMEOUT` (a target not reading is dropped).
- **Nothing is left paused.** Every accepted connection is tracked until
  released; on drop, stall or exit each one is hung up (`shutdown` + `close`),
  and `accept()` is woken with `listener.shutdown()` — `close()` alone
  doesn't wake it on Linux.

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
   `client.REPLY_TIMEOUT` (a plain socat listener) → default size; a line that
   isn't a JSON object → same.
3. The stream is then a raw vt100 terminal both ways until the socket closes.
   Both ends turn on TCP keepalive (same cadence on each side), so a peer
   that vanishes without a FIN reads as a hang-up within about a minute.

## Module map

```
reverse_ipdb/
  api.py         layer 1: entry points (set_trace / set_trace_ipython) -> client.attach
  client.py      layer 2: attach() = dial() + handshake(); shell lock; cooldown -> pty_bridge.run
  pty_bridge.py  layer 3: run(frame, sock, release, cols, rows): deferred debugger import,
                 pty, pumps <-> socket, teardown
  debugger.py    layer 3: ReverseIPdb (TerminalPdb) + DetachMixin
  protocol.py    the wire, shared by both ends: JSON-object-per-line encode / decode / read_line
  serve.py       the peer: the listener, roster + raw pass-through (default no queue, --queue)
```

Dependencies point **down only** (api → client → pty_bridge → debugger; serve
and client share just `protocol`). `tests/test_layers.py` AST-scans every
intra-package import, deferred ones included, and fails on any other edge —
extend its `ALLOWED` table when adding a module, never by widening a layer.

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

CI (`.github/workflows/ci.yml`) runs all three on push/PR for Python 3.10–3.12.
Keep the gate green. **3.10 matters:** it is where production runs, and it
resolves **IPython 8** (IPython 9 needs 3.11+), so both IPython lines must
keep working. Known IPython 8 difference: its frame-skip predicates live in
class-level `lru_cache`s keyed on `(debugger, frame)`; `_release` empties
them, or the debugger and the stopped frame live on. Run the suite under
both before touching `debugger.py`: `uv run --python 3.10 pytest -q`.

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
- `tests/test_serve.py` — banner parsing, `--queue`: "many containers all
  queued, none refused" (each answered with the size line on arrival), the
  default no-queue mode (one holds the slot, the rest get `continue` silently,
  one digest line on detach, slot frees on detach), the mode flags
  (`--cooldown`, `--queue`, the two exclusive), Ctrl-] dropping the attached session, a
  silent client holding up nobody, a target not reading input being dropped,
  exit releasing everyone still waiting, and — under a real pty — Ctrl-C
  passed to the target, Ctrl-] dropping, a second Ctrl-] quitting with the
  terminal restored, and Ctrl-C quitting the idle roster.
- `tests/test_handshake.py` — the target's side of the reply line: size /
  `continue` / hang-up / silence, and a `continue`d target resumes without
  importing IPython and can dial again.
- `tests/test_session.py` — session lifecycle against a real target: detach
  hangs up **while the program keeps running** (the queue-stall regression),
  `c` without breakpoints detaches, and a **listener** hang-up releases the
  target — at its prompt, mid-command (output still flowing) and between
  breakpoints; and Ctrl-C interrupting a command (main and worker thread, with
  the "pending" note for a blocking call) or stopping the running program.
- `tests/test_lifecycle.py` — one process through hundreds of no-listener and
  turned-away breakpoints and ten sessions ending every way: no fds, threads,
  debuggers or prompt sessions left, the stopped frame's 1 MiB local freed
  without the GC, about 11 KiB per session left over (prompt_toolkit); and a
  hang-up while the program is blocked releases the session at once.
- `tests/test_release.py` — a hang-up that lands inside `trace_dispatch` is
  not idle (the pty stays until the debugged thread lets go), and a debugger
  clears only the breakpoints it created.
- `tests/test_lifecycle_static.py` — every resource-creating call has a
  documented release (`LIFECYCLES`), every thread is a daemon, and every
  module-level state is accounted for (`MODULE_STATE`).
- `tests/test_api.py` — hand-off to `client.attach` and aliasing.
- `tests/test_layers.py` — the import graph matches `docs/api-layers.md`.
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
- Ctrl-C in a **worker thread** is an async exception: it can't break a
  blocking C call (`time.sleep`, `recv`), only land after it returns. The shell
  prints "KeyboardInterrupt pending" when that happens; Ctrl-] is the way out.
- Ctrl-C while the program runs stops at the next line **it traces**. After
  `c` with breakpoints, frames without breakpoints may not be traced, so it can
  take until the current call returns. It never escalates into the program.
- IPython is a hard dependency (it brings prompt_toolkit); runtime deps = just
  IPython, everything else stdlib.
