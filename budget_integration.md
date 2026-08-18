# Run budgets: integration guide

This document is for the frontend and cloud-orchestration owners. It covers how to
launch a budgeted AutoProve run, what a budget does to the run's outputs when it
trips, and what your side needs to handle: the new report fields, the on-disk
artifacts, and the operational caveats of the metering.

A *run budget* is a USD ceiling on LLM spend, enforced inside the pipeline while it
runs. When spend approaches a limit, in-flight agents are told to wrap up and publish
their best partial result; when spend exceeds the limit, they are terminated
cooperatively. A component whose formalization was cut short this way is **curtailed**:
it is not a delivery, its outputs are quarantined on disk, and it is reported in a
dedicated appendix of `report.json` rather than in the main property/rule tables.

## 1. The budget file and the `--budget` flag

Both pipeline entry points (prover and foundry) accept:

```
--budget path/to/budget.json
```

Omitting the flag runs unbudgeted — nothing about an unbudgeted run's behavior or
outputs changes.

The file is JSON natively; a `.yaml`/`.yml` file also works when PyYAML happens to be
installed (it is *not* a declared dependency — cloud environments should stick to
JSON). Schema:

```json
{
  "total": 25.0,
  "caps": {
    "formalization": 15.0,
    "property_extraction": 5.0
  }
}
```

- `total` (required, USD, > 0) — the run **pool**, the real bound on overall spend.
- `caps` (optional, USD each, >= 0) — per-phase ceilings, any subset of the five
  phase names below. A phase without an explicit cap defaults to `total`, i.e. it is
  bounded by the pool alone.

Caps are **ceilings, not allotments**: they bound how much a single phase may hog and
need not sum to `total`. Whatever a phase doesn't spend simply stays in the pool for
later phases — rollover is automatic. Cost accrues to the active phase cap *and* the
pool simultaneously; enforcement trips on whichever is tighter.

The five phase names (the keys of `PhaseBudget` in `composer/pipeline/ptypes.py`):

| Phase | Covers |
|---|---|
| `system_analysis` | component analysis of the contract system |
| `system_preparation` | harness construction |
| `formalization_preparation` | prover pre-formalization fan-out: structural invariants, custom summaries, protocol-specific setup |
| `property_extraction` | per-component property inference |
| `formalization` | per-component CVL / test authoring (usually the dominant cost) |

Work outside any named phase — notably the report/grouping step at the end — accrues
to (and feels pressure from) the pool directly.

Validation is strict and fail-fast: unknown top-level keys, unknown phase names,
`total <= 0`, or a negative cap all reject the file with a `ValueError` **before any
services spin up**, so a malformed budget fails the run immediately at startup rather
than mid-pipeline. A cap of `0.0` is legal: that phase starts already inside its
wrap-up window (useful for forcing curtailment in tests).

The parsed budget is echoed into the run's data log as a `budget` record
(`{"total": ..., "caps": {...}}`), so run records show what the run was launched with.

### Enforcement semantics (what "trips" means)

Two thresholds, per counter (each phase cap, and the pool):

- **Wrap-up window** — at **80%** of a counter, the running agent gets a one-time
  injected warning telling it to finish in an orderly fashion, and its validation
  gates are lifted so it can publish a best-effort partial result. The pipeline also
  stops launching new work that would immediately be told to pack it in (e.g. further
  property-extraction rounds).
- **Hard stop** — past **100%**, the agent is terminated cooperatively between turns.

Both checks run *between* agent turns; see the caveats in §4 for what "cooperatively"
implies about overshoot.

## 2. Curtailment in `report.json`

`report.json` (written to `certora/ap_report/report.json`) keeps `schema_version:
"3.0"` — curtailment adds fields, it does not break existing ones.

A curtailed component contributes **nothing** to the main tables: it is excluded from
`properties`, `rules`, `groups`, `skipped`, and `prover_links`. Whatever it published
was accepted with the validation gates lifted, so neither the encoding nor any
verification result it saw along the way is reliable. Instead it appears in the
appendix list:

```jsonc
"curtailed_components": [
  {
    "component": "Withdrawals",
    // Project-relative path of the quarantined partial encoding (see §3);
    // null when the run stopped before anything was published.
    "artifact": "certora/specs/Withdrawals.spec.unverified",
    // Last verification-run link the partial carried, if any. Context only —
    // its outcome predates the final content and proves nothing about it.
    "run_link": "https://prover.certora.com/output/...",
    // Optional account of the termination (hard-stop message, or the
    // author's own words).
    "detail": "Token cost budget exhausted; the agent was cooperatively terminated.",
    // The component's inferred properties, partitioned by disposition:
    "drafted": [
      // claims the author made before being cut off — encoded but UNVERIFIED
      { "title": "solvency_preserved", "sort": "safety_property",
        "description": "...", "units": ["solvencyPreserved"] }
    ],
    "skipped": [
      // properties the author explicitly declined (usually citing the budget)
      { "title": "reentrancy_safe", "sort": "attack_vector",
        "description": "...", "reason": "insufficient budget remaining" }
    ],
    "unattempted": [
      // properties the author never reached
      { "title": "fee_bounded", "sort": "invariant", "description": "..." }
    ]
  }
]
```

Field notes for the FE:

- Every property record carries the standard formulation fields `title` (unique
  snake_case id), `sort` (`attack_vector` | `safety_property` | `invariant`), and
  `description`.
- `drafted[].units` are the rule/test names the author *declared* at publish — an
  unchecked claim, never validated against a judge or a verification run. Do not
  render them as results.
