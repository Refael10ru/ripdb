"""The reverse IPython debugger.

``ReverseIPdb`` is IPython's ``TerminalPdb`` driven over a pseudo-terminal (set
up by ``pty_bridge``) so prompt_toolkit engages — tab/dot-completion, history
and colour. ``DetachMixin`` redefines the quit/EOF commands so that leaving the
shell *detaches* and lets the program continue, rather than raising ``BdbQuit``
into the debugged code; it comes first in the bases so its methods win.

Ctrl-C typed at the listener reaches us as a plain ``\\x03`` byte: the pty is
nobody's controlling terminal, so no SIGINT is generated and the program's own
handler is never involved. ``ReverseIPdb.interrupt`` decides what it means,
and never raises into the program's own code:

- at the prompt: nothing here, prompt_toolkit clears the line;
- while a command evaluates code (``!expr``, a bare statement, ``p``, ``pp``):
  ``KeyboardInterrupt`` in that command, and back to the prompt;
- while the program runs (after ``n``, ``s``, ``c``...): stop at the next line.
"""

import asyncio
import contextlib
import ctypes
import functools
import signal
import threading

from IPython.terminal.debugger import TerminalPdb

# Where the debugging thread is, as the socket pump sees it.
RUNNING = "running"          # the program runs; no stop in progress
IN_DEBUGGER = "debugger"     # stopped, handling a debugger command (l, bt, ...)
AT_PROMPT = "prompt"         # stopped, prompt_toolkit reading a line
IN_CODE = "code"             # stopped, evaluating code for a command (!x, p x)

PENDING_NOTE_DELAY = 1.0     # an interrupt not landed by then is stuck behind a C call


def _raise_keyboard_interrupt(thread_ident):
    """Raise ``KeyboardInterrupt`` in the debugging thread.

    The main thread with Python's default SIGINT handler gets a real SIGINT
    (thread-directed), which also breaks it out of a blocking call such as
    ``time.sleep``. Any other thread — or a main thread whose program has its
    own handler, which we must not trigger — gets an asynchronous exception,
    raised at its next bytecode: a blocking C call there is interrupted only
    once it returns. Returns whether the interrupt is immediate.
    """
    if (thread_ident == threading.main_thread().ident
            and signal.getsignal(signal.SIGINT) is signal.default_int_handler):
        signal.pthread_kill(thread_ident, signal.SIGINT)
        return True
    ctypes.pythonapi.PyThreadState_SetAsyncExc(
        ctypes.c_ulong(thread_ident), ctypes.py_object(KeyboardInterrupt)
    )
    return False


def _evaluates_code(method):
    """Mark a command as running the user's code, so Ctrl-C may interrupt it.

    Only these commands: a ``KeyboardInterrupt`` aimed at one of them can only
    land inside it or in the prompt loop around it (they never resume the
    program), and pdb catches it there and prompts again.
    """
    @functools.wraps(method)
    def wrapper(self, arg):
        self._reverse_ipdb_command += 1
        self._reverse_ipdb_state = IN_CODE
        try:
            return method(self, arg)
        finally:
            self._reverse_ipdb_state = IN_DEBUGGER
    return wrapper


class DetachMixin:
    """Make quitting the shell detach-and-continue instead of killing the program."""

    def do_detach(self, arg):
        """detach | q | Ctrl-D
        Remove all breakpoints, tear down the connection, let the program run."""
        self.clear_all_breaks()
        close = getattr(self, "_reverse_ipdb_close", None)
        if close is not None:
            close()  # set by pty_bridge: shut down the prompt, close pty + socket
        return super().do_continue(arg)

    # 'q' would normally raise BdbQuit inside the program; make it detach.
    do_quit = do_q = do_exit = do_EOF = do_detach

    def do_continue(self, arg):
        """c(ont(inue))
        Resume the program. With breakpoints armed the session stays open and
        the prompt comes back at the next one; with none, nothing could ever
        bring the prompt back, so this detaches too — the listener moves on to
        its next session and a later breakpoint in this process can dial again.
        """
        if not self.breaks:
            self.message("no breakpoints set: detaching, the program runs on")
            return self.do_detach(arg)
        return super().do_continue(arg)

    do_c = do_cont = do_continue


