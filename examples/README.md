# Examples

Runnable scripts for trying `reverse_ipdb` locally. Each one talks to a listener on
`127.0.0.1:4444` by default (override with `DEBUG_HOST` / `DEBUG_PORT`).

`set_trace()` drops into the **IPython debugger with tab/dot-completion**, so
use `reverse_ipdb-serve` as the listener (it puts your terminal in raw mode so
completion and special keys work). Start it first, in a separate terminal:

```sh
uv run reverse_ipdb-serve                 # one session
uv run reverse_ipdb-serve --keep          # keep listening across detaches / fan-in
uv run reverse_ipdb-serve --keep --no-queue # don't queue: the rest continue (--no-queue 5: stay away 5s)
```

Then, in another terminal, run an example. If no listener is running, the
breakpoints are no-ops and the scripts simply print their results.

| Example          | Shows                                                        | Run                                  |
|------------------|--------------------------------------------------------------|--------------------------------------|
| `basic.py`       | one breakpoint; completion (`order[0].<TAB>`), inspect, detach | `uv run python examples/basic.py`       |
| `threads.py`     | the single-shell lock — one shell, other threads pass through | `uv run python examples/threads.py`     |
| `daemon_loop.py` | attach / detach / re-attach to a long-running process (use `--keep`) | `uv run python examples/daemon_loop.py` |
| `many_containers.py` | **fan-in** — several "containers" dial out to one `reverse_ipdb-serve --keep` | `uv run python examples/many_containers.py` |
| `madbg_example.py` | the **forward** alternative — [madbg](https://github.com/kmaork/madbg) (target listens, you connect in) | `uv run --with madbg python examples/madbg_example.py` |

## A full walkthrough (basic.py)

Terminal 1:

```sh
uv run reverse_ipdb-serve
# *** reverse_ipdb listening on 0.0.0.0:4444 (one-shot) — Ctrl-C to quit
```

Terminal 2:

```sh
uv run python examples/basic.py
```

Terminal 1 now shows a banner and an `ipdb>` prompt — the full IPython
debugger, so completion works. Try:

```
ipdb> order[0].   # press TAB -> name, qty, price, ...
ipdb> p total
16.5
ipdb> pp order
[{'name': 'widget', 'price': 2.5, 'qty': 3},
 {'name': 'gadget', 'price': 9.0, 'qty': 1}]
ipdb> detach
```

Back in terminal 2 the program finishes: `total: 16.5`.

## The container case

The library default host is `host.docker.internal`, so from inside a
container you usually don't set a host at all — just point the port at your
listener and run your own code:

```sh
# on your host
uv run reverse_ipdb-serve --port 4444

# inside the container (your app calls reverse_ipdb.set_trace())
DEBUG_PORT=4444 python your_app.py
```
