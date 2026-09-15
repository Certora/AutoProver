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

```text
 PipelineBackend ──preflight──▶ Pre ──prepare_system──▶ PreparedSystem ──prepare_formalization──▶ Formalizer
 (config, analysis spec,       (backend    (.main: located main                     (formalize / verdicts /
  artifact store)               pre-work)   contract; backend setup)                  report inputs / finalize)
```

The driver (`run_pipeline` in [core.py](../composer/pipeline/core.py)) sequences them:

```python
# 1. analysis-independent backend pre-work runs CONCURRENTLY with the shared analysis
try:
    async with asyncio.TaskGroup() as overlap:        # the gate: either failure cancels the other
        preflight_task = overlap.create_task(backend.preflight(run))
        analysis_task = overlap.create_task(run.runner(TaskInfo(SYSTEM_ANALYSIS_TASK_ID, ...), ...))
except BaseExceptionGroup as eg:
    if len(eg.exceptions) == 1:                       # grouping is the group's doing, not a contract
        raise eg.exceptions[0] from None
    raise
preflight, analyzed = preflight_task.result(), analysis_task.result()

# 2. backend transform: prover lifts to a harnessed app; foundry is identity
prepared = await backend.prepare_system(analyzed, run, preflight)

# 3. pre-formalization setup runs CONCURRENTLY with property extraction
staged_task = asyncio.create_task(prepared.prepare_formalization(run))
batches = await _extract_all(prepared.main, ...)
staged = await staged_task                    # only overlapped; neither side is cancelled

# 4. a backend whose units share one artifact handed back a StagedFormalizer instead: the
#    artifact is authored HERE, once, from every unit's properties (§3.3)
formalizer = await staged.begin(batches, run) if isinstance(staged, StagedFormalizer) else staged

# 5. per-component formalization (parallel), cache-wrapped by the driver
settled = await asyncio.gather(*[_run(b) for b in batches], return_exceptions=True)
await formalizer.finalize(outcomes, run)

# 6. shared: build + persist the report from the outcomes + backend verdicts
report = await build_report(..., fetch_verdicts=formalizer.fetch_verdicts)
```

The key structural point is the two overlaps, and both fall out of the driver generically. For the
CVL backend, launching `prepare_formalization` before awaiting extraction is what overlaps the slow
AutoSetup / summary work with per-component property inference — so Foundry
gets the same overlap with zero extra code.

`preflight` is the earlier peer, for pre-work that needs *nothing at all* from the run: Crucible
builds the program under test and gates a skeleton harness through the real toolchain there, since
neither reads the analyzed model. The two share a task group, so whichever side fails first cancels
the one still running: an agent would otherwise keep spending on a run that can no longer complete,
and a workspace build — the run's slowest non-LLM step — would keep running for a result nothing will
read. That is what makes the preflight a *gate*: a broken workspace stops the run while it has spent
at most one partial analysis agent, instead
of surfacing as unfixable compiler errors in the first authored draft, after the whole extraction
phase ([rust-applications.md §4.2](./rust-applications.md)).

---

## 3. The contract

Three protocols + three abstract bases define the entire seam. The `PipelineBackend` protocol and
the abstract bases (`Formalizer`, `StagedFormalizer`, `PreparedSystem`) live in
[composer/pipeline/core.py](../composer/pipeline/core.py); the data types the driver moves around
(`BackendResult`, `GaveUp`, `Delivered`, `BackendJob`, `ComponentOutcome`, `CorePhases`,
`PipelineRun`, `SystemAnalysisSpec`, `CorePipelineResult`) live in its sibling
[ptypes.py](../composer/pipeline/ptypes.py), and the persistence protocols in
[composer/spec/types.py](../composer/spec/types.py).

### 3.1 The result type: `FormT`

Everything is generic over one type variable, the *backend result*. It is the
intersection of two narrow protocols:

```python
# composer/pipeline/ptypes.py
class BackendResult(FormalResult, ReportableResult, Protocol): ...
```

