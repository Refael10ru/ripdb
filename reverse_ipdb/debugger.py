"""The reverse IPython debugger.

``ReverseIPdb`` is IPython's ``TerminalPdb`` driven over a pseudo-terminal (set
up by ``pty_bridge``) so prompt_toolkit engages — tab/dot-completion, history
and colour. ``DetachMixin`` redefines the quit/EOF commands so that leaving the
shell *detaches* and lets the program continue, rather than raising ``BdbQuit``
into the debugged code; it comes first in the bases so its methods win.
"""

import asyncio
import contextlib
import threading

from IPython.terminal.debugger import TerminalPdb


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

    def _prompt(self):
        """Run one prompt, unless the listener has hung up: then end it as if
        Ctrl-D had been pressed, which the debugger takes as ``detach``.

        Runs in TerminalPdb's prompt thread. The pre-run hook fires once the
        prompt's future exists, so ``hang_up`` either sees its loop or the
        hook sees the flag — never neither.
        """
        app = self.pt_app.app

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
