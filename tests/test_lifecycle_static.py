"""Static lifecycle audit: everything the library allocates has an owner and an end.

Every call that creates an OS resource, a thread, or process-wide state must
be listed in ``LIFECYCLES`` with where it is released; every thread must be a
daemon (it can never keep the host program alive); every piece of module- or
class-level state must be listed in ``MODULE_STATE`` with why it can't grow.
Adding an allocation without a documented lifecycle fails here — and so does
a stale entry. ``tests/test_lifecycle.py`` checks the same claims at run time.
"""

import ast
import pathlib

LIB = pathlib.Path(__file__).resolve().parent.parent / "reverse_ipdb"

RESOURCE_CALLS = {
    # file descriptors
    "open", "os.open", "os.openpty", "os.pipe", "os.dup", "os.dup2", "os.fdopen",
    "socket.socket", "socket.socketpair", "socket.create_connection", "*.accept",
    "selectors.DefaultSelector", "select.epoll", "select.poll", "mmap.mmap",
    "tempfile.TemporaryFile", "tempfile.NamedTemporaryFile", "tempfile.mkstemp",
    # threads, processes, loops
    "threading.Thread", "threading.Timer", "ThreadPoolExecutor", "ProcessPoolExecutor",
    "subprocess.Popen", "subprocess.run", "os.fork", "multiprocessing.Process",
    "asyncio.new_event_loop", "asyncio.run",
    # process-wide hooks
    "atexit.register", "sys.settrace", "threading.settrace", "sys.setprofile",
    "signal.signal", "signal.pthread_kill", "ctypes.pythonapi.PyThreadState_SetAsyncExc",
    "weakref.finalize", "sys.excepthook",
}

# (module, enclosing function, call) -> where and when it is released.
LIFECYCLES = {
    # --- target side: one set per debugging session -------------------------
    ("client", "dial", "socket.create_connection"):
        "release() returned by dial: shutdown + close, exactly once, when the "
        "session ends however it ends (continue reply, hang-up, detach, abort)",
    ("pty_bridge", "run", "os.openpty"):
        "master: closed once by the pty->socket pump when it ends (slave closed). "
        "slave: wrapped by slave_in, closed by close() from the debugging thread, "
        "or from the pump when a hang-up finds the debugger idle",
    ("pty_bridge", "run", "os.fdopen"):
        "slave_in / slave_out: same as the slave fd (close_slave)",
    ("pty_bridge", "run", "os.dup"):
        "the slave dup behind slave_out: close_slave",
    ("pty_bridge", "run", "threading.Thread"):
        "output pump: ends on EIO once the slave is closed, then closes the master. "
        "input pump: ends when release() shuts the socket down",
    ("debugger", "interrupt", "threading.Timer"):
        "one-shot 'pending' note, fires once after PENDING_NOTE_DELAY and ends",
    ("debugger", "_release", "sys.settrace"):
        "removes our trace function from the debugged thread (only if it's ours)",
    ("debugger", "_raise_keyboard_interrupt", "signal.pthread_kill"):
        "a signal, not a resource: only to the main thread, only when its SIGINT "
        "handler is Python's default; no handler is ever installed",
    ("debugger", "_raise_keyboard_interrupt", "ctypes.pythonapi.PyThreadState_SetAsyncExc"):
        "pending exception on the debugging thread, consumed when it lands",
    # --- the listener (your machine) -------------------------------------------
    ("serve", "serve", "socket.socket"):
        "the listening socket: `with` block; shutdown() wakes accept() on exit",
    ("serve", "acceptor", "*.accept"):
        "each connection is in `held` until release(): on detach, drop, stall, "
        "banner timeout, continue reply, or listener exit",
    ("serve", "acceptor", "threading.Thread"):
        "handshake thread: ends after one banner (bounded by BANNER_TIMEOUT)",
    ("serve", "serve", "threading.Thread"):
        "acceptor: ends when listener.shutdown() fails accept(); joined on exit",
}

# Module- and class-level names that hold state, and why each is bounded.
MODULE_STATE = {
    ("client", "_shell_lock"): "one lock; held for one session, released by release()",
    ("client", "_local"): "thread-local cooldown float; dies with its thread",
}

THREAD_CALLS = {"threading.Thread", "threading.Timer"}

# Created inside IPython / prompt_toolkit for each debugger, so invisible to the
# scan above; we own their release. Each must keep a release call in the code.
THIRD_PARTY = {
    ("pty_bridge", "dbg.thread_executor.shutdown"):
        "TerminalPdb's one-thread prompt executor: shut down when the session ends",
    ("debugger", "self.pt_loop.close"):
        "TerminalPdb's per-instance asyncio loop (unused, never closed by IPython)",
    ("debugger", "self.clear_all_breaks"):
        "bdb's process-wide Breakpoint registry entries this session created",
}