- All three lists can be empty; a component cut off before publishing anything has
  `artifact: null` and typically everything under `unattempted`.

`coverage` gains:

- `curtailed_component_count` — the appendix length;
- a `warnings` entry when it is nonzero: `"N component(s) were cut short by the run
  budget; their properties are listed in the curtailed appendix, not the groups."`

Rendering guidance: treat `curtailed_components` as a first-class "cut short by
budget" section, visually distinct from both delivered results and `gave_up_components`
(a give-up is the author's considered infeasibility judgment; curtailment is the run
running out of money).

One more report-level behavior: when *zero* properties were formalized run-wide
(everything gave up or was curtailed), the report is still written, but the LLM
grouping step is skipped — expect `groups: []` with a populated appendix.

## 3. Disk semantics

**Quarantine.** A curtailed component's partial encoding is persisted for inspection
under a poisoned name: the normal artifact filename plus the suffix `.unverified`
(e.g. `Withdrawals.spec.unverified`, `Counter.t.sol.unverified`), in the same
directory as real deliverables. Only the artifact text is written — **no** `.conf`,
no commentary, no property-map JSON — so nothing runnable or machine-readable points
at content that never passed the validation gates. The suffix also keeps forge from
compiling a quarantined `.t.sol`. (The analysis-phase
`properties/{stem}.properties.json` *is* present — it is written before formalization
begins, for every attempted component regardless of outcome.)

**Caching.** Curtailed results are never cached. A re-run of the same project redoes
the curtailed components from scratch (and, given budget, delivers them properly);
already-delivered components come from cache as usual.

**`components_to_prover_runs.json`** (`.certora_internal/autoProve/`): deliveries
only. Curtailed components never get an entry — a partial's last run link says
nothing about its final content.

**Exit code.** The console entry points exit `1` only when *every* attempted
component ended without a deliverable (`all_failed`: gave up, crashed, or curtailed).
A run where some components delivered and some were curtailed exits `0` — 
orchestration must read `coverage.curtailed_component_count` (or the appendix) to
detect partial curtailment; the exit code will not tell you. Each curtailed component
also appears in the result's failure list, printed at the end of a console run as
`NAME: BUDGET: formalization cut short (unvalidated partial kept at PATH | nothing
published)`.

## 4. Operational caveats

- **Enforcement is cooperative, not preemptive.** Budget checks run between agent
  turns; a turn already in flight completes and its cost lands after the fact. With
  several components formalizing in parallel, each can have a turn in flight when the
  pool trips. Treat `total` as a target with margin — worst case overshoot is roughly
  one expensive turn per concurrently running agent — not as a billing guarantee.
- **The 80% threshold is a global constant** (`BUDGET_PRESSURE_THRESHOLD`,
  `composer/diagnostics/budget.py`), not a per-run knob. Consequence: the wrap-up
  window is only 20% of the cap. For small caps that can be about one worst-case turn
  of headroom, so an agent may hard-stop *mid-wrap-up* — orchestration and the FE
  must tolerate curtailed components with `artifact: null` even though the budget
  "asked nicely" first.
- **A model missing from the pricing table accrues $0.** The meter prices each call
  via `composer/llm/pricing.py`; an unpriced model spends invisibly and the budget
  never trips on it. Silent in production — keep the pricing table in sync with the
  model roster.
- **Warm caches spend ~$0.** Components served from the generation cache skip
  authoring entirely, so a budget calibrated for a cold run will never trip on a
  re-run against the same cache namespace. Conversely (per §3) curtailed work is
  never cached, so re-running a curtailed run re-spends on exactly those components.
- **Autosetup subprocess spend is invisible.** LLM calls made inside the autosetup
  subprocess do not flow through the meter and count toward no cap.
- **Sub-agents accrue to their parent's phase.** Judges, researchers, and other
  spawned helpers spend from whatever named scope their root agent runs under; there
  is no separate accounting knob for them.
- **The report step draws on the pool only.** It has no named cap; a run that arrives
  at the report phase with an exhausted pool will feel pressure there too.

## 5. Calibration and cost telemetry

To size budgets from real runs, use `composer/scripts/budget_math.py`:

```
uv run composer/scripts/budget_math.py <dump.json[.gz] | run-id> [--uid U]
    [--cap-fraction 0.75] [--wrapup-turns 4] [--emit-matrix DIR]
```

It replays a run trail (an `ap-trail export` dump, or a bare run id fetched live from
the store) and reconstructs, per LLM call, exactly what the cost meter would have
accrued — at both cache-write TTLs, since the trail doesn't record which TTL a
conversation used. Calibrate on the 1h bound (the authors run with the long cache).
Sub-agent threads fold into their root thread, matching how budget scopes accrue.

`--emit-matrix DIR` writes a ready-made live-test budget matrix (a control budget
plus budgets that trip the formalization cap, the preparation cap, and the shared
pool) with a `manifest.md` of expected outcomes per test.

Phase attribution rides on **cost-center telemetry**: every logged thread records the
named budget scope it ran under (`ThreadMeta.cost_center`), stamped unconditionally —
budget or no budget — so phase attribution works on unbudgeted runs too. `null` means
pool-level or pre-pipeline work; trails recorded before cost-center tracking existed
lack the field and classify as unattributed.

Note the distinction from the existing token accounting: `ap_report/job_info.json`
and `token_usage.json` report token *counts* by model and by workflow phase; the
budget meters *USD* via the pricing table. They are related but not the same numbers.
