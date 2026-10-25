"""The listener for a program instead of a person: an AI agent's debugger.

``serve`` hands your terminal to one target at a time. ``AgentListener`` takes
every target that dials in, attaches each one in **line mode** (see
``protocol``) and keeps them all open at once, each a ``Session`` its caller
drives by sending one command and getting back that command's output once the
next prompt is up::

    listener = AgentListener(port=4444)
    session = listener.wait_for_session(timeout=60)   # a target hit set_trace
    print(session.wait().output)                       # where it stopped
    print(session.send("p data").output)               # {'answer': 42}
    session.detach()                                   # the program runs on

``mcp_server`` puts this behind MCP tools. Like ``serve`` it shares only the
wire with the target side. Targets that predate line mode are told to
continue (they run on) and listed in ``refused``.
"""

import collections
import contextlib
import enum
import itertools
import re
import socket
import threading
import time
from typing import NamedTuple

from reverse_ipdb import protocol
from reverse_ipdb.protocol import Field, Identity

BANNER_TIMEOUT = 5.0  # a target sends its banner at once; silence means it isn't one
SEND_TIMEOUT = 5.0    # a target that doesn't take a command this long is dropped
DETACH_TIMEOUT = 5.0  # how long a detach may take to hang up
COLS, ROWS = 200, 50  # the pty size we ask for: wide, so output wraps little
MAX_SESSIONS = 32     # open at once; later arrivals are told to continue
MAX_OUTPUT = 20_000   # characters of one reply; the middle of longer output is cut
COOLDOWN = 1.0        # a target told to continue stays away this long

PROMPT_END = protocol.PROMPT_END.encode()
LINE_PROMPT = protocol.LINE_PROMPT.encode()
_ESCAPES = re.compile(
    r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"  # OSC (the prompt marks among them)
    r"|\x1b\[[0-?]*[ -/]*[@-~]"           # CSI: colour, cursor
    r"|\x1b[@-Z\\-_]"                     # the rest
)


class Status(enum.Enum):
    """Where a session is, as its caller sees it."""

    PROMPT = "at the prompt"  # stopped: send a command
    RUNNING = "running"       # the program (or a long command) runs: wait or interrupt
    ENDED = "ended"           # detached, or the target went away


class Reply(NamedTuple):
    """What came back from a target: the output since the last prompt (the
    prompt itself left out, escape sequences stripped) and where it is now."""

    output: str
    status: Status


def clean(data):
    """Bytes from a line-mode target as plain text, the prompt it ends with
    left out."""
    data = data[:-len(LINE_PROMPT)] if data.endswith(LINE_PROMPT) else data
    text = _ESCAPES.sub("", data.decode("utf-8", "replace"))
    text = text.replace("\r\n", "\n").replace("\r", "")
    if len(text) > MAX_OUTPUT:
        cut = len(text) - MAX_OUTPUT
        half = MAX_OUTPUT // 2
        text = f"{text[:half]}\n[... {cut} characters cut ...]\n{text[-half:]}"
    return text.strip("\n")


class Session:
    """One target, stopped at its breakpoint, driven one command at a time.

    A reader thread collects everything the target writes. The target is at
    its prompt exactly when what arrived ends with ``PROMPT_END``; a command
    sent then gets back all output up to the next prompt.
    """

    def __init__(self, sid, conn, identity):
        self.id = sid
        self.identity = identity
        self.conn = conn
        self.changed = threading.Condition()  # guards everything below
        self.unread = bytearray()   # output since the last command
        self.at_prompt = False
        self.ended = False
        self.announced = False      # handed out by wait_for_session yet
        threading.Thread(target=self._read, name=f"reverse_ipdb-agent-{sid}",
                         daemon=True).start()

    @property
    def status(self):
        if self.ended:
            return Status.ENDED
        return Status.PROMPT if self.at_prompt else Status.RUNNING

    def wait(self, timeout=30.0):
        """Output so far, once the target is at its prompt again, has ended, or
        ``timeout`` seconds have passed (then it is still running)."""
        with self.changed:
            self.changed.wait_for(lambda: self.at_prompt or self.ended, timeout)
            output, self.unread = bytes(self.unread), bytearray()
            return Reply(clean(output), self.status)

    def send(self, command, timeout=30.0):
        """Run one debugger command (``p x``, ``ll``, ``n``, ``!x = 1``, ...)
        and return its output. Only at the prompt: while the program runs,
        ``wait`` for the next stop or ``interrupt`` it first."""
        command = " ".join(command.splitlines()).strip()
        with self.changed:
            if not self.at_prompt:
                return Reply(clean(bytes(self.unread)), self.status)
            self.at_prompt = False
            self.unread.clear()
        if not self._write(command.encode() + b"\n"):
            return Reply("", Status.ENDED)
        return self.wait(timeout)

    def interrupt(self, timeout=5.0):
        """Ctrl-C: stop the running program at its next line, or interrupt a
        command that is evaluating code. Returns what the target said, once
        it is at its prompt (or after ``timeout``)."""
        if not self.at_prompt and not self._write(b"\x03"):
            return Reply("", Status.ENDED)
        return self.wait(timeout)

    def detach(self):
        """Leave the target: its breakpoints go, its program runs on."""
        if self.at_prompt:
            self.send("detach", timeout=DETACH_TIMEOUT)
        else:  # mid-command or running: hanging up detaches it just the same
            self.close()
        with self.changed:
            self.changed.wait_for(lambda: self.ended, DETACH_TIMEOUT)
        self.close()
        return self.status

    def close(self):
        """Hang up: the target detaches and runs on."""
        protocol.hang_up(self.conn)

    def _write(self, data):
        try:
            self.conn.sendall(data)
            return True
        except OSError:  # gone, or not reading for SEND_TIMEOUT
            self.close()
            return False

    def _read(self):
        try:
            while True:
                try:
                    data = self.conn.recv(65536)
                except TimeoutError:
                    continue  # the timeout is there for sendall
                if not data:
                    break
                with self.changed:
                    self.unread += data
                    self.at_prompt = self.unread.endswith(PROMPT_END)
                    self.changed.notify_all()
        except OSError:
            pass
        finally:
            with self.changed:
                self.ended, self.at_prompt = True, False
                self.changed.notify_all()
            self.close()