- **`FormalResult`** ([types.py](../composer/spec/types.py)) — what *persistence*
  needs: `property_checks()`, `commentary`, `artifact_text`.
- **`ReportableResult`** ([report/collect.py](../composer/spec/source/report/collect.py))
  — what the *report* needs: `skipped`, `property_checks()`, `output_link`.

A backend's concrete result (for CVL, `GeneratedCVL`) structurally satisfies both. The
driver only ever holds it as an opaque `FormT`; it never reads a field.

### 3.2 `Formalizer[FormT]` — the heart of the abstraction

```python
# composer/pipeline/core.py
@dataclass
class Formalizer[FormT: BackendResult, U: FeatureUnit](ABC):
    formalized_type: type[FormT]      # the concrete result class — the cache get/put key
    backend_tag: ReportBackend        # the report vocabulary, stamped into the report

    @abstractmethod
    async def formalize(self, label, feat: U, props, ctx, run) -> FormT | GaveUp: ...

    @abstractmethod
    async def fetch_verdicts(self, inp: ReportComponentInput[FormT]) -> dict[RuleName, Verdict]: ...

        return []                     # synthetic report rows; default none

    async def finalize(self, outcomes, run) -> None:
        return None                   # run-level artifacts from the full outcome set; default none
```

The contract is deliberately small — one required producer (`formalize`), one required
reader (`fetch_verdicts`), and two optional hooks. The second type parameter `U` is the
*formalized unit* the backend consumes (EVM's `ContractComponentInstance`, a Rust backend's
`FeatureUnit`): the backend reads its concrete unit's members without casts while the driver stays
unit-agnostic. Crucially, a `Formalizer` is **immutable and fully constructed** by whatever produced
it: it carries its prover config, resources, and tool as constructor state, never set post-hoc. By
the time `formalize` runs, every dependency is already present.

### 3.3 `PreparedSystem` — the formalizer factory, and `StagedFormalizer`

```python
# composer/pipeline/core.py
@dataclass
class PreparedSystem[FormT: BackendResult, U: FeatureUnit, Main](ABC):
    main: Main                                              # the ecosystem's located "main"
    @abstractmethod
    async def prepare_formalization(
        self, run
    ) -> Formalizer[FormT, U] | StagedFormalizer[FormT, U]: ...
```

`main` is the *ecosystem's* main type (EVM's `ContractInstance`, Solana's
`SolanaProgramInstance`), a different axis from `U` — the driver treats it opaquely and only hands
it to `ecosystem.units(main)`.

The union return is for a backend whose units all build on **one shared artifact** (Crucible's
fixture; whatever setup module a CVLR backend needs). Such an artifact must be authored from the
union of every unit's properties, which pins it to exactly one point in the run — and
`prepare_formalization` is not it, because that overlaps extraction, so no properties exist there
yet. Nor can it be lazy on first `formalize`: whichever unit won the race would decide the artifact
every other unit is then told to work within — harmless at one unit, silently wrong at several. So
such a backend returns a `StagedFormalizer` instead, and the driver calls its one method between
extraction and the fan-out:

```python
class StagedFormalizer[FormT: BackendResult, U: FeatureUnit](ABC):
    @abstractmethod
    async def begin(self, jobs: Sequence[BackendJob[U]], run) -> Formalizer[FormT, U]: ...
```

Which of the two a backend returns is its own declared signature, so a backend with no shared
artifact never mentions staging at all. The prover is one of those: its shared peer
The prover is not one of those backends: its units share only AutoSetup's config and summaries,
which are not authored from anyone's properties. The one `StagedFormalizer` in the tree is
Rust/Crucible's.

### 3.4 The outcome types the driver produces