class ReverseIPdb(DetachMixin, TerminalPdb):
    """IPython's debugger over a pty, detaching on quit.

    Constructed by ``pty_bridge.run`` with stdin/stdout bound to the pty slave
    and prompt_toolkit I/O in ``pt_session_options``; the launcher also sets
    ``_reverse_ipdb_close`` to tear the bridge down on detach.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._reverse_ipdb_lock = threading.Lock()
        self._reverse_ipdb_hung_up = False
        self._reverse_ipdb_loop = None  # the running prompt's event loop, if any
        # Ctrl-C bookkeeping. The thread that called set_trace is the one that
        # stops, runs the commands and runs the program.
        self._reverse_ipdb_thread = threading.get_ident()
        self._reverse_ipdb_state = RUNNING
        self._reverse_ipdb_stopping = False  # Ctrl-C already asked for a stop
        self._reverse_ipdb_command = 0       # bumped per code-evaluating command
        self._reverse_ipdb_noted = -1        # the command we printed "pending" for

    default = _evaluates_code(TerminalPdb.default)
    do_p = _evaluates_code(TerminalPdb.do_p)
    do_pp = _evaluates_code(TerminalPdb.do_pp)

    def interaction(self, frame, tb_or_exc):
        self._reverse_ipdb_state = IN_DEBUGGER
        self._reverse_ipdb_stopping = False
        try:
            return super().interaction(frame, tb_or_exc)
        finally:
            self._reverse_ipdb_state = RUNNING

    def interrupt(self):
        """A Ctrl-C byte arrived from the listener (called by the socket pump).

        Returns whether it was handled here; if not, the byte goes on to the
        pty, where prompt_toolkit takes it as "clear the line".
        """
        state = self._reverse_ipdb_state
        if state == IN_CODE:
            if not _raise_keyboard_interrupt(self._reverse_ipdb_thread):
                timer = threading.Timer(PENDING_NOTE_DELAY, self._note_if_still_running,
                                        args=(self._reverse_ipdb_command,))
                timer.daemon = True
                timer.start()
            return True
        if state == RUNNING:
            if not self._reverse_ipdb_stopping:
                self._reverse_ipdb_stopping = True
                self.message("\nProgram interrupted: stopping at the next line it runs.")
                self.set_step()
            return True
        return False

    def _note_if_still_running(self, command):
        """The interrupt went in as an async exception and its command is still
        running: it must be inside a blocking C call, which the exception
        can't break. Say so once, rather than look frozen."""
        if (self._reverse_ipdb_state != IN_CODE or self._reverse_ipdb_command != command
                or self._reverse_ipdb_noted == command):
            return
        self._reverse_ipdb_noted = command
        with contextlib.suppress(Exception):  # the session may have closed meanwhile
            self.message("\nKeyboardInterrupt pending: it lands when the current "
                         "blocking call returns (Ctrl-] at the listener drops this "
                         "session meanwhile).")

    def _prompt(self):
        """Run one prompt, unless the listener has hung up: then end it as if
        Ctrl-D had been pressed, which the debugger takes as ``detach``.

        Runs in TerminalPdb's prompt thread. The pre-run hook fires once the
        prompt's future exists, so ``hang_up`` either sees its loop or the
        hook sees the flag — never neither.
        """
        app = self.pt_app.app
        self._reverse_ipdb_state = AT_PROMPT

        def arm():
            with self._reverse_ipdb_lock:
                if self._reverse_ipdb_hung_up:
                    app.exit(exception=EOFError)
                else:
                    self._reverse_ipdb_loop = asyncio.get_running_loop()

        app.pre_run_callables.append(arm)
        try:
            return super()._prompt()
        finally:
            self._reverse_ipdb_state = IN_DEBUGGER
            with self._reverse_ipdb_lock:
                self._reverse_ipdb_loop = None

    def hang_up(self):
        """The listener is gone. Called by the bridge from a pump thread.

        Closing fds under a blocked prompt never wakes it, so end the prompt
        directly: now if one is up, else as soon as the next one starts (after
        a running command, or at the next breakpoint). Either way the program
        detaches and runs on.
        """
        app = self.pt_app.app

        def end_prompt():
            if app.future is not None and not app.future.done():
                app.exit(exception=EOFError)

        with self._reverse_ipdb_lock:
            self._reverse_ipdb_hung_up = True
            loop = self._reverse_ipdb_loop
        if loop is not None:
            with contextlib.suppress(RuntimeError):  # loop already closed: prompt is over
                loop.call_soon_threadsafe(end_prompt)
