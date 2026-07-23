# Design Doc — The Formalization Abstraction

> Detailed design of how AutoProver turns *extracted properties* into *verified
> artifacts*, the abstraction that makes it backend-agnostic, and a concrete
> walk-through of the CVL (Certora Prover) implementation.
>
> Companion to [ARCHITECTURE.md](../ARCHITECTURE.md). Where that document maps the
> whole system, this one zooms into a single seam: the contract between the generic
> pipeline driver and a verification backend.

---

## 1. Problem & motivation

The pipeline has two kinds of work:

- **Shared work** that is identical no matter what you generate — analyze the system
  into components, infer a list of properties per component, cache expensive results,
  and assemble a final report.
- **Backend-specific work** — *how* a property becomes a checkable artifact, and *how*
  that artifact's pass/fail verdict is obtained. For the CVL backend this is "author a
  `.spec`, run the Certora Prover, revise on counterexamples." For the Foundry backend
  it is "write a `.t.sol`, run `forge test`."

The **formalization abstraction** is the seam between the two. It lets the driver in
[composer/pipeline/core.py](../composer/pipeline/core.py) own all the shared work while
delegating every backend-specific decision through a small, typed protocol. The CVL and
Foundry backends are two implementations of that protocol; the driver never imports
either.

### Design goals

1. **The driver inspects nothing backend-specific.** It moves opaque `FormT` values
   around; only the backend ever looks inside them.
2. **No half-initialized state.** Each phase yields an immutable object that is the
   constructor input to the next, so ordering is enforced by the type system, not by
   call-order discipline.
3. **One result type threads everything.** A single generic parameter `FormT` keys the
   cache, the artifact store, the verdict fetcher, and the report — so they cannot drift
   out of agreement.
4. **Concurrency is structural.** Pre-formalization setup overlaps property extraction;
   per-component formalization fans out — all expressed in the driver, inherited by every
   backend for free.

---

## 2. The phase chain

Formalization is the tail of a three-link immutable chain. Each arrow is a method whose
return type is the input to the next link:

```
 PipelineBackend ──prepare_system──▶ PreparedSystem ──prepare_formalization──▶ Formalizer
 (config, analysis spec,            (.main: located main                      (formalize / verdicts /
  artifact store)                    contract; backend setup)                  report inputs / finalize)
```

The driver ([core.py:230](../composer/pipeline/core.py)) sequences them:

```python
# 1. shared: analyze source → SourceApplication (always the same type)
analyzed = await run.runner(TaskInfo(SYSTEM_ANALYSIS_TASK_ID, ...), lambda: run_component_analysis(...))

# 2. backend transform: prover lifts to a harnessed app; foundry is identity
prepared = await backend.prepare_system(analyzed, run)

# 3. pre-formalization setup runs CONCURRENTLY with property extraction
formalizer_task = asyncio.create_task(prepared.prepare_formalization(run))
batches = await _extract_all(prepared.main, backend.backend_guidance, run, ...)
formalizer = await formalizer_task

# 4. per-component formalization (parallel), cache-wrapped by the driver
settled = await asyncio.gather(*[_run(b) for b in batches], return_exceptions=True)

# 5. shared: build + persist the report from the outcomes + backend verdicts
report = await build_report(..., fetch_verdicts=formalizer.fetch_verdicts)
```

The key structural point: `prepare_formalization` is launched as a task *before*
extraction is awaited. For the CVL backend, that is what overlaps the slow AutoSetup /
summary / structural-invariant work with per-component property inference — and it falls
out of the driver generically, so Foundry gets the same overlap with zero extra code.

---

## 3. The contract

Three protocols + two abstract bases define the entire seam. All live in
[composer/pipeline/core.py](../composer/pipeline/core.py) and
[composer/spec/types.py](../composer/spec/types.py).

### 3.1 The result type: `FormT`

Everything is generic over one type variable, the *backend result*. It is the
intersection of two narrow protocols:

```python
# composer/pipeline/core.py
class BackendResult(FormalResult, ReportableResult, Protocol): ...
```

- **`FormalResult`** ([types.py:43](../composer/spec/types.py)) — what *persistence*
  needs: `property_units()`, `commentary`, `artifact_text`.
- **`ReportableResult`** ([report/collect.py:25](../composer/spec/source/report/collect.py))
  — what the *report* needs: `skipped`, `property_units()`, `output_link`.

A backend's concrete result (for CVL, `GeneratedCVL`) structurally satisfies both. The
driver only ever holds it as an opaque `FormT`; it never reads a field.