```python
# composer/pipeline/ptypes.py
@dataclass(frozen=True)
class Delivered[FormT: BackendResult]:   # a success + the path it was written to
    result: FormT
    deliverable: Path
    # unit_file (the deliverable's basename — the verdict-disambiguation key) and
    # run_link (the result's output_link) are derived properties.

class GaveUp(BaseModel):          # the single unified give-up signal
    reason: str

@dataclass
class ComponentOutcome[FormT: BackendResult, U: FeatureUnit](BackendJob[U]):
    result: Delivered[FormT] | GaveUp | BaseException   # success / declined / crashed
```

`ComponentOutcome` is a closed sum of the three things that can happen to one component:
it was `Delivered`, the agent `GaveUp` with a reason, or it raised. The driver's `_tally`
folds these into the `CorePipelineResult`; the report phase renders each.

---

## 4. The CVL backend, method by method

The prover implementation lives in
[composer/spec/source/pipeline.py](../composer/spec/source/pipeline.py). It declares:

```python
@dataclass
class ProverBackend(PipelineBackend[
    AutoProvePhase, GeneratedCVL, None, SpecIdentity, ContractComponentInstance,
    ContractInstance, SourceApplication, None,
]):
    backend_guidance = CERTORA_BACKEND_GUIDANCE
    core_phases = CorePhases({"analysis": ..., "extraction": ...,
                              "formalization": AutoProvePhase.CVL_GEN, "report": AutoProvePhase.REPORT})
    analysis_spec = SystemAnalysisSpec(COMMON_SYSTEM_CACHE_KEY, AP_PROPERTIES_KEY_NAME)

    _store: ProverArtifactStore
    _prover_opts: ProverOptions

    @property
    @override
    def artifact_store(self) -> ProverArtifactStore: return self._store
```

So `FormT = GeneratedCVL`, the artifact id type `A = SpecIdentity`, and the phase enum is
`AutoProvePhase`. The seam is structural, so naming `PipelineBackend` as a base is optional — but
that is the one place those eight arguments can be written down and tied to each other, and it
moves the conformance check from wherever the backend reaches `run_pipeline` to where it is
*defined*.

Three of the four non-method members are run-constants the driver only reads, so they are stated as
plain class attributes. The store is the exception, and is declared **read-only** on the protocol so
this backend can narrow it: the driver needs only `ArtifactStore[SpecIdentity, GeneratedCVL]`, while
`ProverPrepared` needs the `ProverArtifactStore` this returns (for `write_component_runs`), and a
mutable attribute is invariant — a narrowed field would not typecheck. A backend with nothing to
narrow returns its store the same way; `RustBackend` derives its guidance and analysis spec from the
wheel's descriptor in `__post_init__` rather than by accessor, for the same reason.

### 4.1 `prepare_system` — harness lift

```python
async def prepare_system(
    self, analyzed: SourceApplication, run, preflight: None,
) -> PreparedSystem[GeneratedCVL, ContractComponentInstance, ContractInstance]:
    sys_desc  = await run.runner(TaskInfo(HARNESS_TASK_ID, ...), lambda: run_harness_creation(...))
    harnessed = _lift_harnessed(analyzed, sys_desc)             # SourceApplication → HarnessedApplication
    prover_tool = get_prover_tool(run.env.llm_heavy(), run.source.contract_name,
                                  run.source.project_root, prover_opts=self._prover_opts)
    return ProverPrepared(main_instance(harnessed, run.source), self._store,
                          sys_desc, harnessed, prover_tool, self._prover_opts, analyzed)
```

It runs the harness-classification agent, folds the generated harnesses back into the app
as a `HarnessedApplication` (`_lift_harnessed` in [pipeline.py](../composer/spec/source/pipeline.py)), builds
the shared `verify_spec` prover tool once, and packages everything the next phase needs into
an immutable `ProverPrepared`. (Foundry's `prepare_system` is an identity transform — no
harness, no tool.)

### 4.2 `prepare_formalization` — the pre-formalization setup

This is where the CVL backend does its pre-work. From `ProverPrepared` in
[pipeline.py](../composer/spec/source/pipeline.py):

