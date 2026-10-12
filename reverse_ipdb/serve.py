"""The catcher: a listener that fans in reverse-pdb sessions.

Built for code spread across many containers: each one dials OUT to this one
listener. Incoming containers are accepted immediately (so none is ever
refused — each stays paused at its ``set_trace``) and queued; you walk them
one at a time, with a live roster of who is waiting.

Usage::

    python -m reverse_ipdb.serve [--host HOST] [--port PORT] [--keep]

Without ``--keep`` the listener serves the first container and exits. With
``--keep`` it keeps serving: detach from one and you drop straight into the
next one waiting. With ``--no-queue [SECONDS]`` nobody waits: while one
container is attached, every other one that dials in is told to ``continue``
on the spot, runs on as if no listener existed, and stays away for SECONDS
(default 1) before it dials again.
"""

import argparse
import collections
import contextlib
import os
import queue
import select
import socket
import sys
import termios
import threading
import tty

from reverse_ipdb import protocol


def _note(msg):
    """Print a roster line to stderr, flushed so it shows up live."""
    print(f"*** {msg}", file=sys.stderr, flush=True)


def _read_banner(conn):
    """The target's identity from its banner: ``<host> pid=.. thread=..``,
    built from whichever of those keys it sent (``""`` if none).
    """
    msg = protocol.decode(protocol.read_line(conn))
    parts = [str(msg["host"])] if "host" in msg else []
    parts += [f"{key}={msg[key]}" for key in ("pid", "thread") if key in msg]
    return " ".join(parts)


def _terminal_size():
    with contextlib.suppress(OSError, ValueError):
        size = os.get_terminal_size(sys.stdin.fileno())
        return size.columns, size.lines
    return 80, 24


def _note_continued(counts):
    """One digest line for the containers turned away during a session.

    Printed on detach, never mid-session: while attached, the terminal is in
    raw mode and belongs to the remote shell, and a loop hitting its
    breakpoint would otherwise spray a line per iteration over it. Grouped by
    process, since many threads passing the same breakpoint would otherwise
    each be their own entry.
    """
    if not counts:
        return
    per_proc = collections.Counter()
    threads = collections.defaultdict(set)
    for ident, n in counts.items():
        proc, _, thread = ident.partition(" thread=")
        per_proc[proc] += n
        threads[proc].add(thread)

    def describe(proc, n):
        names = threads[proc] - {""}
        who = proc
        if len(names) == 1:
            who += f" thread={names.pop()}"
        elif names:
            who += f" ({len(names)} threads)"
        return f"{who} ×{n}" if n > 1 else who

    top = per_proc.most_common(3)
    who = ", ".join(describe(proc, n) for proc, n in top)
    if len(per_proc) > len(top):
        who += f", +{len(per_proc) - len(top)} more"
    _note(f"continued ×{sum(counts.values())} while attached: {who}")
    counts.clear()


def _reply(conn, fields):
    """Answer a banner with one protocol line (see ``protocol``)."""
    with contextlib.suppress(OSError):
        conn.sendall(protocol.encode(fields))


def _pump(conn):
    """Raw pass-through bridge: a transparent terminal between you and the target.

    Puts the local tty in raw mode and shuttles bytes both ways via select —
    so keystrokes (TAB included) reach the target's prompt_toolkit and its
    vt100 rendering comes straight back. Returns the instant the socket closes
    (clean detach, no extra keypress). The terminal size was already sent by
    the acceptor when the container was queued.
    """
    fd = sys.stdin.fileno()
    saved = None
    with contextlib.suppress(OSError, termios.error):
        saved = termios.tcgetattr(fd)
        tty.setraw(fd)
    try:
        while True:
            readable, _, _ = select.select([fd, conn], [], [])
            if conn in readable:
                data = conn.recv(65536)
                if not data:
                    break  # remote detached/closed
                os.write(sys.stdout.fileno(), data)
            if fd in readable:
                data = os.read(fd, 65536)
                if not data:
                    break
                with contextlib.suppress(OSError):
                    conn.sendall(data)
    finally:
        if saved is not None:
            with contextlib.suppress(OSError, termios.error):
                termios.tcsetattr(fd, termios.TCSADRAIN, saved)


