"""Run-scoped capture of per-rule counterexample analysis.

The autoprove prover tool already runs an LLM analysis of every violated rule during the run
(``TrivialFanoutCexHandler`` -> ``analyze_cex_raw``); that text is otherwise consumed only as agent
feedback and discarded. This in-memory, run-scoped store captures it keyed by rule name
(last-write-wins across prover iterations) so the report phase can reshape the *final* iteration's
analysis into a finding without re-reasoning about the counterexample.

In-memory is sufficient: the report phase runs in the same process as formalization within
``composer.pipeline.core.run_pipeline``. A ``BaseStore``-backed variant (cf.
``composer.prover.report_store``) would be the resume-safe upgrade.
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class CexAnalysis:
    """One rule's captured counterexample analysis: the root-cause / fix explanation and, when
    available, the counterexample call-trace dump it was derived from."""
    analysis: str
    counterexample: str | None = None


class CexAnalysisStore:
    """In-memory ``{rule name -> CexAnalysis}``, last-write-wins. Written by the prover tool's
    callbacks as analysis completes each iteration; read by the report's findings synthesizer.

    A violated rule that remains violated to the end was analyzed on the final prover run, so
    last-write-wins holds its final-iteration analysis; a rule fixed before the end is GOOD in the
    report and its (stale) analysis is simply never looked up."""

    def __init__(self) -> None:
        self._by_rule: dict[str, CexAnalysis] = {}

    def record(self, rule_name: str, analysis: str, counterexample: str | None = None) -> None:
        """Store one rule's analysis under ``rule_name``."""
        self._by_rule[rule_name] = CexAnalysis(analysis=analysis, counterexample=counterexample)

    def get(self, rule_name: str) -> CexAnalysis | None:
        return self._by_rule.get(rule_name)
