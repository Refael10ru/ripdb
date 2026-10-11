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
import struct
import sys
import termios
import threading
import traceback

from reverse_ipdb import client

DEFAULT_COLS, DEFAULT_ROWS = 80, 24
SIZE_TIMEOUT = 0.5  # how long to wait for the listener's terminal size


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


def _read_size(sock):
    """Read the listener's ``"cols rows\\n"`` size line, or fall back to a default.

    reverse_ipdb-serve sends it right after attaching; a plain socat/nc listener
    won't, so we time out and use a sensible default.
    """
    sock.settimeout(SIZE_TIMEOUT)
    buf = bytearray()
    with contextlib.suppress(OSError):
        while not buf.endswith(b"\n") and len(buf) < 32:
            chunk = sock.recv(1)
            if not chunk:
                break
            buf += chunk
    sock.settimeout(None)
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


def _abort(sock, release, reason):
    """Fail open after dialing: say why on stderr and on the listener, then
    close the socket and release the shell lock so later breakpoints still work.
    """
    text = f"*** reverse_ipdb: {reason}\n{traceback.format_exc()}"
    with contextlib.suppress(OSError):
        sys.stderr.write(text)
    with contextlib.suppress(OSError):
        sock.sendall(text.encode("utf-8", "replace"))
    with contextlib.suppress(OSError):
        sock.close()
    release()


def run(frame, host=None, port=None):
    """Dial out and drop ``frame`` into IPython's debugger over a pty.

    No listener (or a shell already active elsewhere) -> no-op, program runs on.
    A debugger that fails to import -> reported on stderr and to the listener,
    then the program runs on as well; never raises into the debugged program.
    """
    dialed = client.dial(host, port)
    if dialed is None:
        return
    sock, release = dialed

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

    try:
        sock.sendall(client.banner().encode("utf-8"))
    except OSError:
        release()
        return

    cols, rows = _read_size(sock)
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

    def close():
        nonlocal closed
        with close_lock:
            if closed:
                return
            closed = True
        with contextlib.suppress(Exception):
            dbg.thread_executor.shutdown(wait=False)  # TerminalPdb's prompt thread
        for closer in (lambda: os.close(master_fd),
                       slave_in.close, slave_out.close, sock.close):
            with contextlib.suppress(OSError):
                closer()
        release()

    dbg._reverse_ipdb_close = close

    # Pump bytes both ways; either EOF tears the whole bridge down.
    threading.Thread(
        target=_pump, args=(lambda n: os.read(master_fd, n), sock.sendall, close),
        daemon=True,
    ).start()
    threading.Thread(
        target=_pump, args=(sock.recv, lambda d: os.write(master_fd, d), close),
        daemon=True,
    ).start()

    try:
        dbg.set_trace(frame)
    except BaseException:
        close()
        raise
