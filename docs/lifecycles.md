# Resource lifecycles

Everything reverse_ipdb allocates, who owns it, and when it is released.
`tests/test_lifecycle_static.py` keeps this list honest. Every call that creates
a file descriptor, thread, loop or process-wide hook must have an entry there,
every thread must be a daemon, and every module-level state must say why it
can't grow. `tests/test_lifecycle.py` checks the same claims at run time.

## Per breakpoint that reaches no shell

No listener, a `continue` reply (the default mode turning a container away),
or a thread still cooling down. **No debugger is built and IPython is not
imported.** One socket at most, closed before `set_trace` returns. Over
hundreds of calls: no file descriptors, no threads, and less than 16 KiB of
traced memory.

## Per debugging session (target side)

| Resource | Created by | Released |
|---|---|---|
| socket to the listener | `client.dial` | `release()`: shutdown + close, once, however the session ends |
| shell lock | `client.dial` | `release()` |
| pty master | `pty_bridge.run` | the pty→socket pump, when it ends (slave closed) |
| pty slave (2 fds) | `pty_bridge.run` | the debugged thread on detach, or the pump at once when a hang-up finds the debugger idle |
| pty→socket pump thread | `pty_bridge.run` | ends on EIO once the slave is closed |
| socket→pty pump thread | `pty_bridge.run` | ends when `release()` shuts the socket down |
| prompt thread (`TerminalPdb.thread_executor`) | IPython | shut down on detach, or on an idle hang-up |
| asyncio loop per prompt | prompt_toolkit | `asyncio.run` closes it after each prompt |
| `TerminalPdb.pt_loop` (epoll + socketpair) | IPython, never used or closed by it | closed by `_close_pt_loop` on detach or idle hang-up |
| "KeyboardInterrupt pending" timer | `ReverseIPdb.interrupt` | one-shot daemon, ends after 1 s |
| trace hooks on the debugged thread's frames and `sys.settrace` | bdb | `ReverseIPdb._release` at the first trace event after the session |
| breakpoints in bdb's process-wide registry | bdb | cleared on detach and in `_release` |
| IPython 8's class-level `lru_cache`s of the frame-skip predicates, keyed on (debugger, frame) | IPython 8 (IPython 9 keeps them per instance) | emptied in `_release` |
| the stopped frame and its locals | — | freed by reference counting as soon as the program is done with them |
| the debugger object and its prompt session (about 820 KiB) | — | unreachable once the program resumes; in IPython/prompt_toolkit reference cycles, so freed at the next GC pass |

## What was found and fixed

- **The last debugger and its stopped frame stayed alive.** bdb's
  `set_continue` unhooks every frame except `botframe`, the outermost frame,
  which lives as long as the program or thread. IPython also keeps the stopped
  frame in `initial_frame`. So the last session's debugger, frame and every
  local in it stayed reachable until the next session, or forever. `_release`
  now unhooks every frame, drops all frame references, and clears the
  completer's namespaces.
- **End-of-file crossed threads with a traceback.** The traceback captured the
  prompt loop's frame and, through `f_back`, the debugged frame, so locals were
  freed only when the garbage collector ran. `_prompt` now turns end-of-file
  into the `"EOF"` command inside the prompt thread.
- **bdb's `clear_all_breaks` cleared every breakpoint in the process.** That
  included those of a newer session in another thread, while an older one was
  still releasing. Each debugger now deletes only the `Breakpoint` objects it
  created.
- **IPython's per-debugger event loop was never closed.** It held 3 fds until
  the garbage collector ran. It is now closed.
- **A hang-up while the program ran left the session alive.** If the program
  never stopped or traced again (blocked, or the thread ended), the pty, pump
  and prompt thread stayed forever. A hang-up that finds the debugger idle now
  retires them at once. It counts as idle only outside a stop and outside the
  trace function, because pdb prints `--Return--` / `--Call--` before a stop
  begins. A lock shared with both keeps a new stop from beginning on a closed
  pty.
- **Breakpoints left armed by such a hang-up stayed in bdb's registry.** They
  are now cleared.

## Flagged: bounded, but not freed

- **prompt_toolkit's filter memos: about 11 KiB per debugging session, for the
  life of the process.** Each `Filter` memoizes every filter it was combined
  with (`_and_cache` / `_or_cache`), and module-level filters such as `vi_mode`
  live forever, so each new `PromptSession` leaves a little behind. This is an
  upstream issue. It grows with the number of times a person attaches, not with
  breakpoints passed; 1,000 attaches cost about 11 MiB.
- **IPython's shell singleton, once per process.** The first session creates
  `TerminalInteractiveShell.instance()`. It brings the
  `IPythonHistorySavingThread` (daemon), a history SQLite connection (1 fd),
  its own event loop, an atexit hook, and about 20 MiB of imports. These live
  for the rest of the process and never grow.
- **A debugger stuck in a command.** A command that never returns, say
  `!while True: pass` after the listener has gone, keeps its session until it
  returns. User code can't be stopped safely from outside, and Ctrl-C from the
  listener is the tool for that while attached.
- **The debugger object waits for the GC.** IPython's prompt-text closure and
  prompt_toolkit's own objects form reference cycles, so each finished
  debugger holds about 820 KiB until the next collection. Normal programs
  collect automatically. A program that runs with `gc.disable()` keeps one
  per session until it collects.
- **A suspended generator stepped into during a session.** It keeps our trace
  function on its frame, and so the debugger object (not the stopped frame),
  until it resumes, where the hook removes itself, or is collected.
- **A blocked thread's trace hook.** After a hang-up, a thread that never runs
  Python again keeps our trace function, and with it the debugger object without
  any frames or pty, until it does run.
- **Listener: the `continued` counter.** It grows with the distinct
  thread identities turned away during one attached session, and is cleared at
  every detach.