### 3.2 `Formalizer[FormT]` — the heart of the abstraction

```python
# composer/pipeline/core.py
@dataclass
class Formalizer[FormT: BackendResult](ABC):
    formalized_type: type[FormT]      # the concrete result class — the cache get/put key
    backend_tag: ReportBackend        # "prover" | "foundry", stamped into the report

    @abstractmethod
    async def formalize(self, label, feat, props, ctx, run) -> FormT | GaveUp: ...

    @abstractmethod
    async def fetch_verdicts(self, inp: ReportComponentInput[FormT]) -> dict[RuleName, Verdict]: ...

    def extra_report_inputs(self) -> list[ReportComponentInput[FormT]]:
        return []                     # synthetic report rows; default none

    async def finalize(self, outcomes, run) -> None:
        return None                   # run-level artifacts from the full outcome set; default none
```

The contract is deliberately small — one required producer (`formalize`), one required
reader (`fetch_verdicts`), and two optional hooks. Crucially, a `Formalizer` is
**immutable and fully constructed** by `prepare_formalization`: it carries its prover
config, resources, and tool as constructor state, never set post-hoc. By the time
`formalize` runs, every dependency is already present.

### 3.3 `PreparedSystem[FormT]` — the formalizer factory

```python
# composer/pipeline/core.py
@dataclass
class PreparedSystem[FormT: BackendResult](ABC):
    main: ContractInstance                                  # located main contract
    @abstractmethod
    async def prepare_formalization(self, run) -> Formalizer[FormT]: ...
```

### 3.4 The outcome types the driver produces

```python
@dataclass(frozen=True)
class Delivered[FormT]:           # a success + the path it was written to
    result: FormT
    deliverable: Path

class GaveUp(BaseModel):          # the single unified give-up signal
    reason: str

@dataclass
class ComponentOutcome[FormT](BackendJob):
    result: Delivered[FormT] | GaveUp | BaseException   # success / declined / crashed
```

`ComponentOutcome` is a closed sum of the three things that can happen to one component:
it was `Delivered`, the agent `GaveUp` with a reason, or it raised. The driver's `_tally`
([core.py:351](../composer/pipeline/core.py)) folds these into the run result; the report
phase renders each.

---

## 4. The CVL backend, method by method

The prover implementation lives in
[composer/spec/source/pipeline.py](../composer/spec/source/pipeline.py). It declares:

```python
@dataclass
class ProverBackend:   # PipelineBackend[AutoProvePhase, GeneratedCVL, None, ComponentSpec]
    backend_guidance = CERTORA_BACKEND_GUIDANCE
    core_phases = CorePhases({"analysis": ..., "extraction": ..., "formalization": AutoProvePhase.CVL_GEN})
    analysis_spec = SystemAnalysisSpec("source-analysis")
    artifact_store: ProverArtifactStore
    _prover_opts: ProverOptions
```

So `FormT = GeneratedCVL`, the artifact id type `A = ComponentSpec`, and the phase enum is
`AutoProvePhase`.

### 4.1 `prepare_system` — harness lift

```python
async def prepare_system(self, analyzed: SourceApplication, run) -> PreparedSystem[GeneratedCVL]:
    sys_desc  = await run.runner(TaskInfo(HARNESS_TASK_ID, ...), lambda: run_harness_creation(...))
    harnessed = _lift_harnessed(analyzed, sys_desc)             # SourceApplication → HarnessedApplication
    prover_tool = get_prover_tool(run.env.llm_heavy(), run.source.contract_name,
                                  run.source.project_root, prover_opts=self._prover_opts)
    return ProverPrepared(main_instance(harnessed, run.source), self.artifact_store,
                          sys_desc, harnessed, prover_tool, self._prover_opts, analyzed)
```

It runs the harness-classification agent, folds the generated harnesses back into the app
as a `HarnessedApplication` ([pipeline.py:69](../composer/spec/source/pipeline.py)), builds
the shared `verify_spec` prover tool once, and packages everything the next phase needs into
an immutable `ProverPrepared`. (Foundry's `prepare_system` is an identity transform — no
harness, no tool.)

### 4.2 `prepare_formalization` — the concurrent setup fan-out

This is where the CVL backend does its expensive pre-work, and it is the richest method in
the abstraction. From [pipeline.py:178](../composer/spec/source/pipeline.py):

