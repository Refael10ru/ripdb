"""Public entry points.

Call these directly from the code you want to pause::

    import reverse_ipdb
    reverse_ipdb.set_trace()          # IPython over a pty -> tab/dot-completion
    reverse_ipdb.set_trace_ipython()  # explicit alias of set_trace()

POSIX only: the pty machinery (and so this package) imports ``termios``, so
``import reverse_ipdb`` requires a Unix platform.
"""

import os
import sys
from types import FrameType

from reverse_ipdb import client


def set_trace(*, host: str | None = None, port: int | None = None,
              frame: FrameType | None = None) -> None:
    """Pause here and hand an IPython shell (with completion) to the listener.

    IPython runs over a pseudo-terminal, so completion/history/colour work. No
    listener (or a shell already active elsewhere) -> no-op.

    Args:
        host: Hostname or IP of the listener to dial out to. When ``None``
            (the default), it is resolved from the ``DEBUG_HOST`` environment
            variable, falling back to ``host.docker.internal``.
        port: TCP port of the listener. When ``None`` (the default), it is
            resolved from the ``DEBUG_PORT`` environment variable, falling back
            to ``4444``.
        frame: The stack frame to stop in. When ``None`` (the default), the
            caller's frame is used — i.e. execution pauses at the ``set_trace``
            call site. Rarely passed explicitly.
    """
    client.attach(frame or sys._getframe(1), host=host, port=port)


def docker_set_trace(*, host: str | None = None, port: int | None = None) -> None:
    """``set_trace()`` preconfigured for containers.

    Identical to :func:`set_trace` except the host default is the Docker
    host-gateway name ``host.docker.internal``, which routes to the host
    machine from inside a container.

    On Docker Desktop (macOS/Windows) the name resolves automatically; on Linux
    start the container with ``--add-host=host.docker.internal:host-gateway``
    (compose: ``extra_hosts``) so it maps to the host gateway.

    Args:
        host: Hostname or IP of the listener to dial out to. When ``None``
            (the default), it is resolved from the ``DEBUG_HOST`` environment
            variable, falling back to ``host.docker.internal`` (this is the
            only behavioural difference from :func:`set_trace`).
        port: TCP port of the listener. When ``None`` (the default), it is
            resolved from the ``DEBUG_PORT`` environment variable, falling back
            to ``4444``.

    (There is no ``frame`` parameter: as a convenience preset this always stops
    at its own call site. If you need to control the frame — a wrapper, a signal
    handler, post-mortem — call :func:`set_trace` directly.)
    """
    host = host or os.environ.get("DEBUG_HOST") or client.DOCKER_HOST
    set_trace(host=host, port=port, frame=sys._getframe(1))


# Explicit alias — IPython-over-pty is the only mode.
set_trace_ipython = set_trace