```python
async def prepare_formalization(self, run) -> Formalizer[GeneratedCVL, ContractComponentInstance]:
    setup_config = await run.runner(TaskInfo(AUTOSETUP_TASK_ID, ...),
        lambda: run_autosetup_phase(run.ctx, run.source, self._sys_desc, self._analyzed, ...))
    resources = [CVLResource(path=certora_relative_to_project(setup_config.summaries_path),
        required=True, description="AutoSetup-generated summaries", sort="import")]

    # The custom summaries build on AutoSetup's config, so they follow it rather than
    # running alongside it.
    if self._sys_desc.erc20_contracts or self._sys_desc.external_interfaces:
        resources.append(await run.runner(TaskInfo(SUMMARIES_TASK_ID, ...),
            lambda: setup_summaries(ctx=run.ctx, app=self._harnessed, ...)))

    return ProverRunner(GeneratedCVL, "prover", self._prover_tool,
                        setup_config.prover_config, resources, make_prover_fetcher(), self._deps)
```

Two things worth calling out:

- **No authoring agent runs here.** This method is joined at a barrier before any component is
  formalized, so everything in it delays every property in the run. It used to also formulate
  structural invariants and prove them into a shared `invariants.spec` — a full
  `batch_cvl_generation` with real prover jobs ahead of the barrier, which on a real contract
  was hours. An invariant is now authored by the component that needs one, in that component's
  own spec, and proven in the same `verify_spec` run as the rule citing it. The barrier itself
  stays: `batch_cvl_generation` is constructed with AutoSetup's `prover_config`.
- **The returned `ProverRunner` is fully loaded.** Its config, resource set and prover tool are
  all constructor fields. `formalize` adds nothing — it only *reads* them.

### 4.3 `formalize` — per-component authoring + verification loop

This is one instance of the **authoring session** (§4.3.1), which every backend runs.

The `Formalizer.formalize` impl is a thin adapter; all the work is in
`batch_cvl_generation`:

```python
# ProverRunner.formalize  (pipeline.py)
async def formalize(self, label, feat, props, ctx, run) -> GeneratedCVL | GaveUp:
    return await batch_cvl_generation(
        ctx.abstract(CVLGeneration), self._prover_config, props, feat,
        self._resources, self._prover_tool, run.env, label, run.source, SPECS_DIR)
```

`batch_cvl_generation` ([author.py](../composer/spec/source/author.py)) builds a
dedicated LLM agent graph and runs it to a fixpoint. The agent is given:

- the property batch + component context, rendered into the prompt;
- the resource set as `import` views, with paths made relative to the spec dir so the
  prover resolves CVL `import`s correctly;
- a tool belt: CVL authoring tools, the `verify_spec` prover tool, config-edit tools, and the
  completion/give-up/expectation tools (`PublishResultTool`, `GiveUpTool`,
  `ExpectRuleFailure`/`ExpectRulePassage`).

Two **hard validation gates** must both pass before the agent may publish
([author.py](../composer/spec/source/author.py)):

```python
required_validations=[FEEDBACK_VALIDATION_KEY, PROVER_VALIDATION_KEY]
```

- the **prover gate** — the spec must actually run;
- the **feedback gate** — a separate `property_feedback_judge` agent
  ([composer/spec/feedback.py](../composer/spec/feedback.py)) adjudicates whether each
  property is genuinely covered, and the author may file evidence-backed `Rebuttal`s
  (typecheck failure / counterexample / manual citation / reasoned) against prior feedback.

The agent loop ends in exactly one of two states, mapped onto the abstraction's
`FormT | GaveUp`:

```python
if res_state["failed"]:
    return GaveUp(reason=res_state["result"])
return GeneratedCVL(commentary=..., cvl=..., skipped=..., property_rules=...,
                    config=res_state["config"], final_link=res_state.get("prover_link"))
```

Note `config` and `final_link` are captured into the result. That is deliberate: a later
cache hit skips the prover entirely, so the result must carry enough to rebuild
`certora/confs/` and keep the run link without re-running anything.

