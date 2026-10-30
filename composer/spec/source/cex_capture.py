"""Run-scoped capture of per-rule counterexample analysis.

The prover tool already runs an LLM analysis of every violated rule; that text is otherwise only fed
back to the agent and discarded. Capture rides the ``on_analysis_complete`` callback (fires whichever
CEX handler is in use) into the run's ``BaseStore``, for the report phase to reshape into findings.
Records are keyed per INSTANTIATION of a rule in a spec file: a rule name is only unique within a
spec, so two specs that each declare rule ``r`` keep separate records, and a parametric rule
(``rule r(method f)``) is analyzed once per binding, so keying by rule alone would keep only whichever
binding finished last.

Records written before the spec file was part of the key carry no spec file. A lookup still returns
them for a rule that has no spec-scoped records, marked by ``spec_file=None``; whether a name-only
record can be trusted for one rule depends on whether another spec declares the same name, which only
the report knows (see ``report/findings.py``).
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
    the key, because a store ``filter`` matches on the value only. ``spec_file`` is the spec the
    prover run verified (``<slug>/<buffer>.spec``, as :func:`composer.spec.gen_types.buffer_spec_ref`
    names it); ``None`` on a record written without one."""
    rule: str
    spec_file: str | None = None
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

    def for_spec(self, spec_file: str) -> "SpecCexCapture":
        """The write side of this store for one prover run, which verifies exactly ``spec_file``."""
        return SpecCexCapture(self, spec_file)

    async def record(
        self, spec_file: str, path: RulePath, analysis: str, counterexample: str | None,
    ) -> None:
        """Record one instantiation of ``path.rule`` in ``spec_file``. Re-analyzing it in a later
        prover iteration overwrites it, so a rule's surviving records are its latest analysis per
        instantiation."""
        rec = CexAnalysis(
            rule=path.rule, spec_file=spec_file, label=_label(path), analysis=analysis,
            counterexample=counterexample,
        )
        await self.store.aput(
            self.namespace, f"{spec_file}|{path.rule}|{rec.label}", rec.model_dump(mode="json"),
        )

    async def for_rule(self, spec_file: str, rule_name: str) -> list[CexAnalysis]:
        """Every captured instantiation of ``rule_name`` in ``spec_file``, in a stable order (backends
        disagree on search order, and the report should not vary between runs).

        When the rule has no record under ``spec_file``, the records of ``rule_name`` written without
        a spec file are returned instead (each with ``spec_file=None``), for the caller to accept or
        refuse. Records for the same name under a different spec file are never returned."""
        recs = [CexAnalysis.model_validate(i.value) for i in await self._search(rule_name)]
        scoped = [r for r in recs if r.spec_file == spec_file]
        chosen = scoped or [r for r in recs if r.spec_file is None]
        return sorted(chosen, key=lambda a: a.label)

    async def forget_rule(self, spec_file: str, rule_name: str) -> None:
        """Drop ``rule_name``'s records in ``spec_file``, so an instantiation that failed in an earlier
        iteration but passes now cannot be written up as a current failure. Records of a same-named
        rule in another spec are left alone: this run says nothing about them."""
        for item in await self._search(rule_name):
            if item.value.get("spec_file") == spec_file:
                await self.store.adelete(self.namespace, item.key)

    async def _search(self, rule_name: str):
        return await self.store.asearch(self.namespace, filter={"rule": rule_name}, limit=_LIMIT)


@dataclass(frozen=True)
class SpecCexCapture:
    """A `CexAnalysisStore` bound to the spec file one prover run verifies, so every record and purge
    that run makes is scoped to that spec."""
    store: CexAnalysisStore
    spec_file: str

    async def record(self, path: RulePath, analysis: str, counterexample: str | None) -> None:
        await self.store.record(self.spec_file, path, analysis, counterexample)

    async def forget_rule(self, rule_name: str) -> None:
        await self.store.forget_rule(self.spec_file, rule_name)
