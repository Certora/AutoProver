"""Run-scoped capture of per-rule counterexample analysis.

The autoprove prover tool runs an LLM analysis of every violated rule during the run; that text is
otherwise consumed only as agent feedback and discarded. Capture happens through the prover callback
(``on_analysis_complete``), which fires regardless of which CEX handler is in use, so nothing here
assumes a particular handler. The analyses are persisted to the run's ``BaseStore`` (cf.
``composer.prover.report_store``) so the report phase can reshape the *final* iteration's analysis
into a finding.
"""
from dataclasses import dataclass

from langgraph.store.base import BaseStore
from pydantic import BaseModel


class CexAnalysis(BaseModel):
    """One rule's captured counterexample analysis: the root-cause / fix explanation and, when
    available, the counterexample call-trace dump it was derived from."""
    analysis: str
    counterexample: str | None = None


@dataclass(frozen=True)
class CexAnalysisStore:
    """Typed wrapper over a ``BaseStore`` for per-rule `CexAnalysis` capture. Construct once per run
    with the run's store and a PER-RUN ``namespace`` — rule names are not unique across runs, so the
    namespace (not the key) provides run isolation — then thread the same wrapper to the prover tool
    (write side) and the report phase (read side).

    Keyed by the bare rule name, last-write-wins across prover iterations: a rule that stays violated
    to the end was analyzed on the final run so its final analysis wins, while a rule fixed earlier is
    GOOD in the report and its (stale) analysis is never looked up."""
    store: BaseStore
    namespace: tuple[str, ...]

    async def record(self, rule_name: str, analysis: str, counterexample: str | None = None) -> None:
        """Store one rule's analysis under ``rule_name``, which must be the bare top-level rule name
        (``RulePath.rule``) — exactly what the report's ``RuleVerdict.name`` carries (POU's
        ``context[0]``) — so the findings join is an exact match."""
        await self.store.aput(
            self.namespace,
            rule_name,
            CexAnalysis(analysis=analysis, counterexample=counterexample).model_dump(mode="json"),
        )

    async def get(self, rule_name: str) -> CexAnalysis | None:
        item = await self.store.aget(self.namespace, rule_name)
        return CexAnalysis.model_validate(item.value) if item is not None else None