### 4.3.1 The authoring session, shared by every backend

Every backend authors the same way, and that shape lives in
[composer/authoring/](../composer/authoring/). It is **one stateful agent**, not a retry loop
around a stateless one — the difference is where the state lives, and everything else follows from
it:

- **One `curr_spec` buffer.** `put_spec` replaces it; an edit tool replaces one exact span
  ([composer/core/edit.py](../composer/core/edit.py)) and is what the agent should reach for once a
  draft exists. A backend that can reject a malformed spec cheaply supplies a validator, and a
  rejected write leaves the buffer untouched.
- **Gates stamp, they do not return.** A checker or judge that accepts the draft writes
  `spec_digest(buffer, skips)` into `validations`. `check_completion` then requires every
  `required_validations` key to carry a stamp equal to the digest *as it now stands*, so editing
  after a green run invalidates that run without anything having to remember to clear it. The digest
  covers the skip declarations too, because "this property is left out, here is why" is part of what
  was accepted.
- **A judge that must state a verdict.** `build_feedback_judge` compiles a sub-agent with the
  session's tool belt, a rough-draft scratchpad, the run memory, and an enforced read-back of the
  draft (`did_read` — reviewing the copy in its own prompt is not reviewing what was written). It
  returns a structured `PropertyFeedback`, so there is no unparseable reply to interpret. The author
  may answer a prior round with an evidence-typed `Rebuttal` rather than re-arguing it.
- **Two honest exits.** `record_skip` excuses a property from the publish-time mapping with a
  justification; `give_up` ends the session with a reason that reaches the report. Both are better
  outcomes than a spec that only looks checked.
- **A publish gate that checks the mapping.** `validate_check_mapping` requires every non-skipped
  property to name at least one check, refuses a skipped one, and — when the backend's checker
  reports what it ran (forge names every test; the prover names nothing) — checks both directions
  against that ground truth.

What a backend supplies is the part that genuinely differs: the put-time validator, the gate tools
and the keys they stamp, the mapping's ground truth, the prompts, and its **own vocabulary**. That
last one is deliberate: `MappingVocab` carries the word each backend uses with its own model — CVL
says *rule*, Foundry says *test*, a Rust wheel declares its own via `check_noun` — because an author
writes better in the language its generated code already uses. The framework's generic term for the
concept is a **check**: the backend's named, runnable verification of one property, which yields a
`Verdict`.

One holdout, deliberate: the **report** still says *rule* (`RuleName`, `total_rules`,
`FormalizedProperty.rules`). Those are field names in `certora/ap_report/report.json`, which the
standalone renderer reads back, so renaming them changes an output format rather than an internal
name. `type RuleName = CheckName` marks the seam, and `fetch_verdicts` is the one place the two
vocabularies meet.

The per-backend assembly (which tools, which prompts, which cache) stays in that backend's own entry
point — `batch_cvl_generation`, `batch_foundry_test_generation`,
[`run_session`](../composer/rustapp/session.py). They differ in exactly the parameters the core takes,
so collapsing them into one function would buy nothing.

### 4.5 `fetch_verdicts` — pass/fail per rule

```python
async def fetch_verdicts(self, inp) -> dict[RuleName, Verdict]:
    return await self._fetch(inp)     # make_prover_fetcher(): queries ProverOutputUtility off-thread
```

The fetcher resolves each spec's prover run (via `inp.formalized.run_link`) and rolls per-rule
outcomes into `Verdict`s. The `collect` step
([report/collect.py](../composer/spec/source/report/collect.py)) then keys rules by
`(unit_file, name)` so one definition seen through several runs collapses
to one entry, and uses `Verdict.merge` (priority `BAD > ERROR > TIMEOUT > UNKNOWN > GOOD`) to
roll up multiple results for one rule. Foundry's fetcher instead reads pass/fail straight off
the result with no run service — same protocol, different source.

### 4.6 `finalize` — run-level artifact

