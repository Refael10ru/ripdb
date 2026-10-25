"""The reverse IPython debugger: IPython's ``TerminalPdb`` on a pty, made safe
to leave.

``pty_bridge.Bridge`` gives ``ReverseIPdb`` a pty to prompt on, so
prompt_toolkit engages (tab/dot-completion, history, colour). On top of
TerminalPdb it changes four things, none of which may ever raise into the
debugged program:

- **Leaving detaches.** ``q`` / ``quit`` / ``exit`` / Ctrl-D / ``detach``, and
  ``c`` with nothing armed, clear this debugger's breakpoints and let the
  program run on — never ``BdbQuit``. A failure inside a stop detaches too.
- **Ctrl-C** arrives as a ``\\x03`` byte (the pty is nobody's controlling
  terminal, so no SIGINT). ``interrupt`` decides what it means: nothing at
  the prompt, ``KeyboardInterrupt`` in a command that evaluates code, a stop
  at the next line while the program runs.
- **A listener hang-up** (``hang_up``) ends the prompt; no new stop begins.
- **Afterwards it lets go** of the program's frames and of its own resources
  (``_release``), so nothing outlives the session.

With a ``line_prompt`` it runs in **line mode**, for a program at the other
end rather than a person: no prompt_toolkit and no colour, each prompt is
``line_prompt`` written as is, and each command one line read from the pty.
"""

import asyncio
import bdb
import contextlib
import ctypes
import dataclasses
import enum
import functools
import os
import select
import signal
import sys
import threading
import traceback
from concurrent.futures import Future

from IPython.terminal.debugger import TerminalPdb

EOF_COMMAND = "EOF"       # the line pdb's cmdloop turns into do_EOF
PENDING_NOTE_DELAY = 1.0  # an interrupt not landed by then is stuck behind a C call
LINE_POLL = 0.1           # line mode: how often a waiting prompt checks for a hang-up


class State(enum.Enum):
    """Where the debugged thread is, as the socket pump sees it."""

    RUNNING = enum.auto()  # the program runs; no stop in progress
    STOPPED = enum.auto()  # stopped, handling a debugger command (l, bt, ...)
    PROMPT = enum.auto()   # stopped, prompt_toolkit reading a line
    CODE = enum.auto()     # stopped, evaluating code for a command (!x, p x)


@dataclasses.dataclass
class _Session:
    """Everything ReverseIPdb adds to a debugger. ``lock`` guards the fields
    the pump threads read: detached, dispatching, loop, state."""

    bridge: object                 # the pty_bridge.Bridge; None once released
    line_prompt: object = None     # line mode's prompt; None: prompt_toolkit
    thread: int = dataclasses.field(default_factory=threading.get_ident)  # debugged thread
    lock: threading.Lock = dataclasses.field(default_factory=threading.Lock)
    state: State = State.RUNNING
    detached: bool = False         # no stop begins after this; release follows
    dispatching: bool = False      # inside trace_dispatch (pdb may print before a stop)
    loop: object = None            # the running prompt's event loop, if any
    released: bool = False
    stopping: bool = False         # Ctrl-C already asked the running program to stop
    command: int = 0               # bumped per code-evaluating command
    breaks: list = dataclasses.field(default_factory=list)  # Breakpoints we created


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
        self._rv.command += 1
        self._rv.state = State.CODE
        try:
            return method(self, arg)
        finally:
            self._rv.state = State.STOPPED
    return wrapper


class _PromptThread:
    """Runs each prompt on its own short-lived daemon thread.

    Stands in for ``TerminalPdb.thread_executor`` (a ``ThreadPoolExecutor``)
    with the one call TerminalPdb makes on it. A thread pool refuses new work
    once the interpreter has begun shutting down — as soon as the main thread
    finishes, while Python still waits for non-daemon threads, and in
    children forked after that — so a breakpoint in such a worker could never
    prompt. A plain thread has no such check.
    """

    def submit(self, fn):
        future = Future()

        def run():
            try:
                future.set_result(fn())
            except BaseException as exc:
                future.set_exception(exc)

        threading.Thread(target=run, name="reverse_ipdb-prompt", daemon=True).start()
        return future


