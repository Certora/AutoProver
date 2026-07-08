"""Canonical ``run_task`` task_ids for the AutoProve pipeline's fixed phases.

These strings are a contract between the orchestration that sets them on
``TaskInfo`` (``pipeline.core`` for system-analysis/report, ``spec.source.pipeline``
for the prover phases, ``pipeline.cli`` for doc discovery) and anything that keys
off them — notably the fake-LLM tape lanes in
``composer/testing/ui_harness_autoprove_Counter.py``, which route scripted
responses by task_id. The per-component ids (``extract-{i}`` / ``formalize-{i}``)
are backend-agnostic and live with the driver in ``pipeline.core``.
"""

DESIGN_DOC_DISCOVERY_TASK_ID = "doc-finder"
SYSTEM_ANALYSIS_TASK_ID = "system-analysis"
HARNESS_TASK_ID = "harness"
AUTOSETUP_TASK_ID = "autosetup"
SUMMARIES_TASK_ID = "summaries"
INVARIANTS_TASK_ID = "invariants"
INVARIANT_CVL_TASK_ID = "invariant-cvl"
REPORT_TASK_ID = "report"
