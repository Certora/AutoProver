"""Task-ids for the agentic CEX analyzer's sub-agents.

The codegen pipeline runs through ``run_graph`` without a ``run_task`` scope, and
the per-rule CEX analyzers fan out under ``asyncio.gather`` (see
``AgenticCexHandler.analyze``). Concurrent sub-agents that share one task_id
can't be routed deterministically by the harness tape, so each is scoped to its
own task_id lane via ``set_current_task_id``.

The keys fold in the prover ``tool_call_id`` so analyses from distinct prover
runs land in distinct lanes (otherwise repeated runs of the same rule collide).
Centralized here so the harness tape reconstructs the same lane keys — the tape
controls the ``tool_call_id`` it scripts, so the derivation stays deterministic.
"""


def cex_rule_task_id(tool_call_id: str, rule_name: str) -> str:
    """Lane for the per-rule CEX analyzer of ``rule_name`` under one prover run."""
    return f"cex-{tool_call_id}-{rule_name}"


def cex_aggregator_task_id(tool_call_id: str) -> str:
    """Lane for the cross-rule CEX aggregator of one prover run."""
    return f"cex-agg-{tool_call_id}"