class ReverseIPdb(TerminalPdb):
    """IPython's debugger over the pty of ``bridge``, detaching on quit."""

    def __init__(self, bridge=None, line_prompt=None, **kwargs):
        super().__init__(**kwargs)
        # TerminalPdb's thread pool hasn't started a thread yet (it does on the
        # first submit); retire it and prompt on our own threads instead.
        self.thread_executor.shutdown(wait=False)
        self.thread_executor = _PromptThread()
        self._rv = _Session(bridge, line_prompt)
        if line_prompt is not None:
            try:
                self.set_theme_name("nocolor")  # IPython 9
            except AttributeError:
                self.set_colors("NoColor")      # IPython 8

    # --- leaving ------------------------------------------------------------

    def do_detach(self, arg):
        """detach | q | Ctrl-D
        Remove our breakpoints, tear down the connection, let the program run."""
        self._clear_breaks()
        self._rv.detached = True  # trace_dispatch releases everything after this stop
        if self._rv.bridge is not None:
            self._rv.bridge.close()
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

    def set_break(self, *args, **kwargs):
        """bdb's set_break, remembering which Breakpoint objects are ours: bdb
        keeps one process-wide registry, and its ``clear_all_breaks`` would
        empty another session's breakpoints too."""
        first_new = len(bdb.Breakpoint.bpbynumber)
        error = super().set_break(*args, **kwargs)
        self._rv.breaks += [bp for bp in bdb.Breakpoint.bpbynumber[first_new:] if bp]
        return error

    def _clear_breaks(self):
        """Delete the breakpoints this debugger created, and only those."""
        breaks, self._rv.breaks = self._rv.breaks, []
        for bp in breaks:
            if bdb.Breakpoint.bpbynumber[bp.number] is bp:  # not cleared already
                bp.deleteMe()
        self.breaks = {}

    def do_interact(self, arg):
        if self._rv.line_prompt is not None:  # it would start a prompt_toolkit shell
            self.error("interact needs a terminal: in line mode, run statements with !")
            return None
        return super().do_interact(arg)

    def do_debug(self, arg):
        if self._rv.line_prompt is not None:  # the nested debugger would prompt_toolkit
            self.error("debug needs a terminal: in line mode, set a breakpoint and step")
            return None
        return super().do_debug(arg)

    do_interact.__doc__ = TerminalPdb.do_interact.__doc__  # `help interact` as before
    do_debug.__doc__ = TerminalPdb.do_debug.__doc__

    # --- a stop ---------------------------------------------------------------

    default = _evaluates_code(TerminalPdb.default)
    do_p = _evaluates_code(TerminalPdb.do_p)
    do_pp = _evaluates_code(TerminalPdb.do_pp)

    def interaction(self, frame, tb_or_exc):
        # Under the lock hang_up takes: either the hang-up sees us stopped
        # (and leaves the pty to us), or we see it and never stop again.
        with self._rv.lock:
            if self._rv.detached:
                return None
            self._rv.state = State.STOPPED
        self._rv.stopping = False
        try:
            return super().interaction(frame, tb_or_exc)
        except Exception:
            # Whatever broke inside the stop — the prompt machinery, a command,
            # IPython itself — is this session's failure, not the program's.
            self._fail(traceback.format_exc())
            return None
        finally:
            self._rv.state = State.RUNNING

    def _fail(self, text):
        """Report a failure inside the debugger on both ends, then detach."""
        text = f"*** reverse_ipdb: the debugger failed, detaching:\n{text}"
        for stream in (sys.stderr, self.stdout):  # self.stdout: the listener
            with contextlib.suppress(Exception):
                stream.write(text)
        try:
            self.do_detach("")
        except Exception:  # even detaching failed: at least stop tracing
            self._rv.detached = True
            with contextlib.suppress(Exception):
                self.set_continue()

    def _prompt(self):
        """Run one prompt, unless the listener has hung up: then end it as if
        Ctrl-D had been pressed. Runs in the prompt thread. The pre-run hook
        fires once the prompt's future exists, so ``hang_up`` either sees its
        loop or the hook sees ``detached`` — never neither.
        """
        if self._rv.line_prompt is not None:
            return self._read_line()
        app = self.pt_app.app
        self._rv.state = State.PROMPT

        def arm():
            with self._rv.lock:
                if self._rv.detached:
                    app.exit(exception=EOFError)
                else:
                    self._rv.loop = asyncio.get_running_loop()

        app.pre_run_callables.append(arm)
        try:
            return super()._prompt()
        except EOFError:
            # cmdloop's own answer, given here: re-raised in the debugged
            # thread, the exception's traceback would pin cmdloop's frame and,
            # through f_back, the debugged frame and its locals.
            return EOF_COMMAND
        finally:
            self._rv.state = State.STOPPED
            with self._rv.lock:
                self._rv.loop = None

    def _read_line(self):
        """Line mode's prompt: write the prompt, read one line. Polls, so a
        hang-up (``detached``) ends it as end-of-file within ``LINE_POLL``."""
        self._rv.state = State.PROMPT
        try:
            self.stdout.write(self._rv.line_prompt)
            self.stdout.flush()
            fd = self.stdin.fileno()
            while not self._rv.detached:
                if select.select([fd], [], [], LINE_POLL)[0]:
                    line = os.read(fd, 65536)  # canonical mode: one whole line
                    if not line:
                        break
                    return line.decode("utf-8", "replace").rstrip("\r\n")
            return EOF_COMMAND
        except (OSError, ValueError):  # the pty is gone
            return EOF_COMMAND
        finally:
            self._rv.state = State.STOPPED

    # --- called by the bridge's pump threads ------------------------------------

    def interrupt(self):
        """A Ctrl-C byte arrived from the listener. Returns whether it was
        handled here; if not, it goes on to the pty and prompt_toolkit clears
        the line."""
        state = self._rv.state
        if state is State.CODE:
            if not _raise_keyboard_interrupt(self._rv.thread):
                timer = threading.Timer(PENDING_NOTE_DELAY, self._note_if_pending,
                                        args=(self._rv.command,))
                timer.daemon = True
                timer.start()
            return True
        if state is State.RUNNING:
            if not self._rv.stopping:
                self._rv.stopping = True
                self.message("\nProgram interrupted: stopping at the next line it runs.")
                self.set_step()
            return True
        # At the prompt: prompt_toolkit clears the line; a line-mode reader
        # has no line to clear, so the byte is dropped.
        return self._rv.line_prompt is not None

    def _note_if_pending(self, command):
        """The interrupt went in as an async exception and its command is still
        running: it is stuck in a blocking C call. Say so, rather than look
        frozen."""
        if self._rv.state is State.CODE and self._rv.command == command:
            with contextlib.suppress(Exception):  # the session may be gone by now
                self.message("\nKeyboardInterrupt pending: it lands when the current "
                             "blocking call returns (Ctrl-] at the listener drops this "
                             "session meanwhile).")

    def hang_up(self):
        """The listener is gone. End the prompt as if Ctrl-D had been pressed —
        now if one is up, else when the next one starts (after a running
        command) — and let no stop begin after this. Returns whether the
        debugger is idle (not in a stop, not inside the trace function): then
        nothing will touch the pty again and the bridge retires it at once.
        """
        app = self.pt_app.app

        def end_prompt():
            if app.future is not None and not app.future.done():
                app.exit(exception=EOFError)

        with self._rv.lock:
            self._rv.detached = True
            loop = self._rv.loop
            idle = self._rv.state is State.RUNNING and not self._rv.dispatching
        if loop is not None:
            with contextlib.suppress(RuntimeError):  # loop closed: the prompt is over
                loop.call_soon_threadsafe(end_prompt)
        if idle:
            self._close_pt_loop()  # no prompt will ever use it
        return idle

    # --- afterwards -------------------------------------------------------------

    def trace_dispatch(self, frame, event, arg):
        """bdb's trace function until the session is over. Detach can't release
        everything from inside its stop; the first trace event after it does,
        and from then on this debugger traces nothing."""
        with self._rv.lock:
            detached = self._rv.detached
            self._rv.dispatching = not detached
        if not detached:
            try:
                result = super().trace_dispatch(frame, event, arg)
            finally:
                with self._rv.lock:
                    self._rv.dispatching = False
            if not self._rv.detached:
                return result
        self._release()
        return None

    def _release(self):
        """Let go of the program's frames and of this session's resources.

        bdb's ``set_continue`` unhooks every frame except the outermost one it
        hooked (the module's top level, or a thread's bootstrap frame — alive
        as long as the program or thread), and IPython keeps the stopped frame
        and points its completer at its namespaces. Left alone, the debugger,
        its stopped frame and every local in it would stay reachable. Runs in
        the debugged thread; idempotent.
        """
        if getattr(sys.gettrace(), "__self__", None) is self:
            sys.settrace(None)
        frame = sys._getframe()
        while frame is not None:
            if getattr(frame.f_trace, "__self__", None) is self:
                frame.f_trace = None
            frame = frame.f_back
        del frame
        if self._rv.released:
            return
        self._rv.released = True
        bridge, self._rv.bridge = self._rv.bridge, None
        if bridge is not None:
            bridge.close()  # from this thread: retires the pty slave
        with contextlib.suppress(Exception):
            self._clear_breaks()  # a hang-up after `c` leaves them armed
        self.botframe = self.stopframe = self.returnframe = self.curframe = None
        self.frame_returning = self.initial_frame = None
        self.stack = []
        if "curframe_locals" in vars(self):  # a plain attribute up to 3.13
            self.curframe_locals = {}
        with contextlib.suppress(AttributeError):
            self._ptcomp.ipy_completer.namespace = {}
            self._ptcomp.ipy_completer.global_namespace = {}
        self.tb_lineno.clear()
        self.displaying.clear()
        for owner, cache in ((self, "_skip_cache"), (self, "_parent_skip_cache"),
                             (type(self), "_cachable_skip"),
                             (type(self), "_cached_one_parent_frame_debuggerskip")):
            # IPython 9 keeps its frame-skip caches on the instance; IPython 8 in
            # class-level lru_caches keyed on (debugger, frame). Either would pin
            # this debugger and the stopped frame.
            with contextlib.suppress(AttributeError):
                found = getattr(owner, cache)
                found.clear() if isinstance(found, dict) else found.cache_clear()
        self._close_pt_loop()

    def _close_pt_loop(self):
        """IPython's TerminalPdb makes an event loop per debugger and never
        closes it (nor uses it): an epoll fd and a socketpair until garbage
        collection. Serialized: the pump and the debugged thread both get here,
        and a loop's close() isn't thread-safe. Idempotent."""
        with self._rv.lock, contextlib.suppress(AttributeError, RuntimeError):
            self.pt_loop.close()
