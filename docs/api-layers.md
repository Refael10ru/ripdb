# API layers

`reverse_ipdb` is organised as **three thin layers** on the target side, stacked so
that each one depends only on the layer directly below it. A fourth component,
the listener, is the remote *peer* of the stack rather than another layer in
it — it lives on your machine, at the far end of the socket. The one piece of
code both ends share is `protocol.py`, the wire format of the handshake.

```
            TARGET PROCESS                                  YOUR MACHINE
 ┌───────────────────────────────────┐
 │ 1. Public API      api.py          │   what you call: reverse_ipdb.set_trace()
 │        │                           │
 │        ▼                           │
 │ 2. Session         client.py       │   decide IF a session happens:
 │        │                           │   resolve host/port, dial out,
 │        │                           │   fail-open, single-shell lock,
 │        │                           │   per-thread cooldown after "continue"
 │        ▼                           │
 │ 3. Debugger+pty    pty_bridge.py   │   handshake, then IPython on a pty;
 │                    debugger.py     │   pumps bytes to the socket; detach
 └────────┼──────────────────────────┘   tears down
          │  TCP: one JSON line each way,          ┌────────────────────────┐
          │  then a raw vt100 terminal            │  Listener   serve.py   │
          └───────────────────────────────────────│  the peer: answers the │
                     protocol.py (both ends)       │  banner, queues or     │
                                                   │  continues, raw-bridges│
                                                   │  your terminal to it   │
                                                   └────────────────────────┘
```

A breakpoint travels **down** the stack (1 → 3), crosses the socket, and the
listener drives it from the other end.

---

## Layer 1 — Public API (`api.py`, re-exported from `__init__.py`)

The only surface callers touch.

- `set_trace(*, host=None, port=None, frame=None)` — the IPython debugger
- `set_trace_ipython` — explicit alias of `set_trace`
- `docker_set_trace(*, …)` — `set_trace` with the default host set to
  `host.docker.internal` (arg → `DEBUG_HOST` → that)

**Responsibility:** be the entry point and nothing more. It grabs the caller's
frame and hands off to Layer 2 (`client.attach`). It holds no sockets, no
configuration logic, and no debugger behaviour of its own — so the public
contract stays stable even if the layers beneath change.

## Layer 2 — Session / connection (`client.py`)

Decides **whether a debugging session should happen at all**, before any
debugger exists.

- `attach(frame, host, port)` — the layer's entry: `dial`, then `handshake`,
  and only if the listener wants us, hand the live socket **down** to Layer 3
  (`pty_bridge.run`). Every "no" on the way is a silent no-op.
- `resolve_target()` — host/port precedence: **argument → env var → default**.
- `dial()` — acquires the shell lock, then dials the listener with a short
  timeout and **fails open** (returns `None`, releasing the lock) when nobody
  is listening. Returns `(sock, release)` otherwise; `release` closes the
  socket and frees the lock. It also returns `None` without connecting while
  this thread is cooling down (below).
- `banner()` / `handshake()` — send the identity line (a JSON object tagged
  `"reverse_ipdb": "pty"` with `host` / `pid` / `thread`) and read the reply:
  a terminal size → proceed; `continue` (with an optional cooldown) or the
  listener hanging up → no session, before any debugger is imported.
- `cool_down(seconds)` — called when the listener answered `continue` with a
  cooldown: this thread won't dial again for that long. The state is a
  **thread-local**, so no lock is involved and other threads are unaffected;
  a hot loop across many threads costs no round trip per pass.
- The module-level **single-shell lock** — the first thread to reach a
  breakpoint wins the shell; concurrent arrivals get `None` and pass through.
  The lock is held for the whole session and released by `release()` when the
  pty bridge tears down.

This is the layer that enforces the headline guarantees — *no listener →
no-op*, *one shell at a time*, *told to continue → stay away for a while* —
and it does so without importing the debugger, so the policy is testable on
its own.

## Layer 3 — Debugger + pty transport (`debugger.py`, `pty_bridge.py`)

The interactive behaviour, and the byte pipe that carries it.

- `pty_bridge.run(frame, sock, release, cols, rows)` takes a **live socket**
  from Layer 2 — it never dials or handshakes itself. Its first act is the
  deferred import of IPython / prompt_toolkit / the debugger (deferred so
  `import reverse_ipdb` stays light and so a `continue`d breakpoint never pays
  for it); if that import fails (a broken venv), `_abort` writes the traceback
  to stderr *and* to the listener, hangs up, and the program runs on.