```python
async def finalize(self, outcomes, run) -> None:
    runs = {ComponentSpec(o.feat.slugified_name).run_key: o.result.run_link
            for o in outcomes if isinstance(o.result, Delivered) and o.result.run_link}
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

    def property_checks(self) -> list[tuple[str, list[str]]]:       # FormalResult + ReportableResult
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
| **Artifact store** | `artifact_text`, `commentary`, `property_checks()` | `ArtifactStore.write_artifact` |
| **Report** | `skipped`, `property_checks()`, `output_link` | `ReportableResult` |
| **Run-link map** | the persisted `final_link` | `finalize` |

---

## 6. Persistence: the artifact store

`Delivered` pairs a result with the path it was written to — and those two always travel
together because the path *exists only because the result did*
([ptypes.py](../composer/pipeline/ptypes.py)). The write happens in the driver's `_run`
closure:

```python
backend.artifact_store.write_properties(result_key, batch.props)   # before generation
...
Delivered(result, backend.artifact_store.write_artifact(result_key, result))   # after success
```

The store is generic over `(ArtifactIdentifier, FormalResult)`
([artifacts.py](../composer/spec/artifacts.py)). The base writes everything that is
identical across backends — `properties.json`, `commentary.md`, the
`{property title → the checks demonstrating it}` map — keyed off the identifier's `stem`. The CVL
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
```

`ProverBackend.to_artifact_id(component)` maps a component instance to its `ComponentSpec`;
the driver uses it both to write properties before generation and to write the artifact after.

