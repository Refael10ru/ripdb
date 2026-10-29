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

``interact`` opens a full IPython shell on the current frame over the same
connection (``_FrameShell``): prompts and output go through the pty instead of
the process's own stdio, which in a container isn't a terminal at all.
"""

import asyncio
import bdb
import contextlib
import ctypes
import functools
import signal
import sys
import threading
import traceback
from collections.abc import Callable, Iterator
from concurrent.futures import Future
from types import FrameType, TracebackType
from typing import TYPE_CHECKING, Any, TextIO, TypeVar, cast

from IPython.terminal.debugger import TerminalPdb
from IPython.terminal.embed import InteractiveShellEmbed
from prompt_toolkit.application import Application
from prompt_toolkit.application.current import create_app_session
from traitlets.config import Config

if TYPE_CHECKING:
    _Base = TerminalPdb  # the mixin's methods are written against TerminalPdb
else:
    _Base = object

_T = TypeVar("_T")

# Where the debugging thread is, as the socket pump sees it.
RUNNING = "running"          # the program runs; no stop in progress
IN_DEBUGGER = "debugger"     # stopped, handling a debugger command (l, bt, ...)
AT_PROMPT = "prompt"         # stopped, prompt_toolkit reading a line
IN_CODE = "code"             # stopped, evaluating code for a command (!x, p x)

PENDING_NOTE_DELAY = 1.0     # an interrupt not landed by then is stuck behind a C call


def _raise_keyboard_interrupt(thread_ident: int) -> bool:
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


def _evaluates_code(
    method: Callable[["ReverseIPdb", str], _T],
) -> Callable[["ReverseIPdb", str], _T]:
    """Mark a command as running the user's code, so Ctrl-C may interrupt it.

    Only these commands: a ``KeyboardInterrupt`` aimed at one of them can only
    land inside it or in the prompt loop around it (they never resume the
    program), and pdb catches it there and prompts again.
    """
    @functools.wraps(method)
    def wrapper(self: "ReverseIPdb", arg: str) -> _T:
        with self._evaluating():
            return method(self, arg)
    return wrapper


class _FrameShell(InteractiveShellEmbed):
    """The IPython shell behind ``interact``, driven over the debugger's pty.

    An embedded IPython would otherwise prompt on the process's own stdin and
    stdout — not the listener, and in a container not even a terminal. So its
    prompt_toolkit application is bound to the debugger's pty input/output,
    its prompts run on the debugger's prompt thread (as TerminalPdb's do, so
    an event loop running in the debugged thread can't clash with
    prompt_toolkit) and its cells run in the debugging thread like any other
    command, interruptible by Ctrl-C the same way.
    """

    def __init__(self, debugger: "ReverseIPdb", **kw: Any) -> None:
        self._reverse_ipdb = debugger
        pt_app = debugger.pt_app
        # __init__ builds the prompt session; an application takes the input
        # and output of the app session current at construction, for good.
        with create_app_session(input=pt_app.input, output=pt_app.output):
            super().__init__(**kw)  # type: ignore[no-untyped-call]

    def prompt_for_code(self) -> str:
        debugger = self._reverse_ipdb
        prompt = super().prompt_for_code
        assert self.pt_app is not None  # simple_prompt is off: the session exists
        future = debugger.thread_executor.submit(debugger._guarded_prompt, self.pt_app.app, prompt)
        return future.result()

    def run_cell(self, *args: Any, **kw: Any) -> Any:
        with self._reverse_ipdb._evaluating():
            return super().run_cell(*args, **kw)  # type: ignore[no-untyped-call]


class DetachMixin(_Base):
    """Make quitting the shell detach-and-continue instead of killing the program."""

    # Set by pty_bridge: shuts down the prompt, closes pty + socket.
    _reverse_ipdb_close: Callable[[], None] | None = None
    _reverse_ipdb_detached = False  # trace_dispatch releases us after this stop

    def do_detach(self, arg: str) -> bool | None:
        """detach | q | Ctrl-D
        Remove all breakpoints, tear down the connection, let the program run."""
        self._clear_own_breaks()
        self._reverse_ipdb_detached = True
        close = self._reverse_ipdb_close
        if close is not None:
            close()
        return super().do_continue(arg)

    # 'q' would normally raise BdbQuit inside the program; make it detach.
    do_quit = do_q = do_exit = do_EOF = do_detach

    def set_break(self, *args: Any, **kwargs: Any) -> str | None:
        """bdb's set_break, remembering which Breakpoint objects are ours.

        bdb keeps every breakpoint in one process-wide registry and its
        ``clear_all_breaks`` empties all of it — another session's included
        (one still running after a hang-up, while a newer one is attached).
        """
        first_new = len(bdb.Breakpoint.bpbynumber)
        error = super().set_break(*args, **kwargs)
        self.__dict__.setdefault("_reverse_ipdb_breaks", []).extend(
            bp for bp in bdb.Breakpoint.bpbynumber[first_new:] if bp is not None)
        return error

    def _clear_own_breaks(self) -> None:
        for bp in self.__dict__.pop("_reverse_ipdb_breaks", []):
            if bdb.Breakpoint.bpbynumber[bp.number] is bp:  # not already cleared
                bp.deleteMe()
        self.breaks = {}

    def do_continue(self, arg: str) -> bool | None:
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


class _PromptThread:
    """Runs each prompt on its own short-lived daemon thread.

    Stands in for ``TerminalPdb.thread_executor``, a ``ThreadPoolExecutor(1)``,
    and offers the two calls TerminalPdb makes on it. A thread pool refuses
    new work once ``concurrent.futures`` has seen the interpreter begin to
    shut down ("cannot schedule new futures after interpreter shutdown"). That
    happens as soon as the main thread finishes, while Python still waits for
    non-daemon threads, and in children forked after that point. A breakpoint
    hit in such a worker thread could never prompt, and the session detached
    at once. A plain thread has no such check.
    """

    def submit(self, fn: Callable[..., _T], *args: Any, **kwargs: Any) -> Future[_T]:
        future: Future[_T] = Future()

        def run() -> None:
            if not future.set_running_or_notify_cancel():
                return
            try:
                result = fn(*args, **kwargs)
            except BaseException as exc:
                future.set_exception(exc)
            else:
                future.set_result(result)

        threading.Thread(target=run, name="reverse_ipdb-prompt", daemon=True).start()
        return future

    def shutdown(self, wait: bool = True, **kwargs: Any) -> None:
        """Nothing to retire: each prompt's thread ends with its prompt."""