- `ReverseIPdb` (over IPython's `TerminalPdb`). `pty_bridge.run` launches it on
  a **pseudo-terminal** so `prompt_toolkit` engages and computes
  **tab/dot-completion** against the live frame. It passes `nosigint=True` /
  `readrc=False` so the debugger neither hijacks the program's Ctrl-C handler
  nor reads a stray `~/.pdbrc` from the container.
- The **pty is the transport**: two pump threads shuttle bytes between the pty
  master and the socket. Either side ending tears the bridge down. A detach
  closes the pty slave (the output pump drains the parting message, then
  closes the master), then calls Layer 2's `release`, which **shuts down** and
  closes the socket and frees the shell lock. The `shutdown` matters: a bare `close()` while the other pump is
  blocked in `recv` sends no FIN, and the listener would never see the detach.
- A **listener hang-up** calls `ReverseIPdb.hang_up()`, which ends the prompt
  (now, or the next one to start) as if Ctrl-D were pressed, so the target
  detaches and runs on instead of staying paused. Until it does, the output
  pump discards whatever the debugger writes, so a dead connection never
  blocks or raises into the program.
- `ReverseIPdb.interrupt()` — the socket->pty pump hands it each Ctrl-C byte
  (no SIGINT exists for a pty that isn't the controlling terminal). During a
  command that evaluates code it raises `KeyboardInterrupt` in that command;
  while the program runs it stops at the next line; at the prompt the byte
  passes on and clears the line. It never raises into the program's own code.
- `DetachMixin` — redefines `q` / `quit` / `exit` / EOF (and `c` when no
  breakpoint is set) to **detach and
  continue** (via that teardown hook) instead of raising `BdbQuit` into the
  program.

`pty_bridge` is the only code that touches the raw socket and the pty fds.

---

## The wire — `protocol.py` (both ends)

The only code shared by the target and the listener. The handshake is **one
JSON object per line in each direction**, and every key is optional: each side
uses defaults for what's missing and ignores what it doesn't know, so either
side can add fields without breaking the other (add keys, never repurpose or
remove them).

- `encode(fields)` / `decode(line)` — a dict to a line and back; anything that
  isn't a JSON object (garbage, silence, a hang-up) decodes to `{}`.
- `read_line(sock)` — reads byte-at-a-time so nothing past the line is
  consumed: the bytes after it belong to the vt100 session.

```
target   -> {"reverse_ipdb": "pty", "host": .., "pid": .., "thread": ..}
listener -> {"cols": .., "rows": ..}                attach (80x24 for what's missing)
listener -> {"continue": true, "cooldown": ..}      no shell; stay away that long
```

After the reply the socket is a raw vt100 terminal until it closes.

## The peer — Listener (`serve.py`)

Not part of the target-side stack; it runs on **your** machine as the other
end of the socket (`python -m reverse_ipdb.serve`, or a plain `socat`/`nc`). A
background acceptor takes **every** connecting container at once, handing
each to a short-lived handshake thread that reads its banner (with a timeout,
so a silent client holds up nobody) and answers it *at once*, while the
foreground serves sessions one at a time with a live roster (`--keep` to
advance into the next instead of exiting). By default the handshake answers
`continue` plus a cooldown (`--cooldown`, default 1s) whenever another
container already holds the slot, and that container runs on untouched; the
listener stays silent while a session is attached (the terminal belongs to
the remote shell then) and reports the turned-away containers in one digest
line on detach, grouped by process with a thread count. With `--queue` it
answers every container with the terminal size instead and queues it, so none
is refused and each waits its turn. Each session is a raw-mode pass-through: it
forwards bytes both ways via select (so completion and special keys work) and
returns the instant the socket closes. Ctrl-] is the one key kept for the
listener, as its terminal interrupt key: it drops the session, and a second
one right after quits. Ctrl-C goes to the target like any other key. A target
that stops reading input is dropped after a timeout. Every container held is
released (hung up, so it runs on) when its session ends and when the listener
exits. One bad session is caught and never brings the listener down. It
depends on none of the layers above; `protocol.py` is the only code it shares
with them.

## Why these boundaries

Each guarantee lives in exactly one layer, so it can be reasoned about and
tested in isolation:

| Guarantee                                 | Layer that owns it   |
|--------------------------------------------|----------------------|
| Stable public call surface                 | 1 — Public API       |
| No listener → no-op; one shell at a time; told to continue → cool down | 2 — Session |
| Quit detaches instead of killing; a broken link or a broken debugger import never crashes the program | 3 — Debugger + pty |
| Either end can add handshake fields without breaking the other | the wire — `protocol.py` |

Dependencies only ever point downward (1→2→3), and the listener sits off to
the side, connected solely by the socket and the shared wire format.
`tests/test_layers.py` scans every intra-package import (deferred ones
included) and fails the gate on any upward or skipped edge.
