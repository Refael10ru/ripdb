"""Target-side pseudo-terminal bridge for the IPython debugger.

prompt_toolkit (which gives IPython its tab/dot-completion, history and
colour) needs a real TTY. Our socket isn't one, so we give the debugger a
**pty**: the debugger's prompt_toolkit talks to the pty *slave* (a genuine
terminal, so termios/raw-mode work and completion is computed here against
the live frame), while two pump threads shuttle bytes between the pty
*master* and the socket. The listener on the far end runs in raw mode and is
a transparent pass-through.

POSIX only (uses ``pty``/``termios``). The plain-pdb path in ``api`` stays
pure-stdlib and cross-platform.
"""

import contextlib
import fcntl
import functools
import os
import socket
import struct
import sys
import termios
import threading
import traceback

from reverse_ipdb import client

DEFAULT_COLS, DEFAULT_ROWS = 80, 24
SIZE_TIMEOUT = 0.5  # how long to wait for the listener's terminal size
DRAIN_TIMEOUT = 1.0  # on close, how long the last output may take to reach the socket


@contextlib.contextmanager
def _tolerate_shutdown_state():
    """Let the debugger imports succeed even if ``threading`` thinks the
    interpreter is already shutting down.

    A child forked while its parent was tearing down inherits
    ``threading._SHUTTING_DOWN = True`` (``_after_fork`` never resets it), and
    from then on the first ``concurrent.futures.thread`` import — which IPython
    needs — raises ``RuntimeError: can't register atexit after shutdown``. We
    can't change how that process was created, so while importing we register
    threading atexits exactly as ``_register_atexit`` does, minus the shutdown
    check: the handler is still appended and still runs at this process's exit.
    Restored immediately after the import.
    """
    if not getattr(threading, "_SHUTTING_DOWN", False):
        yield
        return
    original = threading._register_atexit

    def lenient(func, *args, **kwargs):
        threading._threading_atexits.append(
            functools.partial(func, *args, **kwargs)
        )

    threading._register_atexit = lenient
    try:
        yield
    finally:
        threading._register_atexit = original


def _set_winsize(fd, cols, rows):
    with contextlib.suppress(OSError):
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


def _handshake(sock):
    """Send our banner and read the listener's one-line reply.

    ``"cols rows\\n"`` -> that terminal size. ``"continue\\n"``, or the listener
    hanging up -> ``None``: no shell for us, run on. A plain socat/nc listener
    never replies, so after ``SIZE_TIMEOUT`` we fall back to a default size.
    """
    sock.settimeout(SIZE_TIMEOUT)
    buf = bytearray()
    try:
        sock.sendall(client.banner().encode("utf-8"))
        while not buf.endswith(b"\n") and len(buf) < 32:
            chunk = sock.recv(1)
            if not chunk:
                return None  # listener hung up
            buf += chunk
    except TimeoutError:
        pass  # a plain socat/nc listener never replies
    except OSError:
        return None  # listener gone
    sock.settimeout(None)
    if buf.strip() == b"continue":
        return None
    try:
        cols, rows = (int(x) for x in buf.split())
        return cols, rows
    except ValueError:
        return DEFAULT_COLS, DEFAULT_ROWS


def _pump(src_read, dst_write, on_eof):
    """Copy src -> dst until either side dies, then fire on_eof once."""
    try:
        while True:
            data = src_read(65536)
            if not data:
                break
            dst_write(data)
    except OSError:
        pass
    finally:
        on_eof()


def _close_socket(sock):
    """Hang up for real. ``close()`` alone is not enough: while another thread
    is blocked in ``recv`` on it, Linux keeps the connection open — no FIN
    goes out, so the listener never learns we left and keeps the session (and
    everyone queued behind it) waiting. ``shutdown`` sends the FIN and wakes
    that reader."""
    with contextlib.suppress(OSError):
        sock.shutdown(socket.SHUT_RDWR)
    with contextlib.suppress(OSError):
        sock.close()


def _hangup(sock, release):
    """Give the socket and the shell lock back; later breakpoints can dial again."""
    _close_socket(sock)
    release()


def _abort(sock, release, reason):
    """Fail open after dialing: say why on stderr and on the listener, then
    hang up so the program runs on and later breakpoints still work.
    """
    text = f"*** reverse_ipdb: {reason}\n{traceback.format_exc()}"
    with contextlib.suppress(OSError):
        sys.stderr.write(text)
    with contextlib.suppress(OSError):
        sock.sendall(text.encode("utf-8", "replace"))
    _hangup(sock, release)


