"""Fan-in example: several "containers" dial out to one listener.

This simulates code spread across containers — each worker is a separate
process that hits a breakpoint and dials OUT to the same listener. You run
ONE listener and walk them one at a time.

    uv run reverse_ipdb-serve --keep --queue             # terminal 1 (leave it running)
    uv run python examples/many_containers.py      # terminal 2

Watch terminal 1: the workers queue up ("queued: ... (N waiting)"), and you
drop into them one by one. At each `ipdb>` prompt — the full IPython debugger
with tab-completion — inspect `worker_id` and `payload` (try `payload[<TAB>`
or `pay<TAB>`), then type `detach` to advance to the next waiting worker.

With no listener running, every worker's breakpoint is a no-op and they all
just finish. Run the listener without ``--queue`` (its default) and only one
worker at a time gets a shell: the others are told to continue and finish
right away.
"""

import multiprocessing
import os
import time

import reverse_ipdb

HOST = os.environ.get("DEBUG_HOST", "127.0.0.1")
PORT = int(os.environ.get("DEBUG_PORT", "4444"))
N_WORKERS = int(os.environ.get("N_WORKERS", "3"))


def worker(worker_id):
    # Stagger so they arrive at the listener at slightly different times.
    time.sleep(0.3 * worker_id)
    payload = {"id": worker_id, "value": worker_id * 111}
    print(f"worker {worker_id} (pid {os.getpid()}) hitting its breakpoint")
    reverse_ipdb.set_trace(host=HOST, port=PORT)  # dials out; pauses until it's served
    print(f"worker {worker_id} resumed -> {payload['value']}")


def main():
    print(f"starting {N_WORKERS} workers -> {HOST}:{PORT} "
          f"(run `reverse_ipdb-serve --keep --queue` to catch them)")
    procs = [
        multiprocessing.Process(target=worker, args=(i,), name=f"worker-{i}")
        for i in range(1, N_WORKERS + 1)
    ]
    for p in procs:
        p.start()
    for p in procs:
        p.join()
    print("all workers finished")


if __name__ == "__main__":
    main()
