"""Process-wide ledger of prover-REPORTED runtime for the jobs this autosetup run
actually executed.

Cache hits are excluded: the runners short-circuit to a cached ``ProverResult`` before
the fresh-run path that feeds this ledger, so a cached job (which consumed no prover
compute this run) never lands here. Mirrors the LLM usage ledger in ``llm_util`` — a
module-global accumulator reset at process start (``cli.main``) and harvested at the end
into ``prover_usage.json``, which composer ingests.

"Runtime" is the prover engine's own start-to-end wall time (``statsdata.json``
``run_id.start_to_end_time``, in milliseconds) — matching what composer records for its
own native prover runs, and NOT client-side wall-clock (which also covers cloud queue,
polling, and result download).
"""

import threading

_lock = threading.Lock()
_total_ms: int = 0
_runs: int = 0


def reset() -> None:
    """Start a clean ledger for this process."""
    global _total_ms, _runs
    with _lock:
        _total_ms = 0
        _runs = 0


def record_prover_runtime_ms(ms: int) -> None:
    """Add one freshly-executed prover run's prover-reported runtime (milliseconds)."""
    global _total_ms, _runs
    with _lock:
        _total_ms += int(ms)
        _runs += 1


def usage() -> dict[str, int]:
    """Serializable rollup written to ``prover_usage.json`` and ingested by composer."""
    with _lock:
        return {"ms": _total_ms, "runs": _runs}