def run(frame, host=None, port=None):
    """Dial out and drop ``frame`` into IPython's debugger over a pty.

    No listener (or a shell already active elsewhere) -> no-op, program runs on.
    A listener that replies ``continue`` -> same no-op, nothing imported.
    A debugger that fails to import -> reported on stderr and to the listener,
    then the program runs on as well; never raises into the debugged program.
    """
    dialed = client.dial(host, port)
    if dialed is None:
        return
    sock, release = dialed

    # Banner out, one reply line back. A listener that answers "continue"
    # (reverse_ipdb-serve --no-queue while it's busy) gets no shell from us:
    # hang up and run on, without ever importing the debugger.
    size = _handshake(sock)
    if size is None:
        _hangup(sock, release)
        return
    cols, rows = size

    # Imported here, not at module top, on purpose: it keeps `import
    # reverse_ipdb` from pulling in IPython/prompt_toolkit (and, via IPython,
    # concurrent.futures) at import time. That preserves the fail-open cheap
    # no-op when nobody is listening, and avoids importing concurrent.futures
    # during interpreter shutdown — which raises "can't register atexit after
    # shutdown" in finalizing/multiprocessing contexts.
    try:
        with _tolerate_shutdown_state():
            from prompt_toolkit.input.vt100 import Vt100Input  # noqa: PLC0415
            from prompt_toolkit.output.vt100 import Vt100_Output  # noqa: PLC0415

            from reverse_ipdb.debugger import ReverseIPdb  # noqa: PLC0415
    except Exception:
        # A broken debugger environment (e.g. mismatched IPython/pygments
        # files) must not take the program down: report it on both ends of
        # the wire, hand the socket and the shell lock back, and run on.
        _abort(sock, release, "debugger import failed, continuing without a shell")
        return

    master_fd, slave_fd = os.openpty()  # api.set_trace guards non-POSIX first
    _set_winsize(slave_fd, cols, rows)

    slave_in = os.fdopen(slave_fd, "r", buffering=1, encoding="utf-8", errors="replace")
    slave_out = os.fdopen(os.dup(slave_fd), "w", buffering=1, encoding="utf-8",
                          errors="replace")

    pt_in = Vt100Input(slave_in)
    pt_out = Vt100_Output.from_pty(slave_out)

    dbg = ReverseIPdb(
        stdin=slave_in, stdout=slave_out,
        nosigint=True, readrc=False,
        pt_session_options={"input": pt_in, "output": pt_out},
    )
    # TerminalPdb refuses use_rawinput=False, but passing stdin set it off;
    # prompt_toolkit uses pt_in/pt_out regardless, so force it back on.
    dbg.use_rawinput = True

    close_lock = threading.Lock()
    closed = False
    debugging_thread = threading.get_ident()  # runs the prompt loop and do_detach

    def close_slave():
        for closer in (slave_in.close, slave_out.close):
            with contextlib.suppress(OSError):
                closer()

    # The master fd is closed exactly once, by the pty->socket pump when it
    # ends (the only reader), and writes check under a lock: a closed fd
    # number can be reused by the program at once, and a late read or write
    # must never land on whatever it opened.
    master_lock = threading.Lock()
    master_open = True

    def write_master(data):
        with master_lock:
            if not master_open:
                raise OSError("pty closed")
            os.write(master_fd, data)

    def close_master():
        nonlocal master_open
        with master_lock:
            if master_open:
                master_open = False
                with contextlib.suppress(OSError):
                    os.close(master_fd)

    def pump_output():
        """pty -> socket. Once the listener is gone, keep reading and discard:
        the debugger may still write (a command's output, the stack at the
        next breakpoint) before it detaches, and a pty nobody reads would
        block those writes, a closed one would fail them — either would land
        in the program. Ends when the slave closes, i.e. the debugger left."""
        connected = True
        try:
            while True:
                try:
                    data = os.read(master_fd, 65536)
                except OSError:
                    break  # EIO: slave closed
                if not data:
                    break
                if connected:
                    try:
                        sock.sendall(data)
                    except OSError:
                        connected = False
                        close()
        finally:
            close()
            close_master()

    def close():
        """Tear the bridge down, from either side.

        From the debugger (``detach``): it's past its prompt, so close the
        slave first and let the pty->socket pump drain the parting output.
        From a pump (the listener hung up): the debugger may be blocked in its
        prompt, and closing fds under a blocked reader never wakes it — the
        target thread would stay paused forever. ``hang_up`` ends the prompt
        instead; the debugger takes that as ``detach`` and comes back here to
        close the slave itself.
        """
        nonlocal closed
        from_debugger = threading.get_ident() == debugging_thread
        with close_lock:
            first = not closed
            closed = True
        if from_debugger:
            # It has left its prompt for good: retire the prompt thread (only
            # here — a debugger still mid-command needs it for one more,
            # hung-up prompt) and the slave.
            with contextlib.suppress(Exception):
                dbg.thread_executor.shutdown(wait=False)
            close_slave()
        if not first:
            return
        if from_debugger:
            output_pump.join(timeout=DRAIN_TIMEOUT)  # slave closed: it drains, then ends
        else:
            with contextlib.suppress(Exception):
                dbg.hang_up()
        _close_socket(sock)  # wakes the socket->pty pump blocked in recv
        release()

    output_pump = threading.Thread(target=pump_output, daemon=True)
    dbg._reverse_ipdb_close = close

    def to_pty(data):
        """listener -> pty. Ctrl-C bytes go to the debugger first: the pty is
        nobody's controlling terminal, so on its own a ``\\x03`` would only
        ever reach a prompt. Those the debugger handles are not passed on."""
        if b"\x03" in data:
            parts = data.split(b"\x03")
            kept = bytearray(parts[0])
            for part in parts[1:]:
                handled = False
                with contextlib.suppress(Exception):
                    handled = dbg.interrupt()
                if not handled:
                    kept += b"\x03"
                kept += part
            data = bytes(kept)
        if data:
            write_master(data)

    # Pump bytes both ways; either EOF tears the whole bridge down.
    output_pump.start()
    threading.Thread(
        target=_pump, args=(sock.recv, to_pty, close),
        daemon=True,
    ).start()

    try:
        dbg.set_trace(frame)
    except BaseException:
        close()
        raise
