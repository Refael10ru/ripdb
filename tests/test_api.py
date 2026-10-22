"""Tests for the public API surface: default routing and aliasing."""

import inspect

import reverse_ipdb
from reverse_ipdb import api, client
from reverse_ipdb.debugger import ReverseIPdb


def test_frame_is_set_trace_only():
    # `frame` is advanced plumbing; the docker preset must not expose it.
    assert "frame" in inspect.signature(reverse_ipdb.set_trace).parameters
    assert "frame" not in inspect.signature(reverse_ipdb.docker_set_trace).parameters


def test_set_trace_and_alias():
    assert reverse_ipdb.set_trace is api.set_trace
    assert reverse_ipdb.set_trace_ipython is reverse_ipdb.set_trace


def test_pdb_variant_is_gone():
    # The line-based pdb path was dropped; only IPython-over-pty remains.
    assert not hasattr(reverse_ipdb, "set_trace_pdb")


def test_set_trace_hands_off_to_the_session_layer(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        client, "attach",
        lambda frame, host=None, port=None: captured.update(
            frame=frame, host=host, port=port),
    )
    reverse_ipdb.set_trace(host="h.example", port=1234)
    assert captured["host"] == "h.example"
    assert captured["port"] == 1234
    assert hasattr(captured["frame"], "f_lineno")  # a real caller frame


def test_docker_set_trace_defaults_to_docker_host(monkeypatch):
    monkeypatch.delenv("DEBUG_HOST", raising=False)
    captured = {}
    monkeypatch.setattr(client, "attach",
                        lambda frame, host=None, port=None: captured.update(host=host))
    reverse_ipdb.docker_set_trace()
    assert captured["host"] == "host.docker.internal"


def test_docker_set_trace_respects_arg_then_env(monkeypatch):
    captured = {}
    monkeypatch.setattr(client, "attach",
                        lambda frame, host=None, port=None: captured.update(host=host))

    monkeypatch.setenv("DEBUG_HOST", "10.0.0.5")
    reverse_ipdb.docker_set_trace()
    assert captured["host"] == "10.0.0.5"          # env beats the docker default

    reverse_ipdb.docker_set_trace(host="1.2.3.4")
    assert captured["host"] == "1.2.3.4"           # explicit arg wins


def test_reverse_ipdb_detaches_on_quit():
    assert ReverseIPdb.do_quit is ReverseIPdb.do_detach
    assert ReverseIPdb.do_EOF is ReverseIPdb.do_detach