The resulting on-disk layout (all under the project's `certora/`):

```
certora/specs/autospec_<slug>.spec      # per-component CVL
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
`formalizer.formalized_type` (the `_run` closure in [core.py](../composer/pipeline/core.py)):

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
prover.

---

## 8. Failure handling

The abstraction encodes three distinct failure modes, each handled differently:

| Failure | Representation | Driver behavior |
|---|---|---|
| Agent declines a component | `formalize` returns `GaveUp(reason)` | recorded as a `ComponentOutcome`, surfaced in `failures`, rendered in report as a gap; **not cached** |
| Component crashes | `formalize` raises | `asyncio.gather(..., return_exceptions=True)` captures it into `ComponentOutcome.result` |
| Report build fails | exception in `build_report` | best-effort: logged, run still succeeds — unless `report_build.RERAISE_REPORT_FAILURES` is set, which tests flip to make a silent report failure fail loudly |

A component giving up is a normal, reportable outcome. The one remaining way
`prepare_formalization` kills a run is AutoSetup itself failing — without a `prover_config`
there is nothing for any component to verify against.

---

## 9. Extending: what a new backend must provide

To add a backend you implement `PipelineBackend` and the three phase objects — nothing in the
driver changes. The Foundry backend
([composer/foundry/pipeline.py](../composer/foundry/pipeline.py)) is the proof: it reuses
system analysis, property extraction, caching, and the report, and contributes only:

| Abstraction member | CVL backend | Foundry backend |
|---|---|---|
| `FormT` | `GeneratedCVL` | `GeneratedFoundryTest` |
| `preflight` | none (its pre-work needs the harnessed model) | none (`forge` builds the project already) |
| `prepare_system` | harness lift + prover tool | identity |
| `prepare_formalization` | AutoSetup, then custom summaries | trivial (pre-built formalizer) |
| shared artifact (`StagedFormalizer`) | none — units share only AutoSetup's config and summaries | none |
| `formalize` | authoring session, gated by `verify_spec` | authoring session, gated by `forge_test` |
| `fetch_verdicts` | query prover output off-thread | read ran/expected tests off the result |
| `finalize` | `components_to_prover_runs.json` | none |
| artifact bundle | `.spec` + `.conf` | `.t.sol` + metadata |

A backend author's checklist:

1. Define a result type satisfying `FormalResult` + `ReportableResult` (`artifact_text`,
   `commentary`, `property_checks()`, `skipped`, `output_link`).
2. Subclass `ArtifactStore` for the on-disk bundle; define an `ArtifactIdentifier` sum type.
3. Implement `PipelineBackend` (`preflight`, `prepare_system`, `to_artifact_id`,
   `backend_guidance`, `core_phases`, `analysis_spec`, `artifact_store`) — naming it as a base and
   filling in its eight type arguments is what the in-tree backends do. `preflight` returns `None`
   unless the backend has pre-work that needs nothing from the run — if it must *build* something,
   that is where the build belongs, so it overlaps system analysis and can fail the run before the
   model has been spent (Crucible: [rust-applications.md §4.2](./rust-applications.md)).
4. Implement `PreparedSystem.prepare_formalization` returning a fully-constructed
   `Formalizer` — or, if every unit builds on one shared artifact, a `StagedFormalizer` whose
   `begin` authors it from the union of all units' properties (§3.3).
5. Implement `Formalizer.formalize` + `fetch_verdicts`; override `finalize` only if needed. `formalize` should assemble the shared authoring session (§4.3.1)
   rather than grow its own loop — what it supplies is the gate tools, the prompts, and its own
   noun for a check.

---

## 10. End-to-end trace (CVL backend)

Putting it together, one run of the CVL backend over a component:

```
_entry_point → cli_pipeline → cont(env, ProverBackend, EVM)
└─ run_pipeline(ProverBackend, run, ecosystem=EVM)
   1. ┌ create_task: ProverBackend.preflight ─▶ None   # a building backend puts its build here
      └ run_component_analysis ─────────────▶ SourceApplication   # concurrent with the above
   2. ProverBackend.prepare_system
        run_harness_creation ─▶ SystemDescriptionHarnessed
        _lift_harnessed       ─▶ HarnessedApplication
        get_prover_tool       ─▶ verify_spec tool
                                 ▶ ProverPrepared(main=located main contract, ...)
   3. ┌ create_task: ProverPrepared.prepare_formalization
      │    run_autosetup_phase → (config, [summaries resource])
      │    setup_summaries (only when the system has erc20s/external interfaces)
      │                                         ▶ ProverRunner(config, resources, fetch)
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
   5. build_report( per-component inputs,
                    fetch_verdicts=ProverRunner.fetch_verdicts ) ─▶ certora/ap_report/report.json
```

---

## 11. Key files

| Concern | File |
|---|---|
| Driver + the seam (`PipelineBackend`, `Formalizer`, `StagedFormalizer`, `PreparedSystem`) | [composer/pipeline/core.py](../composer/pipeline/core.py) |
| The driver's data types (`BackendResult`, `Delivered`, `GaveUp`, `ComponentOutcome`, `PipelineRun`, …) | [composer/pipeline/ptypes.py](../composer/pipeline/ptypes.py) |
| Result protocols (`FormalResult`, `ArtifactIdentifier`) | [composer/spec/types.py](../composer/spec/types.py) |
| `ReportableResult`, `Verdict`, `VerdictFetcher`, `collect` | [composer/spec/source/report/collect.py](../composer/spec/source/report/collect.py) |
| CVL backend (the three phase objects) | [composer/spec/source/pipeline.py](../composer/spec/source/pipeline.py) |
| The shared authoring session (buffer, stamps, judge, publish gate) | [composer/authoring/](../composer/authoring/) |
| CVL authoring agent (`batch_cvl_generation`) | [composer/spec/source/author.py](../composer/spec/source/author.py) |
| CVL result type (`GeneratedCVL`) | [composer/spec/cvl_generation.py](../composer/spec/cvl_generation.py) |
| Artifact store base / CVL subclass | [composer/spec/artifacts.py](../composer/spec/artifacts.py) · [composer/spec/source/artifacts.py](../composer/spec/source/artifacts.py) |
| Foundry backend (contrast) | [composer/foundry/pipeline.py](../composer/foundry/pipeline.py) |
| A Rust-wheel backend on this seam | [docs/rust-applications.md](./rust-applications.md) |
