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

While attached, Ctrl-] is the listener's key and every other key goes to the
target — Ctrl-C included, which interrupts what the target is running. Ctrl-]
drops the session (the target detaches and runs on) and the next one in the
queue comes up; a second Ctrl-] right after quits, releasing every container
still waiting. At the idle roster, Ctrl-C quits as usual.
"""

import argparse
import collections
import contextlib
import os
import queue
import select
import signal
import socket
import sys
import termios
import threading
import time
import tty

from reverse_ipdb import protocol


def _note(msg):
    """Print a roster line to stderr, flushed so it shows up live."""
    print(f"*** {msg}", file=sys.stderr, flush=True)


def _identity(msg):
    """``<host> pid=.. thread=..`` from a decoded banner, built from whichever
    of those keys it sent (``""`` if none)."""
    parts = [str(msg["host"])] if "host" in msg else []
    parts += [f"{key}={msg[key]}" for key in ("pid", "thread") if key in msg]
    return " ".join(parts)


def _read_banner(conn):
    """The target's identity from its banner line (see ``protocol``)."""
    return _identity(protocol.decode(protocol.read_line(conn)))


def _terminal_size():
    with contextlib.suppress(OSError, ValueError):
        size = os.get_terminal_size(sys.stdin.fileno())
        if size.columns and size.lines:  # a fresh pty can report 0x0
            return size.columns, size.lines
    return 80, 24


def _enable_keepalive(conn):
    """Probe idle connections, so a container that vanished without a FIN
    (killed, network gone) reads as a hang-up within about a minute and
    stops holding the attached slot. Same cadence as the target side; kept
    separate because the listener shares no code with the target stack."""
    with contextlib.suppress(OSError):
        conn.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
    for name, value in (("TCP_KEEPIDLE", 30), ("TCP_KEEPINTVL", 10), ("TCP_KEEPCNT", 3)):
        try:
            option = getattr(socket, name)
        except AttributeError:
            continue  # not exposed on this platform: the OS default cadence then
        with contextlib.suppress(OSError):
            conn.setsockopt(socket.IPPROTO_TCP, option, value)


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


def _hangup(conn):
    """Release a container: it reads EOF and runs on.

    ``shutdown`` before ``close`` so the FIN goes out even if another thread
    still holds the socket (a bare ``close`` then sends nothing on Linux, and
    the container would stay paused at its breakpoint).
    """
    with contextlib.suppress(OSError):
        conn.shutdown(socket.SHUT_RDWR)
    with contextlib.suppress(OSError):
        conn.close()


BANNER_TIMEOUT = 5.0  # a container sends its banner at once; silence means it isn't one
SEND_TIMEOUT = 5.0    # a target that doesn't take our keystrokes this long is dropped
QUIT_WINDOW = 1.0     # a second Ctrl-] this soon after a drop quits the listener
DROP_KEY = b"\x1d"    # Ctrl-], telnet's escape: the listener's own key while attached


def _disabled_char(fd):
    """The value that switches a terminal control character off."""
    try:
        value = os.fpathconf(fd, "PC_VDISABLE")
    except (OSError, ValueError):
        value = 0
    return bytes([value & 0xFF])


def _raw_with_signals(fd):
    r"""Raw mode for the pass-through, with Ctrl-] as the one signal key.

    Every key goes to the target unchanged (TAB, arrows, Ctrl-C, Ctrl-\, ...)
    except Ctrl-], which the terminal turns into a SIGINT for the listener:
    ``KeyboardInterrupt`` here, which also breaks it out of any blocking call,
    so the listener can never be wedged. Ctrl-\ and Ctrl-Z are switched off
    as signals, so they can't kill or suspend it.
    """
    tty.setraw(fd)
    attrs = termios.tcgetattr(fd)
    attrs[3] |= termios.ISIG
    attrs[6][termios.VINTR] = DROP_KEY
    off = _disabled_char(fd)
    for name in ("VQUIT", "VSUSP", "VDSUSP"):
        try:
            attrs[6][getattr(termios, name)] = off
        except AttributeError:
            continue  # VDSUSP is BSD/macOS only
    termios.tcsetattr(fd, termios.TCSANOW, attrs)


def _restore_terminal(fd, saved):
    """Put the terminal back, with SIGINT held off so a Ctrl-] landing right
    now can't leave it in raw mode."""
    if saved is None:
        return
    old_mask = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGINT})
    try:
        with contextlib.suppress(OSError, termios.error):
            termios.tcsetattr(fd, termios.TCSADRAIN, saved)
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, old_mask)


def _pump(conn):
    """Raw pass-through bridge: a transparent terminal between you and the target.

    Shuttles bytes both ways via select — keystrokes (TAB and Ctrl-C
    included) reach the target and its vt100 rendering comes straight back.
    Returns how the session ended:

    - ``"detached"`` — the target hung up (you detached), the instant it does;
    - ``"dropped"``  — Ctrl-] arrived as a byte (stdin isn't a terminal);
    - ``"stalled"``  — the target stopped taking input for ``SEND_TIMEOUT``.

    On a terminal Ctrl-] never arrives as a byte: it raises
    ``KeyboardInterrupt`` here, and the caller drops the session. Whatever was
    typed before the attach (into the roster, not the session) is discarded.
    The terminal size was already sent when the container was queued.
    """
    fd = sys.stdin.fileno()
    saved = None
    with contextlib.suppress(OSError, termios.error):
        saved = termios.tcgetattr(fd)
        _raw_with_signals(fd)
        termios.tcflush(fd, termios.TCIFLUSH)
    conn.settimeout(SEND_TIMEOUT)  # bounds sendall; recv only runs once readable
    try:
        while True:
            readable, _, _ = select.select([fd, conn], [], [])
            if conn in readable:
                try:
                    data = conn.recv(65536)
                except OSError:
                    return "detached"  # reset by the target
                if not data:
                    return "detached"
                os.write(sys.stdout.fileno(), data)
            if fd in readable:
                data = os.read(fd, 65536)
                if not data:
                    return "dropped"  # our own stdin closed: nobody to drive it
                data, drop, _ = data.partition(DROP_KEY)
                try:
                    conn.sendall(data)
                except TimeoutError:
                    return "stalled"
                except OSError:
                    return "detached"
                if drop:
                    return "dropped"
    finally:
        _restore_terminal(fd, saved)