```python
async def prepare_formalization(self, run) -> Formalizer[GeneratedCVL]:
    # AutoSetup (+ custom summaries) ∥ structural-invariant formulation — both depend only
    # on the harnessed app, so they run concurrently.
    (setup_config, resources), invariants = await asyncio.gather(
        self._autosetup(run), self._invariants(run),
    )

    invariant = None
    if invariants.inv:
        inv_props = [PropertyFormulation(title=inv.name, description=inv.description, sort="invariant")
                     for inv in invariants.inv]
        self._store.write_properties(InvariantSpec(), inv_props)

        # Generate invariants.spec ONCE, with cache short-circuit
        inv_cvl_ctx = run.ctx.child(INV_CVL_KEY)
        cached = await inv_cvl_ctx.cache_get(GeneratedCVL)
        if cached is not None:
            inv_cvl = cached
        else:
            inv_result = await run.runner(TaskInfo(INVARIANT_CVL_TASK_ID, ...),
                lambda: batch_cvl_generation(inv_cvl_ctx.abstract(CVLGeneration),
                    setup_config.prover_config, inv_props, None, resources, self._prover_tool, ...))
            if isinstance(inv_result, GaveUp):
                raise RuntimeError(f"Structural invariant CVL generation gave up: {inv_result.reason}")
            inv_cvl = inv_result
            await inv_cvl_ctx.cache_put(inv_cvl)

        inv_path = self._store.write_artifact(InvariantSpec(), inv_cvl)
        # Append invariants.spec to the resource set so EVERY per-component spec imports it.
        resources = [*resources, CVLResource(path=inv_path, required=False,
            description="Structural invariants that may be assumed as preconditions", sort="import")]
        invariant = (inv_props, Delivered(inv_cvl, inv_path))

    return ProverRunner(GeneratedCVL, "prover", self._store, self._prover_tool,
                        setup_config.prover_config, resources, invariant, make_prover_fetcher())
```

Three things worth calling out:

- **Concurrency inside the method.** AutoSetup+summaries and invariant *formulation* are
  independent, so they `gather`. This nests under the driver-level overlap (this whole
  method already runs concurrently with property extraction).
- **Structural invariants are formalized eagerly, here, not per-component.** They are
  generated once into `invariants.spec`, then injected into `resources` so every later
  per-component spec can `import` them as preconditions. The invariant CVL goes through the
  exact same `batch_cvl_generation` path that components do (with `component=None`).
- **The returned `ProverRunner` is fully loaded.** Its config, resource set (now including
  `invariants.spec`), prover tool, and the in-memory invariant result are all constructor
  fields. `formalize` adds nothing — it only *reads* them.

### 4.3 `formalize` — per-component authoring + verification loop

The `Formalizer.formalize` impl is a thin adapter; all the work is in
`batch_cvl_generation`:

```python
# ProverRunner.formalize  (pipeline.py:114)
async def formalize(self, label, feat, props, ctx, run) -> GeneratedCVL | GaveUp:
    return await batch_cvl_generation(
        ctx.abstract(CVLGeneration), self._prover_config, props, feat,
        self._resources, self._prover_tool, run.env, label, run.source, SPECS_DIR)
```

`batch_cvl_generation` ([author.py:334](../composer/spec/source/author.py)) builds a
dedicated LLM agent graph and runs it to a fixpoint. The agent is given:

- the property batch + component context, rendered into the prompt;
- the resource set as `import` views, with paths made relative to `certora/specs/` so the
  prover resolves CVL `import`s correctly ([author.py:349](../composer/spec/source/author.py));
- a tool belt: CVL authoring tools, the `verify_spec` prover tool, config-edit tools, and the
  completion/give-up/expectation tools (`PublishResultTool`, `GiveUpTool`,
  `ExpectRuleFailure`/`ExpectRulePassage`).

Two **hard validation gates** must both pass before the agent may publish
([author.py:404](../composer/spec/source/author.py)):

```python
required_validations=[FEEDBACK_VALIDATION_KEY, PROVER_VALIDATION_KEY]
```

- the **prover gate** — the spec must actually run;
- the **feedback gate** — a separate `property_feedback_judge` agent
  ([composer/spec/feedback.py](../composer/spec/feedback.py)) adjudicates whether each
  property is genuinely covered, and the author may file evidence-backed `Rebuttal`s
  (typecheck failure / counterexample / manual citation / reasoned) against prior feedback.

The agent loop ends in exactly one of two states, mapped onto the abstraction's
`FormT | GaveUp` ([author.py:413](../composer/spec/source/author.py)):