class AgentListener:
    """Accepts every target that dials ``host:port`` and keeps a ``Session``
    for each, all open at once (up to ``MAX_SESSIONS``)."""

    def __init__(self, host="0.0.0.0", port=4444):
        self.sessions = {}            # id -> Session, ended ones included
        self.refused = collections.deque(maxlen=20)  # (Identity, why), newest last
        self.changed = threading.Condition()  # guards sessions and refused
        self._ids = (f"s{n}" for n in itertools.count(1))
        self._closed = False
        self.sock = socket.create_server((host, port), backlog=128)
        self.port = self.sock.getsockname()[1]  # the one bound, if port was 0
        threading.Thread(target=self._accept, name="reverse_ipdb-agent-accept",
                         daemon=True).start()

    def open_sessions(self):
        with self.changed:
            return [s for s in self.sessions.values() if not s.ended]

    def session(self, sid):
        with self.changed:
            return self.sessions.get(sid)

    def wait_for_session(self, timeout=60.0):
        """The oldest session not handed out yet, as soon as it is at its
        prompt (or has ended); ``None`` if none arrives within ``timeout``."""
        def ready():
            return next((s for s in self.sessions.values()
                         if not s.announced and (s.at_prompt or s.ended)), None)

        deadline = time.monotonic() + timeout
        with self.changed:
            while (found := ready()) is None:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or self._closed:
                    return None
                self.changed.wait(min(remaining, 0.1))  # a session's prompt notifies it
            found.announced = True
            return found

    def close(self):
        """Stop listening and hang up every session: their programs run on."""
        with self.changed:
            self._closed = True
            sessions = list(self.sessions.values())
            self.changed.notify_all()
        with contextlib.suppress(OSError):
            self.sock.shutdown(socket.SHUT_RDWR)  # wakes accept(); close() alone doesn't
        with contextlib.suppress(OSError):
            self.sock.close()
        for session in sessions:
            session.close()

    def _accept(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return  # closed
            protocol.keep_alive(conn)
            threading.Thread(target=self._handshake, args=(conn,), daemon=True).start()

    def _handshake(self, conn):
        try:
            conn.settimeout(BANNER_TIMEOUT)
            banner = protocol.decode(protocol.read_line(conn))
            identity = Identity.from_banner(banner)
            with self.changed:
                why = None
                if self._closed:
                    why = "the listener is closing"
                elif banner.get(Field.LINE) is not True:
                    why = "no line mode: reverse_ipdb on the target predates it"
                elif len(self.open_sessions()) >= MAX_SESSIONS:
                    why = f"{MAX_SESSIONS} sessions already open"
                if why is None:
                    conn.sendall(protocol.encode(
                        {Field.COLS: COLS, Field.ROWS: ROWS, Field.LINE: True}))
                    conn.settimeout(SEND_TIMEOUT)  # bounds sendall; recv retries
                    session = Session(next(self._ids), conn, identity)
                    self.sessions[session.id] = session
                    self._forget_ended()
                    return
                if banner:  # silent or garbage: not a target, no reply owed
                    self.refused.append((identity, why))
            with contextlib.suppress(OSError):
                conn.sendall(protocol.encode({Field.CONTINUE: True, Field.COOLDOWN: COOLDOWN}))
            protocol.hang_up(conn)
        except Exception:  # never let one connection take a target down with it
            protocol.hang_up(conn)

    def _forget_ended(self):
        """Keep ended sessions answerable (``status`` = ended) for a while, not
        forever."""
        ended = [sid for sid, s in self.sessions.items() if s.ended]
        for sid in ended[:-MAX_SESSIONS]:
            del self.sessions[sid]