class ReverseIPdb(DetachMixin, TerminalPdb):
    """IPython's debugger over a pty, detaching on quit.

    Constructed by ``pty_bridge.run`` with stdin/stdout bound to the pty slave
    and prompt_toolkit I/O in ``pt_session_options``; the launcher also sets
    ``_reverse_ipdb_close`` to tear the bridge down on detach.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        # IPython's debugger classes are untyped: the ignores on super() calls
        # below are that, not anything about our own types.
        super().__init__(*args, **kwargs)  # type: ignore[no-untyped-call]
        # TerminalPdb's thread pool hasn't started a thread yet (it does on the
        # first submit); retire it and prompt on our own threads instead.
        self.thread_executor.shutdown(wait=False)
        self.thread_executor = _PromptThread()  # type: ignore[assignment]  # offers the two calls TerminalPdb makes
        self._reverse_ipdb_lock = threading.Lock()
        self._reverse_ipdb_hung_up = False
        # Set by detach or a listener hang-up; from then on the next trace
        # event in the debugged thread releases everything (see _release).
        self._reverse_ipdb_detached = False
        self._reverse_ipdb_released = False
        # True while the debugged thread is inside trace_dispatch: pdb may
        # print ("--Return--", "--Call--") before a stop even begins, so a
        # hang-up then must not count as idle and close the pty under it.
        self._reverse_ipdb_dispatching = False
        # The prompt that is up (the debugger's or interact's), as its event
        # loop and prompt_toolkit application, if any.
        self._reverse_ipdb_prompt: tuple[asyncio.AbstractEventLoop, Application[Any]] | None = None
        # Ctrl-C bookkeeping. The thread that called set_trace is the one that
        # stops, runs the commands and runs the program.
        self._reverse_ipdb_thread = threading.get_ident()
        self._reverse_ipdb_state = RUNNING
        self._reverse_ipdb_stopping = False  # Ctrl-C already asked for a stop
        self._reverse_ipdb_command = 0       # bumped per code-evaluating command
        self._reverse_ipdb_noted = -1        # the command we printed "pending" for

    @_evaluates_code
    def default(self, line: str) -> None:
        super().default(line)  # type: ignore[no-untyped-call]

    @_evaluates_code
    def do_p(self, arg: str) -> bool | None:
        return super().do_p(arg)

    @_evaluates_code
    def do_pp(self, arg: str) -> bool | None:
        return super().do_pp(arg)

    @contextlib.contextmanager
    def _evaluating(self) -> Iterator[None]:
        """The user's code is running (a code-evaluating command, an interact
        cell): Ctrl-C may interrupt it, see ``interrupt``."""
        self._reverse_ipdb_command += 1
        self._reverse_ipdb_state = IN_CODE
        try:
            yield
        finally:
            self._reverse_ipdb_state = IN_DEBUGGER

    def do_interact(self, arg: str) -> None:
        """interact
        Open a full IPython shell on the current frame — completion, history,
        magics — over this same connection. Assignments made there land in the
        frame's locals. Ctrl-D or ``exit`` comes back to the ipdb> prompt.
        """
        frame = self.curframe
        assert frame is not None  # we are at a prompt, so we are stopped somewhere
        # While the shell runs, the process's stdio is the pty — exactly what
        # pdb does around the code a command evaluates — so that output,
        # tracebacks and input() land at the listener, not in the container's
        # own stdout.
        saved = sys.stdin, sys.stdout, sys.stderr
        sys.stdin = cast(TextIO, self.stdin)
        sys.stdout = sys.stderr = cast(TextIO, self.stdout)
        try:
            config = Config(self.shell.config)  # a copy: the entry below is this shell's only
            # IPython 9's tip-of-the-day banner; IPython 8 has no such trait
            # and ignores the entry.
            config.TerminalInteractiveShell.enable_tip = False
            shell = _FrameShell(
                self, config=config, simple_prompt=False, confirm_exit=False,
                exit_msg="*** back to ipdb>",
                banner1="*** IPython on this frame; Ctrl-D or exit returns to ipdb>",
            )
            # pdb caches the stopped frame's locals up to 3.12 (a snapshot that
            # must be reused, so writes persist); from 3.13 f_locals is a
            # write-through proxy and pdb no longer caches it.
            if sys.version_info >= (3, 13):
                local_ns = frame.f_locals
            else:
                local_ns = self.curframe_locals
            shell(module=sys.modules.get(frame.f_globals.get("__name__", "")),
                  local_ns=local_ns)
        except Exception:
            # A shell that fails to come up or blows up is this session's
            # problem, shown here; it must never raise into the program.
            self.message(f"*** interact failed:\n{traceback.format_exc()}")
        finally:
            sys.stdin, sys.stdout, sys.stderr = saved

    def interaction(self, frame: FrameType | None,
                    tb_or_exc: TracebackType | BaseException | None) -> None:
        # Under the lock that hang_up takes: either the listener's hang-up sees
        # us stopped (and leaves the pty to us), or we see the hang-up and
        # never stop again — a stop can't begin on a pty being torn down.
        with self._reverse_ipdb_lock:
            if self._reverse_ipdb_detached:
                return None
            self._reverse_ipdb_state = IN_DEBUGGER
        self._reverse_ipdb_stopping = False
        try:
            super().interaction(frame, tb_or_exc)  # type: ignore[no-untyped-call]
        except Exception:
            # Whatever broke inside the stop — the prompt machinery, a command,
            # IPython itself — is this session's failure, not the program's:
            # it would otherwise travel up through trace_dispatch into the
            # debugged code. Say what happened on both ends, then detach.
            self._fail(traceback.format_exc())
            return None
        finally:
            self._reverse_ipdb_state = RUNNING

    def _fail(self, text: str) -> None:
        """Report a failure inside the debugger and leave the program alone."""
        text = f"*** reverse_ipdb: the debugger failed, detaching:\n{text}"
        with contextlib.suppress(Exception):
            sys.stderr.write(text)
        with contextlib.suppress(Exception):
            self.stdout.write(text)  # the listener, if the pty is still up
        try:
            self.do_detach("")
        except Exception:  # even detaching failed: at least stop tracing
            self._reverse_ipdb_detached = True
            with contextlib.suppress(Exception):
                self.set_continue()

    def interrupt(self) -> bool:
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

    def _note_if_still_running(self, command: int) -> None:
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

    def _prompt(self) -> str:
        prompt = super()._prompt
        try:
            return self._guarded_prompt(self.pt_app.app, prompt)
        except EOFError:
            # What cmdloop would make of it — but said here, in the prompt
            # thread: re-raised in the debugged thread, the exception's
            # traceback would capture cmdloop's frame and, through f_back,
            # the debugged frame and its locals, keeping them alive in a
            # cycle until the garbage collector happens to run.
            return "EOF"

    def _guarded_prompt(self, app: Application[Any], prompt: Callable[[], str]) -> str:
        """Run one prompt — the debugger's own, or the interact shell's —
        unless the listener has hung up: then end it as if Ctrl-D had been
        pressed, which either takes as "leave".

        Runs in the prompt thread. The pre-run hook fires once the prompt's
        future exists, so ``hang_up`` either sees the prompt or the hook sees
        the flag — never neither.
        """
        self._reverse_ipdb_state = AT_PROMPT

        def arm() -> None:
            with self._reverse_ipdb_lock:
                if self._reverse_ipdb_hung_up:
                    app.exit(exception=EOFError)
                else:
                    self._reverse_ipdb_prompt = (asyncio.get_running_loop(), app)

        app.pre_run_callables.append(arm)
        try:
            return prompt()
        finally:
            self._reverse_ipdb_state = IN_DEBUGGER
            with self._reverse_ipdb_lock:
                self._reverse_ipdb_prompt = None

    def hang_up(self) -> bool:
        """The listener is gone. Called by the bridge from a pump thread.

        Closing fds under a blocked prompt never wakes it, so end the prompt
        directly: now if one is up, else as soon as the next one starts (after
        a running command). Either way the program detaches and runs on, and
        no stop begins after this. Returns whether the debugger is idle — not
        inside a stop — in which case nothing will touch the pty again and the
        bridge retires it right away.
        """
        with self._reverse_ipdb_lock:
            self._reverse_ipdb_hung_up = True
            self._reverse_ipdb_detached = True
            prompt = self._reverse_ipdb_prompt
            idle = (self._reverse_ipdb_state == RUNNING
                    and not self._reverse_ipdb_dispatching)
        if prompt is not None:
            loop, app = prompt

            def end_prompt() -> None:
                if app.future is not None and not app.future.done():
                    app.exit(exception=EOFError)

            with contextlib.suppress(RuntimeError):  # loop already closed: prompt is over
                loop.call_soon_threadsafe(end_prompt)
        if idle:
            self._close_pt_loop()  # no prompt will ever use it; don't wait for _release
        return idle

    def trace_dispatch(self, frame: FrameType, event: str, arg: Any) -> Any:
        """bdb's trace function, until this session is over; then ``_release``.

        Detach (or a hang-up) can't release everything on the spot: pdb is
        still inside its stop then. The first trace event after it — in the
        debugged thread, outside any stop — does it, and from then on this
        debugger traces nothing.
        """
        with self._reverse_ipdb_lock:
            detached = self._reverse_ipdb_detached
            self._reverse_ipdb_dispatching = not detached
        if not detached:
            try:
                result = super().trace_dispatch(frame, event, arg)
            finally:
                with self._reverse_ipdb_lock:
                    self._reverse_ipdb_dispatching = False
            if not self._reverse_ipdb_detached:
                return result
        self._release()
        return None

    def _release(self) -> None:
        """Let go of the debugged program and of this session's resources.

        bdb's ``set_continue`` unhooks every frame *except* the outermost one
        it hooked (``botframe``: the module's top level, or a thread's
        bootstrap frame — alive as long as the program or thread), and IPython
        keeps the stopped frame in ``initial_frame``. Left alone, the last
        debugger, its stopped frame and every local in it stay reachable until
        the next session replaces them — or forever. Runs in the debugged
        thread; idempotent.
        """
        if getattr(sys.gettrace(), "__self__", None) is self:
            sys.settrace(None)
        frame: FrameType | None = sys._getframe()
        while frame is not None:
            if getattr(frame.f_trace, "__self__", None) is self:
                frame.f_trace = None
            frame = frame.f_back
        del frame
        if self._reverse_ipdb_released:
            return
        self._reverse_ipdb_released = True
        close = getattr(self, "_reverse_ipdb_close", None)
        if close is not None:
            close()  # from this thread: retires the prompt thread and the pty slave
        self._reverse_ipdb_close = None  # the bridge's closure refers back to us
        # Breakpoints still armed (a hang-up after `c`) live in bdb's
        # process-wide Breakpoint registry, not just on this debugger.
        with contextlib.suppress(Exception):
            self._clear_own_breaks()
        for name in ("botframe", "stopframe", "returnframe", "curframe",
                     "frame_returning", "initial_frame"):
            setattr(self, name, None)
        self.stack = []
        if "curframe_locals" in vars(self):  # a plain attribute up to 3.13
            self.curframe_locals = {}
        # TerminalPdb points its completer at the stopped frame's locals and
        # globals before every prompt, for tab completion.
        ptcomp = self._ptcomp
        if ptcomp is not None:
            ptcomp.ipy_completer.namespace = {}
            ptcomp.ipy_completer.global_namespace = {}
        for name in ("tb_lineno", "displaying", "_skip_cache", "_parent_skip_cache"):
            with contextlib.suppress(AttributeError):
                getattr(self, name).clear()
        # IPython 8 (Python 3.10) keeps its frame-skip predicates in class-level
        # lru_caches keyed on (debugger, frame): this debugger and the stopped
        # frame would live on in them. IPython 9 moved them onto the instance
        # (cleared just above). They are caches shared by every debugger, so
        # emptying them costs the others a recomputation and nothing else.
        for name in ("_cachable_skip", "_cached_one_parent_frame_debuggerskip"):
            with contextlib.suppress(AttributeError):
                getattr(type(self), name).cache_clear()
        self._close_pt_loop()

    def _close_pt_loop(self) -> None:
        """IPython's TerminalPdb makes an event loop per debugger and never
        closes it (nor uses it): an epoll fd and a socketpair, until garbage
        collection otherwise. Idempotent; called from the pump on an idle
        hang-up and from the debugged thread in _release, so it's serialized
        (a loop's close() isn't thread-safe)."""
        with self._reverse_ipdb_lock, contextlib.suppress(AttributeError, RuntimeError):
            self.pt_loop.close()
