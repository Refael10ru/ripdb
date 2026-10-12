# API layers

`reverse_ipdb` is organised as **three thin layers** on the target side, stacked so
that each one depends only on the layer directly below it. A fourth component,
the listener, is the remote *peer* of the stack rather than another layer in
it — it lives on your machine, at the far end of the socket.

```
            TARGET PROCESS                                  YOUR MACHINE
 ┌───────────────────────────────────┐
 │ 1. Public API      api.py          │   what you call: reverse_ipdb.set_trace()
 │        │                           │
 │        ▼                           │
 │ 2. Session         client.py       │   decide IF a session happens:
 │        │                           │   resolve host/port, dial out,
 │        │                           │   fail-open, single-shell lock
 │        ▼                           │
 │ 3. Debugger+pty    debugger.py     │   IPython on a pty; pumps bytes
 │                    pty_bridge.py   │   to the socket; detach tears down
 └────────┼──────────────────────────┘
          │  TCP (vt100 terminal over a socket)   ┌────────────────────────┐
          └───────────────────────────────────────│  Listener   serve.py   │
                                                   │  the peer: accepts the │
                                                   │  session, raw-bridges  │
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
frame, guards non-POSIX (no `os.openpty` → no-op), and hands off to Layer 3
(`pty_bridge.run`). It holds no sockets, no configuration logic, and no
debugger behaviour of its own — so the public contract stays stable even if the
layers beneath change.

## Layer 2 — Session / connection (`client.py`)

Decides **whether a debugging session should happen at all**, before any
debugger exists.

- `resolve_target()` — host/port precedence: **argument → env var → default**.
- `dial()` — acquires the shell lock, then dials the listener with a short
  timeout and **fails open** (returns `None`, releasing the lock) when nobody
  is listening. Returns `(sock, release)` otherwise.
- `banner()` — the `*** reverse_ipdb/pty …` identity line sent first.
- The module-level **single-shell lock** — the first thread to reach a
  breakpoint wins the shell; concurrent arrivals get `None` and pass through.
  The lock is held for the whole session and released by `release()` when the
  pty bridge tears down.

This is the layer that enforces the two headline guarantees — *no listener →
no-op* and *one shell at a time* — and it does so without importing the
debugger, so the policy is testable on its own.

## Layer 3 — Debugger + pty transport (`debugger.py`, `pty_bridge.py`)

The interactive behaviour, and the byte pipe that carries it.

- `ReverseIPdb` (over IPython's `TerminalPdb`). `pty_bridge.run` launches it on
  a **pseudo-terminal** so `prompt_toolkit` engages and computes
  **tab/dot-completion** against the live frame. It passes `nosigint=True` /
  `readrc=False` so the debugger neither hijacks the program's Ctrl-C handler
  nor reads a stray `~/.pdbrc` from the container.
- The **pty is the transport**: two pump threads shuttle bytes between the pty
  master and the socket. Either EOF tears the bridge down — a teardown hook
  closes the pty, shuts down the prompt thread, closes the socket, and releases
  the shell lock. A dead connection therefore never raises into the program.
- `DetachMixin` — redefines `q` / `quit` / `exit` / EOF to **detach and
  continue** (via that teardown hook) instead of raising `BdbQuit` into the
  program.

`pty_bridge` is the only code that touches the raw socket and the pty fds.

---

## The peer — Listener (`serve.py`)

Not part of the target-side stack; it runs on **your** machine as the other
end of the socket (`python -m reverse_ipdb.serve`, or a plain `socat`/`nc`). A
background acceptor thread fans in **every** connecting container at once —
reading each one's banner, answering it with the terminal size and queueing
it so none is refused — while the foreground serves them one at a time with a
live roster (`--keep` to advance into the next instead of exiting). With
`--no-queue` the acceptor answers `continue` instead whenever another container
already holds the slot, and that container runs on untouched. Each session is
a raw-mode pass-through: it forwards bytes both ways via select (so
completion and special keys work) and returns the instant the socket closes.
One bad session is caught and never brings the listener down. It is documented
alongside the layers because it speaks the same wire protocol, but it depends
on none of the layers above and ships no shared code with them.

## Why these boundaries

Each guarantee lives in exactly one layer, so it can be reasoned about and
tested in isolation:

| Guarantee                                 | Layer that owns it   |
|--------------------------------------------|----------------------|
| Stable public call surface                 | 1 — Public API       |
| No listener → no-op; one shell at a time   | 2 — Session          |
| Quit detaches instead of killing; a broken link never crashes the program | 3 — Debugger + pty |

Dependencies only ever point downward (1→2→3), and the listener sits off to
the side, connected solely by the socket.
