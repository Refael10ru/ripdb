"""The catcher: a listener that fans in reverse-pdb sessions.

Built for code spread across many containers: each one dials OUT to this one
listener. Incoming containers are accepted immediately (so none is ever
refused — each stays paused at its ``set_trace``) and queued; you walk them
one at a time, with a live roster of who is waiting.

Usage::

    python -m reverse_ipdb.serve [--host HOST] [--port PORT] [--keep]
                                 [--queue | --cooldown SECONDS]

By default nobody waits: while one container holds the shell, every other
one that dials in is told to ``continue`` on the spot, runs on as if no
listener existed, and stays away for ``--cooldown`` seconds (default 1)
before it dials again. With ``--queue`` they all wait instead:
each stays paused at its ``set_trace`` and you walk them in arrival order.
Without ``--keep`` the listener serves the first container and exits; with
it, it keeps serving — after a detach, the next breakpoint (or the next one
queued) comes up.

While attached, Ctrl-] is the listener's key and every other key goes to the
target — Ctrl-C included, which interrupts what the target is running. Ctrl-]
drops the session (the target detaches and runs on) and the next one in the
queue comes up; a second Ctrl-] right after quits, releasing every container
still waiting. At the idle roster, Ctrl-C quits as usual.
"""

import argparse
import collections
import contextlib
import enum
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
from typing import NamedTuple

from reverse_ipdb import protocol
from reverse_ipdb.protocol import Field


def _note(msg):
    """Print a roster line to stderr, flushed so it shows up live."""
    print(f"*** {msg}", file=sys.stderr, flush=True)


class Identity(NamedTuple):
    """Who a container is, from its banner: shown as ``<host> pid=.. thread=..``
    with whichever parts it sent."""

    host: object = None
    pid: object = None
    thread: object = None

    @classmethod
    def from_banner(cls, msg):
        """Whatever JSON the banner carries, as strings (hashable: the digest
        counts by identity)."""
        return cls(*(None if msg.get(key) is None else str(msg[key])
                     for key in (Field.HOST, Field.PID, Field.THREAD)))

    @property
    def process(self):
        return self._replace(thread=None)

    def __str__(self):
        parts = [] if self.host is None else [str(self.host)]
        parts += [f"{key.value}={value}" for key, value in
                  ((Field.PID, self.pid), (Field.THREAD, self.thread)) if value is not None]
        return " ".join(parts)


def _read_banner(conn):
    """The target's identity from its banner line (see ``protocol``)."""
    return Identity.from_banner(protocol.decode(protocol.read_line(conn)))


def _terminal_size():
    with contextlib.suppress(OSError, ValueError):
        size = os.get_terminal_size(sys.stdin.fileno())
        if size.columns and size.lines:  # a fresh pty can report 0x0
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
        per_proc[ident.process] += n
        threads[ident.process].add(ident.thread)

    def describe(proc, n):
        names = threads[proc] - {None}
        if len(names) == 1:
            who = str(proc._replace(thread=next(iter(names))))
        elif names:
            who = f"{proc} ({len(names)} threads)"
        else:
            who = str(proc)
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


DEFAULT_COOLDOWN = 1.0  # the default mode: no queue, turned-away threads stay away 1s
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


class Ending(enum.Enum):
    """How an attached session ended, as the roster says it."""

    DETACHED = "detached"  # the target hung up (you detached), the instant it does
    DROPPED = "dropped"    # Ctrl-] (or our own stdin closed)
    STALLED = f"dropped (not reading input for {SEND_TIMEOUT:g}s)"


def _pump(conn):
    """Raw pass-through bridge: a transparent terminal between you and the target.

    Shuttles bytes both ways via select — keystrokes (TAB and Ctrl-C
    included) reach the target and its vt100 rendering comes straight back.
    Returns the ``Ending``: ``DROPPED`` when Ctrl-] arrives as a byte (stdin
    isn't a terminal), ``STALLED`` when the target stops taking input for
    ``SEND_TIMEOUT``.

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
                    return Ending.DETACHED  # reset by the target
                if not data:
                    return Ending.DETACHED
                os.write(sys.stdout.fileno(), data)
            if fd in readable:
                data = os.read(fd, 65536)
                if not data:
                    return Ending.DROPPED  # our own stdin closed: nobody to drive it
                data, drop, _ = data.partition(DROP_KEY)
                try:
                    conn.sendall(data)
                except TimeoutError:
                    return Ending.STALLED
                except OSError:
                    return Ending.DETACHED
                if drop:
                    return Ending.DROPPED
    finally:
        _restore_terminal(fd, saved)


def serve(host="0.0.0.0", port=4444, keep=False, cooldown=DEFAULT_COOLDOWN):
    """Fan in reverse-pdb sessions: accept all, serve one at a time.

    A background acceptor takes every incoming container and hands it to a
    short-lived handshake thread, which reads the banner (bounded by
    ``BANNER_TIMEOUT``, so a silent client holds up nobody) and answers at
    once. By default only one container holds the slot — attached or about to
    be — and every other one is answered ``continue`` with ``cooldown``
    (seconds): it runs on immediately and doesn't dial again for that long.
    With ``cooldown=None`` (``--queue``) every container
    is answered with our terminal size and queued (so nothing is refused and
    each stays paused); the foreground drives them one at a time in arrival
    order.

    Every container we hold is released — hung up, so it runs on — when its
    session ends, when it is dropped, and when the listener exits for any
    reason. Ctrl-] while attached drops the session; a second one within
    ``QUIT_WINDOW`` (or Ctrl-C at the idle roster) quits.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((host, port))
        sock.listen(128)  # roomy backlog so a burst of containers isn't refused
        mode = ("queue" if cooldown is None else f"no queue, {cooldown:g}s cooldown")
        mode += ", keep-alive" if keep else ", one-shot"
        _note(f"reverse_ipdb listening on {host}:{port} ({mode}) — Ctrl-C to quit; "
              "while attached, Ctrl-] drops the session (twice: quit)")
        _Listener(sock, keep, cooldown).run()


