"""The reverse IPython debugger.

``ReverseIPdb`` is IPython's ``TerminalPdb`` driven over a pseudo-terminal (set
up by ``pty_bridge``) so prompt_toolkit engages — tab/dot-completion, history
and colour. ``DetachMixin`` redefines the quit/EOF commands so that leaving the
shell *detaches* and lets the program continue, rather than raising ``BdbQuit``
into the debugged code; it comes first in the bases so its methods win.

The pty is not the target's controlling terminal, so a Ctrl-C typed at the
listener arrives as a plain ``\\x03`` byte and no SIGINT is ever generated.
``ReverseIPdb.interrupt`` is what the bridge calls for that byte whenever the
prompt is *not* up; it mimics pdb's Ctrl-C semantics without installing a
signal handler, so the program's own one is never touched.
"""

import contextlib
import ctypes
import signal
import threading

from IPython.terminal.debugger import TerminalPdb

# Where the debugging thread is, as seen by the bridge's socket pump.
AT_PROMPT = "prompt"      # prompt_toolkit is reading a line: Ctrl-C is just a key
IN_COMMAND = "command"    # executing a debugger command (``!x``, ``p x``, ...)
RUNNING = "running"       # the program runs (after ``n`` / ``s`` / ``c`` ...)

PENDING_NOTE_DELAY = 1.0  # an interrupt not landed by then is stuck behind a C call


def _raise_keyboard_interrupt(thread_ident):
    """Raise ``KeyboardInterrupt`` in another thread.

    The main thread with Python's default SIGINT handler gets a real SIGINT,
    which also breaks it out of a blocking call such as ``time.sleep``. Any
    other thread (or a main thread whose program installed its own handler,
    which we must not trigger) gets an asynchronous exception instead, raised
    at its next bytecode boundary — so a blocking C call there is interrupted
    only once it returns. Returns whether the interrupt is immediate.
    """
    if (thread_ident == threading.main_thread().ident
            and signal.getsignal(signal.SIGINT) is signal.default_int_handler):
        signal.pthread_kill(thread_ident, signal.SIGINT)
        return True
    ctypes.pythonapi.PyThreadState_SetAsyncExc(
        ctypes.c_ulong(thread_ident), ctypes.py_object(KeyboardInterrupt)
    )
    return False


class _PromptExecutor:
    """Wrap ``TerminalPdb.thread_executor`` to record when the prompt is up.

    IPython runs each prompt in a one-thread executor and blocks on the
    result; those are exactly the moments a Ctrl-C byte belongs to
    prompt_toolkit rather than to us.
    """

    def __init__(self, executor, dbg):
        self._executor = executor
        self._dbg = dbg

    def submit(self, fn, *args, **kwargs):
        dbg = self._dbg
        dbg._reverse_ipdb_state = AT_PROMPT
        dbg._reverse_ipdb_interrupted = False

        def at_prompt():
            try:
                return fn(*args, **kwargs)
            finally:
                dbg._reverse_ipdb_state = RUNNING

        return self._executor.submit(at_prompt)

    def shutdown(self, *args, **kwargs):
        return self._executor.shutdown(*args, **kwargs)


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
        # The thread that called set_trace: commands and the program run there.
        self._reverse_ipdb_thread = threading.get_ident()
        self._reverse_ipdb_state = RUNNING
        self._reverse_ipdb_interrupted = False
        self._reverse_ipdb_command = 0     # bumped per command: "is it still that one?"
        self._reverse_ipdb_noted = -1      # the command we last printed the note for
        self.thread_executor = _PromptExecutor(self.thread_executor, self)

    def onecmd(self, line):
        self._reverse_ipdb_command += 1
        self._reverse_ipdb_state = IN_COMMAND
        try:
            return super().onecmd(line)
        finally:
            self._reverse_ipdb_state = RUNNING

    def _note_if_still_running(self, command):
        """The interrupt went in as an async exception and the command it was
        aimed at is still running: it must be inside a blocking C call, which
        the exception can't break. Say so once, rather than look frozen."""
        if (self._reverse_ipdb_state != IN_COMMAND or self._reverse_ipdb_command != command
                or self._reverse_ipdb_noted == command):
            return
        self._reverse_ipdb_noted = command
        with contextlib.suppress(Exception):  # the session may have closed meanwhile
            self.message("\nKeyboardInterrupt pending: it lands as soon as the current "
                         "blocking call returns (or press Ctrl-] at the listener to drop "
                         "this session).")

    def interrupt(self):
        """A Ctrl-C byte arrived from the listener. Called by the socket pump.

        At the prompt the byte is prompt_toolkit's (it clears the line) and
        this is not called. During a debugger command: ``KeyboardInterrupt``
        in the debugging thread, which pdb reports and re-prompts. While the
        program runs: stop at its next traced event, like pdb's own SIGINT
        handler does after ``c``; a second Ctrl-C before that happens raises
        ``KeyboardInterrupt`` into the program instead (a blocking call or a
        tight loop may never reach a traced event).
        """
        state = self._reverse_ipdb_state
        if state == AT_PROMPT:
            return
        if state == IN_COMMAND or self._reverse_ipdb_interrupted:
            immediate = _raise_keyboard_interrupt(self._reverse_ipdb_thread)
            if state == IN_COMMAND and not immediate:
                timer = threading.Timer(PENDING_NOTE_DELAY, self._note_if_still_running,
                                        args=(self._reverse_ipdb_command,))
                timer.daemon = True
                timer.start()
            return
        self._reverse_ipdb_interrupted = True
        self.message("\nProgram interrupted: stopping at the next line "
                     "(Ctrl-C again raises KeyboardInterrupt).")
        self.set_step()
