# Findings on the formalization seam

*What did this run find* is the report's first question, and `AutoProverReport.findings` is where
it is answered. This note says who writes those findings and why it is not the report.

Companion to [formalization-abstraction.md](./formalization-abstraction.md) (the driver ↔ backend
contract, where `findings` sits next to `fetch_verdicts`) and
[rust-applications.md](./rust-applications.md) (a Rust backend's side of it).

---

## The seam

`Formalizer.findings` returns **ready** `Finding`s. `build_report` takes it as a `FindingsBuilder`
callback, next to the `VerdictFetcher` it already had, and attaches what comes back. A backend with
nothing to say returns `[]` — the default — and the HTML omits the section.

```python
# composer/spec/source/report/collect.py
class FindingsBuilder(Protocol):
    async def __call__(
        self, *, contract_name: str, rules: list[RuleVerdict],
        properties: list[FormalizedProperty], groups: list[PropertyGroup],
    ) -> list[Finding]: ...
```

The report package keeps the schema (`Finding`, `IssueContent`, `FindingProvenance`, the severity /
impact / likelihood aliases), the view, the section, and the round-trip tests. It holds no model, no
evidence protocol and no findings templates. There is nothing in it shaped like one backend.

The hook runs after collect + grouping, so a backend sees the rules, properties and groups the
report will persist — the prover's write-up keys its "audit-level claim(s)" on those groups. It also
gets `outcomes` and `run`, supplied by the driver rather than the report: the driver already passes
the outcome set to `finalize` and `source_edits`, and a backend whose findings come from its own
results needs it. Recovering that from rendered `RuleVerdict.message` text would be the alternative.

A failure in the hook costs the findings, never the report (`RERAISE_REPORT_FAILURES` still flips
that for harnesses).

## The two implementations

**CVL / prover** — [`composer/spec/source/findings.py`](../composer/spec/source/findings.py). Pass 2
of the prover's counterexample handling. Pass 1 already runs during verification: a violated rule's
`cex_dump` becomes an explanation for the authoring agent, captured per instantiation in
`CexAnalysisStore` so a parametric rule keeps every binding. `ProverRunner.findings` reads that
store — it does not re-analyze — and asks `llm_heavy` for a `FindingDraft` per BAD rule: title,
impact, likelihood, and the Sherlock-shaped write-up. Severity comes from a fixed impact ×
likelihood matrix; the model never picks one. Best-effort per rule, capped concurrency.

This is prover code because its prompt says "the Certora Prover found a concrete counterexample" and
its evidence exists only for a run that produced calltraces.

**Rust / Crucible** — [`composer/rustapp/findings.py`](../composer/rustapp/findings.py). No model.
The wheel already reported the crash with its reproducing sequence, and where the author declared a
check expected to fail they already said why; `RustFormalResult.reported_verdicts()` folds those two
together and `fetch_verdicts` puts the result on the report's rows. `findings` reshapes one BAD row
into one `Finding`:

| Field | Source |
|---|---|
| `title` | `RustFormalResult.display_name` — the property's own words when the check verifies exactly one, otherwise the check name. The same rule the console rollup names rows by, so a finding and its verdict row cannot be called different things |
| `content.description` | The row's own message — the declared reason leads it, `NOT REPRODUCED` follows when this run reached no counterexample, evidence last |
| `content.summary` | Its opening line. Not a second write-up |
| `content.proof_of_concept` | The wheel's counterexample — only where the run actually produced one |
| `content.impact` | Empty. Nothing here has assessed impact |
| `provenance.risk_reasoning` | The declared reason, so reproduced and unreproduced are distinguishable without reading the evidence |
| `severity` | `informational`. A fuzzer crash is not an impact × likelihood pair, and a fabricated `high` is worse than none |

BAD only — which, after the fold, already includes a declared check this run did not reproduce.
ERROR and TIMEOUT stay verdict rows: a check that could not be run is a gap in coverage, not
something the run found.

Foundry and `none` keep the default `[]`.

## Why not the alternatives

- **An `EvidenceFetcher` a backend opts into.** This is what shipped first, and returning one
  always meant "run the Certora Prover write-up" — a backend-id check spelled as a protocol. The
  two backends that returned `None` were opting out of code that could never have served them.
- **A sum type (`SynthesizeFindings | ReadyFindings`) on the formalizer.** Same shared-layer
  pipeline behind a different flag. The point is that the shared layer never sees evidence.
- **Asking the findings model to score a crash.** Severity on a Rust finding stays
  `informational` until something actually assesses risk.
- **A second findings artifact beside the report.** Two answers to one question. Coverage,
  give-ups and skipped claims already render in `report.html`; the Findings section is the index
  and the group table is the property-keyed complete view.

`RuleVerdict.message` stays what it is — the per-row diagnostic. Both views remain.

## Tests

`tests/test_prover_findings.py` (the write-up: severity matrix, evidence, per-rule best effort),
`tests/test_rustapp_findings.py` (the declaration fold and the findings it produces), and the
report-layer half of `tests/test_autoprove_report.py` — that a ready `Finding` is attached,
rendered and round-tripped, that the default hook yields none, and that a failing hook still
yields a report.