def serve(host="0.0.0.0", port=4444, keep=False, no_queue=None):
    """Fan in reverse-pdb sessions: accept all, serve one at a time.

    A background acceptor takes every incoming container and hands it to a
    short-lived handshake thread, which reads the banner (bounded by
    ``BANNER_TIMEOUT``, so a silent client holds up nobody) and answers at
    once. By default the answer is our terminal size and the container is
    queued (so nothing is refused and each stays paused); the foreground
    drives them one at a time in arrival order. With ``no_queue`` (a cooldown
    in seconds), only one container holds the slot — attached or queued — and
    every other one is answered ``continue`` with a ``cooldown``: it runs on
    immediately and doesn't dial again for that long.

    Every container we hold is released — hung up, so it runs on — when its
    session ends, when it is dropped, and when the listener exits for any
    reason. Ctrl-] while attached drops the session; a second one within
    ``QUIT_WINDOW`` (or Ctrl-C at the idle roster) quits.
    """
    sessions = queue.Queue()
    stopping = threading.Event()
    slot = threading.Lock()   # --no-queue: held from arrival until detach
    continued = collections.Counter()  # --no-queue: turned away while the slot was held
    continued_lock = threading.Lock()  # handshake threads count, the foreground reports
    held = set()              # every container we haven't released yet
    held_lock = threading.Lock()
    cols, rows = _terminal_size()

    def release(conn):
        with held_lock:
            held.discard(conn)
        _hangup(conn)

    def handshake(conn, addr):
        try:
            conn.settimeout(BANNER_TIMEOUT)
            line = protocol.read_line(conn)
            if not line or stopping.is_set():  # hung up, or silent for BANNER_TIMEOUT
                if not stopping.is_set():
                    _note(f"ignored: {addr[0]}:{addr[1]} (no banner)")
                release(conn)
                return
            ident = _identity(protocol.decode(line)) or f"{addr[0]}:{addr[1]}"
            conn.settimeout(None)
            if no_queue is not None and not slot.acquire(blocking=False):
                _reply(conn, {"continue": True, "cooldown": no_queue})
                release(conn)
                with continued_lock:
                    continued[ident] += 1  # reported in one line on detach
                return
            _reply(conn, {"cols": cols, "rows": rows})
            sessions.put((conn, ident))
            _note(f"queued: {ident}  ({sessions.qsize()} waiting)")
        except Exception:  # never let one connection take a container down with it
            release(conn)

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind((host, port))
        listener.listen(128)  # roomy backlog so a burst of containers isn't refused
        mode = ("fan-in" if no_queue is None else f"no queue, {no_queue:g}s cooldown")
        mode += ", keep-alive" if keep else ", one-shot"
        _note(f"reverse_ipdb listening on {host}:{port} ({mode}) — Ctrl-C to quit; "
              "while attached, Ctrl-] drops the session (twice: quit)")

        def acceptor():
            while not stopping.is_set():
                try:
                    conn, addr = listener.accept()
                except OSError:
                    return  # listener shut down -> we're exiting
                with held_lock:
                    if stopping.is_set():
                        _hangup(conn)
                        return
                    held.add(conn)
                _enable_keepalive(conn)  # a vanished container must not hold the slot
                threading.Thread(target=handshake, args=(conn, addr), daemon=True).start()

        accepting = threading.Thread(target=acceptor, daemon=True)
        accepting.start()

        last_drop = None
        try:
            while True:
                conn, ident = sessions.get()
                if conn not in held:
                    continue  # released meanwhile (we're shutting down)
                waiting = sessions.qsize()
                suffix = f"  ({waiting} still waiting)" if waiting else ""
                _note(f"attached: {ident}{suffix}")
                try:
                    outcome = _pump(conn)
                except KeyboardInterrupt:
                    now = time.monotonic()
                    if last_drop is not None and now - last_drop < QUIT_WINDOW:
                        release(conn)
                        _note(f"dropped: {ident}")
                        raise  # second Ctrl-] in a row: quit
                    last_drop = now
                    outcome = "dropped"
                except Exception as exc:  # one bad session must not kill the listener
                    outcome = f"session error ({exc!r})"
                release(conn)
                if outcome == "stalled":
                    outcome = f"dropped (not reading input for {SEND_TIMEOUT:g}s)"
                _note(f"{outcome}: {ident}")
                if no_queue is not None:
                    with continued_lock:
                        _note_continued(continued)
                    slot.release()
                if not keep:
                    return
        finally:
            # Release everyone still waiting on us: queued, mid-handshake, and
            # (once accept() is woken) nobody new gets held after this.
            with held_lock:
                stopping.set()
                pending = list(held)
                held.clear()
            with contextlib.suppress(OSError):
                listener.shutdown(socket.SHUT_RDWR)  # wakes accept(); close() alone doesn't
            for conn in pending:
                _hangup(conn)
            accepting.join(timeout=1.0)


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
