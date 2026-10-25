"""Tests for target-side dial logic: resolution, fail-open, shell lock, banner."""

import contextlib
import json
import os
import socket
import threading

import pytest

from reverse_ipdb import client


def test_resolve_target_defaults(monkeypatch):
    monkeypatch.delenv("DEBUG_HOST", raising=False)
    monkeypatch.delenv("DEBUG_PORT", raising=False)
    assert client.resolve_target() == (client.DEFAULT_HOST, client.DEFAULT_PORT)


def test_resolve_target_env(monkeypatch):
    monkeypatch.setenv("DEBUG_HOST", "10.0.0.9")
    monkeypatch.setenv("DEBUG_PORT", "9999")
    assert client.resolve_target() == ("10.0.0.9", 9999)


def test_resolve_target_args_win(monkeypatch):
    monkeypatch.setenv("DEBUG_HOST", "10.0.0.9")
    assert client.resolve_target(host="1.2.3.4", port=1234) == ("1.2.3.4", 1234)


def test_banner_is_a_pty_tagged_identity():
    line = client.banner()
    assert line.endswith(b"\n")
    msg = json.loads(line)
    assert msg["reverse_ipdb"] == "pty"
    assert msg["pid"] == os.getpid() and msg["thread"] == "MainThread" and msg["host"]
    assert msg["line"] is True                      # it can do line mode (agents)


def test_dial_no_listener_returns_none_and_releases_lock():
    # Port 1 refuses fast -> fail open, and the lock must be released.
    assert client.dial(host="127.0.0.1", port=1) is None
    assert client._shell_lock.acquire(blocking=False) is True
    client._shell_lock.release()


def test_dial_holds_lock_until_release():
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    accepted = []
    t = threading.Thread(target=lambda: accepted.append(srv.accept()))
    t.start()
    try:
        dialed = client.dial(host="127.0.0.1", port=port)
        assert dialed is not None
        sock, release = dialed
        # Lock held during the session: a second dial must no-op.
        assert client.dial(host="127.0.0.1", port=port) is None
        release()
        assert client._shell_lock.acquire(blocking=False) is True
        client._shell_lock.release()
        sock.close()
    finally:
        t.join()
        for s in accepted:
            s[0].close()
        srv.close()


def test_cool_down_is_per_thread():
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(2)
    srv.settimeout(5)
    port = srv.getsockname()[1]

    def dial_and_hang_up():
        dialed = client.dial(host="127.0.0.1", port=port)
        if dialed is not None:
            dialed[0].close()
            dialed[1]()
        return dialed

    try:
        client.cool_down(60)
        assert dial_and_hang_up() is None                 # this thread: no dial at all
        assert not client._shell_lock.locked()

        other = []
        t = threading.Thread(target=lambda: other.append(dial_and_hang_up()))
        t.start()
        t.join()
        assert other[0] is not None                       # another thread: dials
        srv.accept()[0].close()

        client._local.skip_until = 0.0                    # cooldown over
        assert dial_and_hang_up() is not None
        srv.accept()[0].close()
    finally:
        srv.close()


@pytest.fixture(autouse=True)
def _reset_lock():
    # Guard against a leaked lock / cooldown from a failed assertion in another test.
    yield
    client._local.skip_until = 0.0
    if client._shell_lock.locked():
        with contextlib.suppress(RuntimeError):
            client._shell_lock.release()
