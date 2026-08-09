"""Run-scoped capture of per-rule counterexample analysis.

The prover tool already runs an LLM analysis of every violated rule; that text is otherwise only fed
back to the agent and discarded. Capture rides the ``on_analysis_complete`` callback (fires whichever
CEX handler is in use) into the run's ``BaseStore``, for the report phase to reshape into findings.
Records are keyed per INSTANTIATION: a parametric rule (``rule r(method f)``) is analyzed once per
binding, and keying by rule alone would keep only whichever binding finished last.
"""
from dataclasses import dataclass

from langgraph.store.base import BaseStore
from pydantic import BaseModel

from composer.prover.ptypes import RulePath

#: ``asearch`` defaults to 10, and a rule can be instantiated once per method of a wide interface.
_LIMIT = 1000


def _label(path: RulePath) -> str:
    """Names one instantiation; "" when the rule is not parametric. Uses the explicit `RulePath` fields
    rather than ``pprint()``, which drops the contract whenever a method is set."""
    return ".".join(p for p in (path.contract, path.method) if p)


class CexAnalysis(BaseModel):
    """One violated instantiation's captured analysis. ``rule`` is a stored field, not merely part of
    the key, because a store ``filter`` matches on the value only."""
    rule: str
    label: str = ""
    analysis: str
    counterexample: str | None = None


@dataclass(frozen=True)
class CexAnalysisStore:
    """Typed ``BaseStore`` wrapper, built once per run with a PER-RUN ``namespace`` (rule names repeat
    across runs) and threaded to the prover tool (write) and the report phase (read). The namespace
    stays flat and the rule is matched via ``filter``: prefix search is an unanchored string match on
    some backends, so nesting per rule would leak records between rules with a shared name prefix."""
    store: BaseStore
    namespace: tuple[str, ...]

    async def record(self, path: RulePath, analysis: str, counterexample: str | None) -> None:
        """Record one instantiation. Re-analyzing it in a later prover iteration overwrites it, so a
        rule's surviving records are its latest analysis per instantiation."""
        rec = CexAnalysis(
            rule=path.rule, label=_label(path), analysis=analysis, counterexample=counterexample,
        )
        await self.store.aput(self.namespace, f"{path.rule}|{rec.label}", rec.model_dump(mode="json"))

    async def for_rule(self, rule_name: str) -> list[CexAnalysis]:
        """Every captured instantiation of ``rule_name``, in a stable order (backends disagree on
        search order, and the report should not vary between runs)."""
        items = await self._search(rule_name)
        return sorted((CexAnalysis.model_validate(i.value) for i in items), key=lambda a: a.label)

    async def forget_rule(self, rule_name: str) -> None:
        """Drop ``rule_name``'s records, so an instantiation that failed in an earlier iteration but
        passes now cannot be written up as a current failure."""
        for item in await self._search(rule_name):
            await self.store.adelete(self.namespace, item.key)

    async def _search(self, rule_name: str):
        return await self.store.asearch(self.namespace, filter={"rule": rule_name}, limit=_LIMIT)