def serve(host="0.0.0.0", port=4444, keep=False, no_queue=None):
    """Fan in reverse-pdb sessions: accept all, serve one at a time.

    A background thread accepts every incoming container, reads its banner and
    answers it at once. By default the answer is our terminal size and the
    container is queued (so nothing is refused and each stays paused); the
    foreground pops them in arrival order and drives one at a time. With
    ``no_queue`` (a cooldown in seconds), only one container holds the slot —
    attached or queued — and every other one is answered ``continue`` with a
    ``cooldown``: it runs on immediately and doesn't dial again for that long.
    """
    sessions = queue.Queue()
    stopping = threading.Event()
    slot = threading.Lock()   # --no-queue: held from arrival until detach
    continued = collections.Counter()  # --no-queue: turned away while the slot was held
    cols, rows = _terminal_size()

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind((host, port))
        listener.listen(128)  # roomy backlog so a burst of containers isn't refused
        mode = ("fan-in" if no_queue is None else f"no queue, {no_queue:g}s cooldown")
        mode += ", keep-alive" if keep else ", one-shot"
        _note(f"reverse_ipdb listening on {host}:{port} ({mode}) — Ctrl-C to quit")

        def acceptor():
            while not stopping.is_set():
                try:
                    conn, addr = listener.accept()
                except OSError:
                    return  # listener closed -> we're shutting down
                ident = _read_banner(conn) or f"{addr[0]}:{addr[1]}"
                if stopping.is_set():
                    conn.close()
                    return
                if no_queue is not None and not slot.acquire(blocking=False):
                    _reply(conn, {"continue": True, "cooldown": no_queue})
                    conn.close()
                    continued[ident] += 1  # reported in one line on detach
                    continue
                _reply(conn, {"cols": cols, "rows": rows})
                sessions.put((conn, ident))
                _note(f"queued: {ident}  ({sessions.qsize()} waiting)")

        threading.Thread(target=acceptor, daemon=True).start()

        try:
            while True:
                conn, ident = sessions.get()
                waiting = sessions.qsize()
                suffix = f"  ({waiting} still waiting)" if waiting else ""
                _note(f"attached: {ident}{suffix}")
                try:
                    with conn:
                        _pump(conn)
                except Exception as exc:  # one bad session must not kill the listener
                    _note(f"session error ({ident}): {exc!r}")
                else:
                    _note(f"detached: {ident}")
                if no_queue is not None:
                    _note_continued(continued)
                    slot.release()
                if not keep:
                    return
        finally:
            stopping.set()
            with contextlib.suppress(OSError):
                listener.close()  # unblock the acceptor's accept()
            while True:
                try:
                    pending = sessions.get_nowait()[0]
                except queue.Empty:
                    break
                with contextlib.suppress(OSError):
                    pending.close()


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="reverse_ipdb.serve",
        description="Fan in reverse-connecting pdb sessions from many containers.",
    )
    parser.add_argument("--host", default="0.0.0.0",
                        help="interface to bind (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=4444,
                        help="port to listen on (default: 4444)")
    parser.add_argument("--keep", action="store_true",
                        help="after a detach, drop into the next waiting container "
                             "instead of exiting")
    parser.add_argument("--no-queue", nargs="?", const=1.0, type=float, metavar="SECONDS",
                        help="don't queue: while a container is attached, any other "
                             "that dials in is told to continue, and to stay away for "
                             "SECONDS (default 1) before dialing again")
    args = parser.parse_args(argv)
    try:
        serve(args.host, args.port, args.keep, args.no_queue)
    except KeyboardInterrupt:
        print("\n*** bye", file=sys.stderr)
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
