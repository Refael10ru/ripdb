"""Unit checks for the debugger's release rules (no socket, no subprocess).

- A listener hang-up that lands while the debugged thread is inside the trace
  function is not "idle": pdb may print ("--Return--", "--Call--") before a
  stop even begins, so the pty must not be closed under it.
- A debugger clears only the breakpoints it created: bdb's registry is
  process-wide, and another session may be using it.
- A failure inside a stop (the prompt machinery, a command) detaches and is
  reported; it never raises into the debugged program.
"""

import bdb
import sys

from prompt_toolkit.input import DummyInput
from prompt_toolkit.output import DummyOutput

from reverse_ipdb.debugger import ReverseIPdb


def _debugger():
    return ReverseIPdb(nosigint=True, readrc=False,
                       pt_session_options={"input": DummyInput(), "output": DummyOutput()})


def test_hang_up_inside_trace_dispatch_is_not_idle(monkeypatch):
    dbg = _debugger()
    seen = {}

    def dispatch(self, frame, event, arg):          # stands in for bdb's dispatch
        seen["idle"] = self.hang_up()               # the listener hangs up right now
        return None

    monkeypatch.setattr(bdb.Bdb, "trace_dispatch", dispatch)
    dbg.trace_dispatch(None, "return", None)
    assert seen == {"idle": False}                  # the pty stays for the debugged thread
    assert not dbg._reverse_ipdb_dispatching


def test_hang_up_between_events_is_idle():
    dbg = _debugger()
    assert dbg.hang_up() is True                    # not stopped, not dispatching
    assert dbg._reverse_ipdb_detached


def test_a_debugger_clears_only_its_own_breakpoints():
    mine, theirs = _debugger(), _debugger()
    here = __file__
    line = test_a_debugger_clears_only_its_own_breakpoints.__code__.co_firstlineno + 1
    assert mine.set_break(here, line) is None
    assert theirs.set_break(here, line + 1) is None
    theirs_bp = bdb.Breakpoint.bplist[here, line + 1][-1]
    try:
        mine._clear_own_breaks()
        assert (here, line) not in bdb.Breakpoint.bplist
        assert bdb.Breakpoint.bpbynumber[theirs_bp.number] is theirs_bp  # untouched
        mine._clear_own_breaks()                    # idempotent
    finally:
        theirs._clear_own_breaks()
    assert (here, line + 1) not in bdb.Breakpoint.bplist


def test_a_failure_inside_the_stop_detaches_instead_of_raising(monkeypatch, capsys):
    """What a broken prompt (or anything else under interaction) must do:
    report, detach, and let the program run on — never raise through
    trace_dispatch into the program."""
    dbg = _debugger()

    def broken_cmdloop(self, intro=None):
        raise RuntimeError("the prompt machinery blew up")

    monkeypatch.setattr(type(dbg), "cmdloop", broken_cmdloop)
    closed = []
    dbg._reverse_ipdb_close = lambda: closed.append(True)

    def program():
        dbg.set_trace()
        return "ran on"                      # reached only if nothing raised

    assert program() == "ran on"
    assert sys.gettrace() is None
    assert dbg._reverse_ipdb_detached and closed      # closed (the hook is idempotent)
    err = capsys.readouterr().err
    assert "the debugger failed, detaching" in err and "RuntimeError" in err
