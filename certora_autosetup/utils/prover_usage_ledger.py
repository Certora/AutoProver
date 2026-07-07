"""Process-wide ledger of prover-REPORTED runtime for the jobs this autosetup run
actually executed.

Cache hits are excluded: the runners short-circuit to a cached ``ProverResult`` before
the fresh-run path that feeds this ledger, so a cached job (which consumed no prover
compute this run) never lands here. Mirrors the LLM usage ledger in ``llm_util`` — a
module-global accumulator reset at process start (``cli.main``) and harvested at the end
into ``prover_usage.json``, which composer ingests.

"Runtime" (milliseconds) is per-run how long the prover actually ran: for cloud jobs the
server-reported job duration (start→finish from ``JobInfo``), for local jobs the wall-clock
duration (local runs are serialized — one prover at a time, no queueing — so wall-time is
the run time). It deliberately excludes the cloud queue / polling / download overhead that
a client-side cloud wall-clock would include.
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
