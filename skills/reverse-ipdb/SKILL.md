---
name: reverse-ipdb
description: Debug a live Python program from inside its breakpoint with the reverse-ipdb MCP tools. Use when a program (often in a container or worker) calls reverse_ipdb.set_trace() / docker_set_trace(), when the user asks you to inspect a running program's state, step through code that misbehaves only in its real environment, or attach to a reverse_ipdb breakpoint.
---

# Debugging with reverse_ipdb

A program pauses at `reverse_ipdb.set_trace()` and dials out to the
`reverse-ipdb` MCP server. You then drive IPython's debugger (ipdb) inside
that real process: its real data, config and environment. Each command is
one line, exactly as typed at an `ipdb>` prompt.

## Getting a session

1. Make sure a breakpoint will be hit. If the code has none, add
   `import reverse_ipdb; reverse_ipdb.set_trace()` where you want to stop
   (`docker_set_trace()` inside a Docker container). Tell the user you added
   it, and remove it when done.
2. The program dials `DEBUG_HOST:DEBUG_PORT` (default
   `host.docker.internal:4444`). For a program on this machine, run it with
   `DEBUG_HOST=127.0.0.1`. The MCP server's port is in its replies
   ("listening on …").
3. Call `wait_for_session` before or after starting the program (start it in
   the background, or ask the user to trigger it). It returns the session id
   (`s1`, …) and where the program stopped.

## Investigating

Read before you change anything:

- `p expr`, `pp expr` — values. Narrow big objects: `p obj.attr`,
  `pp list(d.items())[:20]`, `p type(x), len(x)`.
- `ll` — source of the current function, `w` — the stack, `u` / `d` — move
  between frames, `a` — the current function's arguments.
- `p dir(obj)` — attributes (there is no tab completion).
- `!stmt` — run a statement, e.g. `!import json; print(json.dumps(cfg, indent=2))`.

Moving:

- `n` (next line), `s` (step into), `r` (to return), `unt N` (until line N),
  `b file.py:N` / `b func` then `c` (continue to it).
- After these the reply may say **running**: the program hasn't stopped yet.
  Call `wait_for_prompt` to keep waiting, or `interrupt` to stop it at the next
  line it runs. While running, `send_command` sends nothing.
- `c` with no breakpoint set ends the session (the program runs on).

## Finishing

**Always `detach` when you are done**, and on any error. A session you leave
open keeps the program paused. `q`, `quit` and `exit` also just detach; they
never kill the program. Report what you found to the user, and remove any
breakpoint you added to their code.

## Rules

- One line per command. Chain debugger commands with `;;` (`p a;;p b`);
  join Python statements with `;` (`!a = 1; b = 2`).
- `interact` and `debug` are refused (they need a terminal).
- This is the user's real process, maybe production: never run anything that
  writes, deletes, sends or charges unless the user asked for exactly that.
  Assigning to a variable (`!x = 1`) changes what the program does next.
- Output over 20,000 characters is cut in the middle; ask for less.
- If `list_sessions` shows a target as refused for "no line mode", its
  reverse_ipdb predates agent support: ask the user to update it there.