```python
if res_state["failed"]:
    return GaveUp(reason=res_state["result"])
return GeneratedCVL(commentary=..., cvl=..., skipped=..., property_rules=...,
                    config=res_state["config"], final_link=res_state.get("prover_link"))
```

Note `config` and `final_link` are captured into the result. That is deliberate: a later
cache hit skips the prover entirely, so the result must carry enough to rebuild
`certora/confs/` and keep the run link without re-running anything.

### 4.4 `extra_report_inputs` — folding in the invariants

Per-component outcomes are assembled by the driver. The structural invariants are a
*synthetic* component the backend contributes ([pipeline.py:136](../composer/spec/source/pipeline.py)):

```python
def extra_report_inputs(self) -> list[ReportComponentInput[GeneratedCVL]]:
    if self._invariant is None:
        return []
    inv_props, inv = self._invariant
    return [ReportComponentInput(name="Structural Invariants", props=inv_props, formalized=inv)]
```

This is the report-side payoff of formalizing invariants in `prepare_formalization`: the
in-memory `Delivered[GeneratedCVL]` is replayed straight into the report with no special
casing in the driver.

### 4.5 `fetch_verdicts` — pass/fail per rule

```python
async def fetch_verdicts(self, inp) -> dict[RuleName, Verdict]:
    return await self._fetch(inp)     # make_prover_fetcher(): queries ProverOutputUtility off-thread
```

The fetcher resolves each spec's prover run (via `inp.formalized.run_link`) and rolls per-rule
outcomes into `Verdict`s. The `collect` step
([report/collect.py:104](../composer/spec/source/report/collect.py)) then keys rules by
`(unit_file, name)` so a structural invariant imported into several component specs collapses
to one entry, and uses `Verdict.merge` (priority `BAD > ERROR > TIMEOUT > UNKNOWN > GOOD`) to
roll up multiple results for one rule. Foundry's fetcher instead reads pass/fail straight off
the result with no run service — same protocol, different source.

### 4.6 `finalize` — run-level artifact

```python
async def finalize(self, outcomes, run) -> None:
    runs = {ComponentSpec(o.feat.slugified_name).run_key: o.result.run_link
            for o in outcomes if isinstance(o.result, Delivered) and o.result.run_link}
    if self._invariant and self._invariant[1].run_link:
        runs[InvariantSpec().run_key] = self._invariant[1].run_link
    self._store.write_component_runs(runs)   # → components_to_prover_runs.json
```

`finalize` is the one hook that sees the *entire* outcome set at once — used here to emit the
`{spec → prover-run link}` map. Foundry omits it (default no-op).

---

## 5. The result type as the central key

`GeneratedCVL` ([cvl_generation.py:125](../composer/spec/cvl_generation.py)) is the concrete
`FormT`. It satisfies `BackendResult` structurally — note nothing declares
`class GeneratedCVL(BackendResult)`; the protocols match by shape:

```python
class GeneratedCVL(BaseModel):
    commentary: str
    cvl: str
    skipped: list[SkippedProperty] = Field(default_factory=list)
    property_rules: list[PropertyRuleMapping] = Field(default_factory=list)
    config: dict | None = None
    final_link: str | None = None

    def property_units(self) -> list[tuple[str, list[str]]]:        # FormalResult + ReportableResult
        return [(m.property_title, m.rules) for m in self.property_rules]

    @property
    def artifact_text(self) -> str:                                # FormalResult: bytes to write
        return self.cvl

    @property
    def output_link(self) -> str | None:                           # ReportableResult: run link
        return _output_link(self.final_link)                       # /jobStatus/ → /output/
```

The same value is the key for four otherwise-independent subsystems, which is what keeps them
from disagreeing:

| Consumer | Uses | Via |
|---|---|---|
| **Cache** | the *type* `GeneratedCVL` | `formalizer.formalized_type` → `cache_get`/`cache_put` |
| **Artifact store** | `artifact_text`, `commentary`, `property_units()` | `ArtifactStore.write_artifact` |
| **Report** | `skipped`, `property_units()`, `output_link` | `ReportableResult` |
| **Run-link map** | the persisted `final_link` | `finalize` |

---

## 6. Persistence: the artifact store

`Delivered` pairs a result with the path it was written to — and those two always travel
together because the path *exists only because the result did*
([core.py:98](../composer/pipeline/core.py)). The write happens in the driver's `_run`
closure:

```python
backend.artifact_store.write_properties(result_key, batch.props)   # before generation
...
Delivered(result, backend.artifact_store.write_artifact(result_key, result))   # after success
```

The store is generic over `(ArtifactIdentifier, FormalResult)`
([artifacts.py:32](../composer/spec/artifacts.py)). The base writes everything that is
identical across backends — `properties.json`, `commentary.md`, the
`{property title → demonstrating units}` map — keyed off the identifier's `stem`. The CVL
subclass [ProverArtifactStore](../composer/spec/source/artifacts.py) adds the
CVL-specific bundle: it overrides `write_artifact` to also emit a `.conf` (base config +
fixed run overlay) alongside the `.spec`.

The artifact id is itself a small sum type, so naming conventions live in one place rather
than being interpolated at call sites:

```python
@dataclass(frozen=True)
class ComponentSpec:            # autospec_<slug>.spec
    slug: str
    @property
    def stem(self): return f"autospec_{self.slug}"
    @property
    def run_key(self): return self.slug

@dataclass(frozen=True)
class InvariantSpec:            # invariants.spec
    @property
    def stem(self): return "invariants"
```

`ProverBackend.to_artifact_id(component)` maps a component instance to its `ComponentSpec`;
the driver uses it both to write properties before generation and to write the artifact after.

