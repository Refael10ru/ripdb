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


def _abort(sock, release, reason):
    """Fail open after dialing: say why on stderr and on the listener, then
    hang up so the program runs on and later breakpoints still work.
    """
    text = f"*** reverse_ipdb: {reason}\n{traceback.format_exc()}"
    with contextlib.suppress(OSError):
        sys.stderr.write(text)
    with contextlib.suppress(OSError):
        sock.sendall(text.encode("utf-8", "replace"))
    release()


def run(frame, sock, release, cols, rows):
    """Drop ``frame`` into IPython's debugger over a pty, on a live ``sock``.

    Layer 2 (``client.attach``) already dialed and handshook; ``release``
    closes the socket and frees the shell lock, and is called exactly when the
    session ends, however it ends. A debugger that fails to import is reported
    on stderr and to the listener, then the program runs on; nothing here ever
    raises into the debugged program.
    """
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

    bridge = Bridge(sock, release, cols, rows)
    dbg = bridge.dbg = ReverseIPdb(
        bridge,
        stdin=bridge.slave_in, stdout=bridge.slave_out,
        nosigint=True, readrc=False,
        pt_session_options={"input": Vt100Input(bridge.slave_in),
                            "output": Vt100_Output.from_pty(bridge.slave_out)},
    )
    # TerminalPdb refuses use_rawinput=False, but passing stdin set it off;
    # prompt_toolkit uses its own input/output regardless, so force it back on.
    dbg.use_rawinput = True
    bridge.start()
    try:
        dbg.set_trace(frame)
    except BaseException:
        bridge.close()
        raise


class Bridge:
    """A pty for the debugger, and two pumps linking its master to ``sock``.

    The debugger talks to the slave (``slave_in`` / ``slave_out``); the
    output pump copies master -> socket, the input pump socket -> master.
    Torn down by ``close``, from whichever side ends first.
    """

    def __init__(self, sock, release, cols, rows):
        self.sock = sock
        self.release = release  # hangs up the socket and frees the shell lock
        self.dbg = None         # set by run, before start
        self.thread = threading.get_ident()  # the debugged thread
        self.master, slave = os.openpty()
        _set_winsize(slave, cols, rows)
        self.slave_in = os.fdopen(slave, "r", buffering=1, encoding="utf-8",
                                  errors="replace")
        self.slave_out = os.fdopen(os.dup(slave), "w", buffering=1, encoding="utf-8",
                                   errors="replace")
        # The master fd is closed exactly once, by the output pump when it ends
        # (its only reader), and writes check under the lock: a closed fd
        # number can be reused by the program at once, and a late write must
        # never land on whatever it opened.
        self.master_lock = threading.Lock()
        self.master_open = True
        self.close_lock = threading.Lock()
        self.closed = False
        self.output_pump = threading.Thread(target=self._pump_output, daemon=True)
        self.input_pump = threading.Thread(target=self._pump_input, daemon=True)

    def start(self):
        self.output_pump.start()
        self.input_pump.start()

    def close(self):
        """Tear the bridge down, from either side.

        From the debugged thread (``detach``): it's past its prompt, so close
        the slave first and let the output pump drain the parting output.
        From a pump (the listener hung up): the debugger may be blocked in its
        prompt, and closing fds under a blocked reader never wakes it — the
        debugged thread would stay paused forever. ``hang_up`` ends the prompt
        instead; the debugger takes that as ``detach`` and comes back here to
        close the slave itself.
        """
        from_debugger = threading.get_ident() == self.thread
        with self.close_lock:
            first, self.closed = not self.closed, True
        if from_debugger:
            # It has left its prompt for good (only here — a debugger still
            # mid-command needs the prompt thread for one more, hung-up prompt).
            self._retire_slave()
        if not first:
            return
        if from_debugger:
            self.output_pump.join(timeout=DRAIN_TIMEOUT)  # slave closed: it drains, ends
        else:
            idle = False
            with contextlib.suppress(Exception):
                idle = self.dbg.hang_up()
            if idle:
                # Not inside a stop or the trace function, and neither can
                # start now: nothing will read or write the pty again — even
                # if the program never traces another line (thread ended, or
                # blocked for good). Retire it here instead of waiting.
                self._retire_slave()
        self.release()  # hangs up the socket (waking the input pump), frees the lock

    def _retire_slave(self):
        """Nothing will prompt on the pty again: close the slave (the output
        pump then drains, ends, closes the master). Idempotent."""
        for closer in (self.slave_in.close, self.slave_out.close):
            with contextlib.suppress(OSError):
                closer()

    def _pump_output(self):
        """pty -> socket. Once the listener is gone, keep reading and discard:
        the debugger may still write (a command's output, the stack at the
        next breakpoint) before it detaches, and a pty nobody reads would
        block those writes, a closed one would fail them — either would land
        in the program. Ends when the slave closes, i.e. the debugger left."""
        connected = True
        try:
            while True:
                try:
                    data = os.read(self.master, 65536)
                except OSError:
                    break  # EIO: slave closed
                if not data:
                    break
                if connected:
                    try:
                        self.sock.sendall(data)
                    except OSError:
                        connected = False
                        self.close()
        finally:
            self.close()
            with self.master_lock:
                self.master_open = False
                with contextlib.suppress(OSError):
                    os.close(self.master)

    def _pump_input(self):
        """socket -> pty, until either side dies; then tear the bridge down."""
        try:
            while True:
                data = self.sock.recv(65536)
                if not data:
                    break
                self._to_pty(data)
        except OSError:
            pass
        finally:
            self.close()

    def _to_pty(self, data):
        """Ctrl-C bytes go to the debugger first: the pty is nobody's
        controlling terminal, so on its own a ``\\x03`` would only ever reach
        a prompt. Those the debugger handles are not passed on."""
        parts = data.split(b"\x03")
        kept = bytearray(parts[0])
        for part in parts[1:]:
            handled = False
            with contextlib.suppress(Exception):
                handled = self.dbg.interrupt()
            if not handled:
                kept += b"\x03"
            kept += part
        if kept:
            with self.master_lock:
                if not self.master_open:
                    raise OSError("pty closed")
                os.write(self.master, kept)