class _Listener:
    """The state ``serve`` shares between its threads: the acceptor, one
    handshake thread per arrival, and the foreground that drives sessions."""

    def __init__(self, sock, keep, cooldown):
        self.sock = sock
        self.keep = keep
        self.cooldown = cooldown   # None: queue everyone
        self.sessions = queue.Queue()
        self.stopping = threading.Event()
        self.slot = threading.Lock()   # no-queue mode: held from arrival until detach
        self.continued = collections.Counter()  # turned away while the slot was held
        self.continued_lock = threading.Lock()  # handshakes count, the foreground reports
        self.held = set()              # every container we haven't released yet
        self.held_lock = threading.Lock()
        self.cols, self.rows = _terminal_size()

    def release(self, conn):
        with self.held_lock:
            self.held.discard(conn)
        protocol.hang_up(conn)

    def accept(self):
        while not self.stopping.is_set():
            try:
                conn, addr = self.sock.accept()
            except OSError:
                return  # listener shut down -> we're exiting
            with self.held_lock:
                if self.stopping.is_set():
                    protocol.hang_up(conn)
                    return
                self.held.add(conn)
            protocol.keep_alive(conn)  # a vanished container must not hold the slot
            threading.Thread(target=self.handshake, args=(conn, addr), daemon=True).start()

    def handshake(self, conn, addr):
        try:
            conn.settimeout(BANNER_TIMEOUT)
            line = protocol.read_line(conn)
            if not line or self.stopping.is_set():  # hung up, or silent for BANNER_TIMEOUT
                if not self.stopping.is_set():
                    _note(f"ignored: {addr[0]}:{addr[1]} (no banner)")
                self.release(conn)
                return
            ident = Identity.from_banner(protocol.decode(line))
            if not any(ident):
                ident = Identity(host=f"{addr[0]}:{addr[1]}")
            conn.settimeout(None)
            if self.cooldown is not None and not self.slot.acquire(blocking=False):
                _reply(conn, {Field.CONTINUE: True, Field.COOLDOWN: self.cooldown})
                self.release(conn)
                with self.continued_lock:
                    self.continued[ident] += 1  # reported in one line on detach
                return
            _reply(conn, {Field.COLS: self.cols, Field.ROWS: self.rows})
            self.sessions.put((conn, ident))
            _note(f"queued: {ident}  ({self.sessions.qsize()} waiting)")
        except Exception:  # never let one connection take a container down with it
            self.release(conn)

    def run(self):
        accepting = threading.Thread(target=self.accept, daemon=True)
        accepting.start()
        last_drop = None
        try:
            while True:
                conn, ident = self.sessions.get()
                if conn not in self.held:
                    continue  # released meanwhile (we're shutting down)
                waiting = self.sessions.qsize()
                suffix = f"  ({waiting} still waiting)" if waiting else ""
                _note(f"attached: {ident}{suffix}")
                try:
                    ending = _pump(conn).value
                except KeyboardInterrupt:
                    now = time.monotonic()
                    if last_drop is not None and now - last_drop < QUIT_WINDOW:
                        self.release(conn)
                        _note(f"{Ending.DROPPED.value}: {ident}")
                        raise  # second Ctrl-] in a row: quit
                    last_drop = now
                    ending = Ending.DROPPED.value
                except Exception as exc:  # one bad session must not kill the listener
                    ending = f"session error ({exc!r})"
                self.release(conn)
                _note(f"{ending}: {ident}")
                if self.cooldown is not None:
                    with self.continued_lock:
                        _note_continued(self.continued)
                    self.slot.release()
                if not self.keep:
                    return
        finally:
            # Release everyone still waiting on us: queued, mid-handshake, and
            # (once accept() is woken) nobody new gets held after this.
            with self.held_lock:
                self.stopping.set()
                pending = list(self.held)
                self.held.clear()
            with contextlib.suppress(OSError):
                self.sock.shutdown(socket.SHUT_RDWR)  # wakes accept(); close() alone doesn't
            for conn in pending:
                protocol.hang_up(conn)
            accepting.join(timeout=1.0)


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="reverse_ipdb.serve",
        description="Catch reverse-connecting pdb sessions from many containers.",
    )
    parser.add_argument("--host", default="0.0.0.0",
                        help="interface to bind (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=4444,
                        help="port to listen on (default: 4444)")
    parser.add_argument("--keep", action="store_true",
                        help="after a detach, keep serving (the next breakpoint, or "
                             "the next one queued) instead of exiting")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--queue", action="store_true",
                      help="every container that dials in waits, paused at its "
                           "breakpoint, and you walk them in arrival order (default: "
                           "while one holds the shell, the others are told to continue)")
    mode.add_argument("--cooldown", type=float, metavar="SECONDS",
                      help="a container told to continue stays away this long before "
                           f"dialing again (default: {DEFAULT_COOLDOWN:g}; not with --queue)")
    args = parser.parse_args(argv)
    try:
        cooldown = DEFAULT_COOLDOWN if args.cooldown is None else args.cooldown
        serve(args.host, args.port, args.keep, None if args.queue else cooldown)
    except KeyboardInterrupt:
        print("\n*** bye", file=sys.stderr)
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
