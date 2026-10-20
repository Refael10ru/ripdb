# Tab / dot completion

`reverse_ipdb.set_trace()` gives you **real completion** — `cfg.<TAB>` lists
attributes, `ord<TAB>` completes `order`, history and colour all work — the
full IPython debugger, over the reverse/dial-out connection.

## How it works (and what it needs)

Completion needs a real terminal: the keystrokes (your TAB) and the namespace
being completed (the live frame) have to meet somewhere a TTY exists. reverse_ipdb
arranges that by giving the **target** a pseudo-terminal:

- `set_trace()` runs IPython's `TerminalPdb` on a **pty** inside the target,
  so `prompt_toolkit` engages and computes completions there against the live
  frame.
- Two pump threads shuttle bytes between that pty and the socket.
- The **listener** puts *your* terminal in raw mode and passes bytes straight
  through, so TAB reaches the target and its vt100 rendering comes back.

Requirements:

- **Use a raw listener.** `reverse_ipdb-serve` does this automatically. With a
  plain socket tool use `socat STDIO,raw,echo=0,escape=0x1d TCP-LISTEN:4444,reuseaddr`
  (Ctrl-] ends the session). `nc` gets you a working prompt but **no
  completion**: it sends whole lines, so `ord<TAB><Enter>` arrives at once and
  the unexpanded `ord` is what runs. Its local echo also doubles every line.
- **POSIX target.** The pty uses `pty`/`termios`, which the package imports at
  the top level, so `import reverse_ipdb` requires a Unix platform.

That's it — there's nothing to turn on.

## This works *across many containers*

Completion composes with the fan-in model. Point every container at one
`reverse_ipdb-serve --keep`, and each breakpoint that gets the shell gives you
a full completion-enabled IPython shell. By default that's one at a time,
while the others continue. With `--queue`, the containers wait, paused, and
you walk them from the roster. You never connect *to* the containers.

## Aside: madbg (a forward alternative)

[madbg](https://github.com/kmaork/madbg) also offers a full remote IPython
debugger with completion, via a pty. The difference is direction: madbg is
**forward** — the debugged process listens and you connect *in* to each one.
That's fine for a single reachable target (or one behind an SSH tunnel), but
for code spread across many containers you'd be chasing N endpoints — which is
exactly what reverse_ipdb's dial-out fan-in avoids. Use madbg for a single target you
can reach; use reverse_ipdb to debug *out* of many containers into one place.