The resulting on-disk layout (all under the project's `certora/`):

```
certora/specs/autospec_<slug>.spec      # per-component CVL
certora/specs/invariants.spec           # structural invariants (imported by the above)
certora/confs/<stem>.conf               # prover config per spec
certora/properties/<stem>.properties.json        # inferred properties
certora/properties/<stem>.property_rules.json    # property → [rule names]
certora/properties/<stem>.commentary.md          # author's commentary
certora/ap_report/report.json                    # final cross-referenced report
.certora_internal/autoProve/components_to_prover_runs.json   # finalize() output
```

---

## 7. Caching wraps formalization (driver-owned)

A backend never writes cache logic — the driver does, keyed by
`formalizer.formalized_type` ([core.py:271](../composer/pipeline/core.py)):

```python
async def _run(batch):
    result_key = backend.to_artifact_id(batch.feat)
    backend.artifact_store.write_properties(result_key, batch.props)
    child = await batch.feat_ctx.child(_batch_cache_key(batch.props), {...})
    cached = await child.cache_get(formalizer.formalized_type)     # ← type comes from the formalizer
    if cached is None:
        result = await run.runner(TaskInfo(formalize_task_id(...)),
                                  lambda: formalizer.formalize(label, batch.feat, batch.props, child, run))
        if not isinstance(result, GaveUp):
            await child.cache_put(result)
    else:
        result = cached
    ...
```

The cache key is the hash of the *property batch* (`_batch_cache_key`), under the component's
context, under the `properties` context — the hierarchical scheme described in
[ARCHITECTURE.md §7](../ARCHITECTURE.md). Because the result type carries `config` and
`final_link`, a cache hit can rebuild the `.conf` and keep the run link without touching the
prover. The structural-invariant CVL has its own cache short-circuit inside
`prepare_formalization` (`INV_CVL_KEY`, §4.2) for the same reason.

---

## 8. Failure handling

The abstraction encodes three distinct failure modes, each handled differently:

| Failure | Representation | Driver behavior |
|---|---|---|
| Agent declines a component | `formalize` returns `GaveUp(reason)` | recorded as a `ComponentOutcome`, surfaced in `failures`, rendered in report as a gap; **not cached** |
| Component crashes | `formalize` raises | `asyncio.gather(..., return_exceptions=True)` captures it into `ComponentOutcome.result` |
| Invariant CVL gives up | `prepare_formalization` raises `RuntimeError` | **fatal** — invariants are a shared precondition, so the whole run aborts |
| Report build fails | exception in `build_report` | best-effort: logged, run still succeeds ([core.py:318](../composer/pipeline/core.py)) |

The asymmetry is intentional: a single component giving up is a normal, reportable outcome,
but the shared invariant spec failing would silently weaken every downstream component, so it
fails loud.

---

## 9. Extending: what a new backend must provide

To add a backend you implement the protocol and the three phase objects — nothing in the
driver changes. The Foundry backend
([composer/foundry/pipeline.py](../composer/foundry/pipeline.py)) is the proof: it reuses
system analysis, property extraction, caching, and the report, and contributes only:

| Abstraction member | CVL backend | Foundry backend |
|---|---|---|
| `FormT` | `GeneratedCVL` | `GeneratedFoundryTest` |
| `prepare_system` | harness lift + prover tool | identity |
| `prepare_formalization` | AutoSetup ∥ summaries ∥ invariants | trivial (pre-built formalizer) |
| `formalize` | author CVL, run prover, revise | author tests, run `forge test` |
| `fetch_verdicts` | query prover output off-thread | read ran/expected tests off the result |
| `extra_report_inputs` | synthetic "Structural Invariants" | none |
| `finalize` | `components_to_prover_runs.json` | none |
| artifact bundle | `.spec` + `.conf` | `.t.sol` + metadata |

A backend author's checklist:

1. Define a result type satisfying `FormalResult` + `ReportableResult` (`artifact_text`,
   `commentary`, `property_units()`, `skipped`, `output_link`).
2. Subclass `ArtifactStore` for the on-disk bundle; define an `ArtifactIdentifier` sum type.
3. Implement `PipelineBackend` (`prepare_system`, `to_artifact_id`, `backend_guidance`,
   `core_phases`, `analysis_spec`, `artifact_store`).
4. Implement `PreparedSystem.prepare_formalization` returning a fully-constructed
   `Formalizer`.
5. Implement `Formalizer.formalize` + `fetch_verdicts`; override `extra_report_inputs` /
   `finalize` only if needed.

---

## 10. End-to-end trace (CVL backend)

Putting it together, one run of the CVL backend over a component:

```
run_autoprove_pipeline
└─ run_pipeline(ProverBackend, run)
   1. run_component_analysis ──────────────▶ SourceApplication
   2. ProverBackend.prepare_system
        run_harness_creation ─▶ SystemDescriptionHarnessed
        _lift_harnessed       ─▶ HarnessedApplication
        get_prover_tool       ─▶ verify_spec tool
                                 ▶ ProverPrepared(main=located main contract, ...)
   3. ┌ create_task: ProverPrepared.prepare_formalization
      │    gather( _autosetup → (config, [summaries]) , _invariants → [BaseInvariant] )
      │    batch_cvl_generation(component=None) ─▶ invariants.spec  (cached under INV_CVL_KEY)
      │    resources += invariants.spec
      │                                         ▶ ProverRunner(config, resources, invariant, fetch)
      └ _extract_all ─▶ [ _Batch(component, props) , ... ]     # runs concurrently with the above
   4. for each batch (parallel, semaphore-bounded):
        write_properties(ComponentSpec(slug), props)
        cache_get(GeneratedCVL)?  ── hit ─▶ reuse
                                  └ miss ─▶ ProverRunner.formalize
                                              batch_cvl_generation(component=feat)
                                                author CVL ⇄ verify_spec ⇄ feedback judge   (loop)
                                                gates: PROVER_VALIDATION + FEEDBACK_VALIDATION
                                              ─▶ GeneratedCVL | GaveUp
                                            cache_put(result)
        write_artifact ─▶ autospec_<slug>.spec (+ .conf)   ⇒ Delivered(result, path)
                                              ─▶ ComponentOutcome
      ProverRunner.finalize(outcomes) ─▶ components_to_prover_runs.json
   5. build_report( per-component inputs + extra_report_inputs(),
                    fetch_verdicts=ProverRunner.fetch_verdicts ) ─▶ certora/ap_report/report.json
```

---

## 11. Key files

| Concern | File |
|---|---|
| Driver + abstraction definitions | [composer/pipeline/core.py](../composer/pipeline/core.py) |
| Result protocols (`FormalResult`, `ArtifactIdentifier`) | [composer/spec/types.py](../composer/spec/types.py) |
| `ReportableResult`, `Verdict`, `VerdictFetcher`, `collect` | [composer/spec/source/report/collect.py](../composer/spec/source/report/collect.py) |
| CVL backend (the three phase objects) | [composer/spec/source/pipeline.py](../composer/spec/source/pipeline.py) |
| CVL authoring agent (`batch_cvl_generation`) | [composer/spec/source/author.py](../composer/spec/source/author.py) |
| CVL result type (`GeneratedCVL`) | [composer/spec/cvl_generation.py](../composer/spec/cvl_generation.py) |
| Artifact store base / CVL subclass | [composer/spec/artifacts.py](../composer/spec/artifacts.py) · [composer/spec/source/artifacts.py](../composer/spec/source/artifacts.py) |
| Foundry backend (contrast) | [composer/foundry/pipeline.py](../composer/foundry/pipeline.py) |
```