def _dotted(node):
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _dotted(node.value)
        return f"{base}.{node.attr}" if base else None
    return None


def _call_name(call):
    name = _dotted(call.func)
    if name in RESOURCE_CALLS:
        return name
    if isinstance(call.func, ast.Attribute) and f"*.{call.func.attr}" in RESOURCE_CALLS:
        return f"*.{call.func.attr}"
    return None


def _visit(node, module, func, scope, calls, daemonless):
    for child in ast.iter_child_nodes(node):
        is_func = isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
        inner, inner_scope = (child.name, child) if is_func else (func, scope)
        if isinstance(child, ast.Call):
            name = _call_name(child)
            if name:
                calls.setdefault((module, func, name), []).append(child.lineno)
                if name in THREAD_CALLS and not _is_daemon(child, scope):
                    daemonless.append(f"{module}.{func}:{child.lineno}")
        _visit(child, module, inner, inner_scope, calls, daemonless)


def _scan():
    calls, state, daemonless = {}, set(), []
    for path in sorted(LIB.glob("*.py")):
        module = path.stem
        tree = ast.parse(path.read_text())
        _visit(tree, module, "<module>", tree, calls, daemonless)
        for node in tree.body:
            if isinstance(node, ast.Assign):
                state.update((module, n) for n in _stateful_names(node.targets, node.value))
            elif isinstance(node, ast.ClassDef):
                for item in node.body:
                    if isinstance(item, (ast.Assign, ast.AnnAssign)) and item.value is not None:
                        targets = item.targets if isinstance(item, ast.Assign) else [item.target]
                        state.update((module, f"{node.name}.{n}")
                                     for n in _stateful_names(targets, item.value))
    return calls, state, daemonless


def _is_daemon(call, scope):
    if any(k.arg == "daemon" and isinstance(k.value, ast.Constant) and k.value.value is True
           for k in call.keywords):
        return True
    # Timer takes no daemon= argument: accept `<name>.daemon = True` in the same function.
    return any(isinstance(n, ast.Assign) and isinstance(n.targets[0], ast.Attribute)
               and n.targets[0].attr == "daemon" and isinstance(n.value, ast.Constant)
               and n.value.value is True for n in ast.walk(scope))


# Calls at module/class level that build behaviour, not state.
STATELESS_FACTORIES = {"_evaluates_code"}


def _stateful_names(targets, value):
    if isinstance(value, ast.Constant) or _is_constant_tuple(value):
        return []          # immutable constants are not state
    if isinstance(value, (ast.Lambda, ast.Name, ast.Attribute)):
        return []          # aliases (do_q = do_detach) and re-exports
    if isinstance(value, ast.Call) and _dotted(value.func) in STATELESS_FACTORIES:
        return []
    names = []
    for target in targets:
        for n in ast.walk(target):
            if isinstance(n, ast.Name) and not n.id.isupper() and n.id != "__all__":
                names.append(n.id)
    return names


def _is_constant_tuple(value):
    return isinstance(value, (ast.Tuple, ast.List)) and all(
        isinstance(e, ast.Constant) for e in value.elts)


def test_every_resource_has_a_documented_lifecycle():
    calls, _, _ = _scan()
    undocumented = sorted(f"{m}.{f}: {c} (line {lines[0]})"
                          for (m, f, c), lines in calls.items() if (m, f, c) not in LIFECYCLES)
    assert not undocumented, "add these to LIFECYCLES with where they are released:\n" + \
        "\n".join(undocumented)
    stale = sorted(f"{m}.{f}: {c}" for (m, f, c) in LIFECYCLES if (m, f, c) not in calls)
    assert not stale, "LIFECYCLES lists allocations that no longer exist:\n" + "\n".join(stale)


def test_every_thread_is_a_daemon():
    """A thread we start can never keep the debugged program (or the
    listener) from exiting."""
    _, _, daemonless = _scan()
    assert not daemonless, f"non-daemon threads: {daemonless}"


def test_module_and_class_state_is_accounted_for():
    _, state, _ = _scan()
    unlisted = sorted(f"{m}.{n}" for m, n in state if (m, n) not in MODULE_STATE)
    assert not unlisted, "module/class-level state needs a MODULE_STATE entry " \
        "saying why it can't grow:\n" + "\n".join(unlisted)
    stale = sorted(f"{m}.{n}" for m, n in MODULE_STATE if (m, n) not in state)
    assert not stale, f"MODULE_STATE lists state that no longer exists: {stale}"


def test_third_party_resources_keep_their_release_calls():
    released = set()
    for path in sorted(LIB.glob("*.py")):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Call) and _dotted(node.func):
                released.add((path.stem, _dotted(node.func)))
    missing = sorted(f"{m}: {c}()  -- {why}" for (m, c), why in THIRD_PARTY.items()
                     if (m, c) not in released)
    assert not missing, "release calls gone:\n" + "\n".join(missing)
