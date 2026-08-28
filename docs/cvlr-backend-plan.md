# Plan — The CVLR Backend (Certora Solana Prover, then Sunbeam/Soroban)

> A **plan**, not a description of what exists. It proposes a new AutoProver verification
> backend that formalizes properties as **CVLR** rules (Rust) and verifies them with the
> Certora Solana Prover, structured so that Soroban/Sunbeam reuses it rather than
> becoming a second backend.
>
> Companions: [ecosystem-abstraction.md](./ecosystem-abstraction.md) (the front half, already
> built for both chains), [formalization-abstraction.md](./formalization-abstraction.md) (the
> seam this plugs into), [command-sandbox.md](./command-sandbox.md) (how the Rust builds are
> confined), [rag-import-format.md](./rag-import-format.md) (the corpus format).

---

## 1. What we are building

A backend that turns extracted properties into **CVLR rules** — Rust `#[rule]` functions using
the `cvlr` crate's `cvlr_assert!` / `cvlr_assume!` / `cvlr_satisfy!` / `nondet()` / `clog!`
primitives plus a chain helper crate (`cvlr-solana`, and the Soroban equivalent) — compiles
them against the program under verification, submits them to the Certora cloud prover, and
feeds verdicts and counterexamples back into an authoring loop.

This is exactly the role [composer/spec/source/](../composer/spec/source/) plays for CVL +
`certoraRun` on EVM, and the intent is to stay as close to that implementation as the domain
allows.

**Scope: one backend, two chains.** Solana ships first; Soroban follows by supplying only the
pieces that genuinely differ. The
two share CVLR itself, the authoring loop, the compile gate, the prover submission path, the
result plumbing, the munge machinery, and most of the knowledge corpus. They differ in the
build recipe, the conf shape, the prover CLI, the project scaffold, and chain-specific
guidance.

---

## 2. Starting position — the front half is already built

Both chains' *ecosystems* exist and are exercised by tests. This project is the **back half
only**.

```
ECOSYSTEM (built, both chains)                     BACKEND (this plan)
source ─analyze─▶ App model ─extract─▶ properties ─formalize─▶ CVLR ─verdicts─▶ report
```

| Piece | State | Where |
|---|---|---|
| `SOLANA = RUST ⊕ solana`, `SOROBAN = RUST ⊕ soroban` registrations | **Built** | [ecosystem.py](../composer/pipeline/ecosystem.py) |
| `SolanaApplication` (programs → instructions → accounts, PDAs, CPIs, authorities) | **Built** | [solana/model.py](../composer/spec/solana/model.py) |
| `SorobanApplication` (contracts → functions, storage durability/TTL, `require_auth`) | **Built** | [soroban/model.py](../composer/spec/soroban/model.py) |
| Analysis + property prompts, per-chain vulnerability fragments, code-explorer prompts | **Built** | [templates/solana/](../composer/templates/solana/), [templates/soroban/](../composer/templates/soroban/) |
| `backend_guidance` injection point in the property system prompt | **Built, awaiting our text** | [prop_inference.py:76](../composer/spec/prop_inference.py#L76) |
| Per-component `units()` split | **Built** | both models |
| Null Solana backend (front-half test double) | **Built** | [solana/null_backend.py](../composer/spec/solana/null_backend.py) |
| Sandbox: Landlock+seccomp launcher, `rust_build_policy`, private per-run `CARGO_HOME`, offline | **Built, never driven from a Python backend** | [composer/sandbox/](../composer/sandbox/), [rust/run-confined/](../rust/run-confined/) |
| `project_toolchain` seam (the analyzed project's build system, keyed by `ChainTag`) | **Solana registered** (§7.2); Soroban still has no entry | [rustapp/toolchain.py](../composer/rustapp/toolchain.py), [cargo/toolchain.py](../composer/cargo/toolchain.py) |
| Munge subsystem: editor agent, versioned edit store, VFS diff, compile-check gate, staleness oracle | **Built for EVM**; spine reusable | [source/munge/](../composer/spec/source/munge/) |
| Prover result parsing (treeView JSON → `RuleResult`), cloud polling, callbacks | **Built, chain-neutral** | [prover/results.py](../composer/prover/results.py), [prover/cloud.py](../composer/prover/cloud.py) |
| `certoraSolanaProver` / `certoraSorobanProver` CLIs | **Already installed in the venv** | `certora_cli` |
| RAG corpus registry (`KNOWLEDGE_BASES`, `rag_env`) | **Built and empty — no corpus registered yet** | [rag/db.py](../composer/rag/db.py), [tools/rag_env.py](../composer/tools/rag_env.py) |
| CVLR corpus · CVLR KB articles · project scaffold · authoring loop · Solana CEX analysis | **Nothing** | — |

**Phase 0 (hands-on experience) is done.** Several team members have completed real Solana
verification projects. What that changes: the first phase is not discovery, it is **capture** —
converting existing project knowledge into a reference corpus, prompts, and KB seeds (§7.1).

---

## 3. Confirmed constraints

These are settled and shape the design; they are not open questions.

1. **The Prover runs in the cloud.** No local Prover build is a prerequisite; we submit and
   poll. `ProverOptions.cloud` is the only path we implement. This removes the entire
   local-results branch of [prover/core.py](../composer/prover/core.py) from scope.
2. **Rust builds run locally**, on the machine running AutoProver, **inside the sandbox** —
   never in the cloud. The one carve-out is the macOS development case in item 3.
3. **Confinement is mandatory in production; unconfined local builds are allowed on macOS for
   development only.** `run-confined` is Landlock + seccomp, i.e. Linux only. The backend
   therefore defaults to `provider="launcher"` and a developer on macOS may opt out to the
   `none` passthrough — the mechanism already exists
   ([sandbox/config.py](../composer/sandbox/config.py): `COMPOSER_SANDBOX_PROVIDER=none`,
   documented there as the "trusted-input dev" override).

   Three properties this must keep, or the exception becomes the rule:

   - **Opting out is explicit, never automatic.** An unavailable launcher must still fail
     closed; only a developer setting the env var runs unconfined. This is exactly what
     `ensure_available`'s fail-closed design protects, and it must not be softened into
     "degrade when Landlock is missing."
   - **Production and CI assert confinement.** The Docker image and the expensive-gate test
     refuse a `none` provider rather than trusting the default.
   - **An unconfined run is visibly marked** in the run output and the report, so a result
     produced without confinement is never mistaken for a production one.
4. **Two chains, one backend.** Soroban is a planned deliverable, not a hypothetical. Every
   seam below is justified by an actual second consumer.

---

## 4. Architecture — where the Solana/Soroban seam goes

### 4.1 Share piecemeal; there is no per-chain bundle

"Which chain" is not one decision, so it should not be one object. The things that differ
between Solana and Soroban — how the artifact is built, which prover CLI takes the conf, what
the scaffold looks like, what guidance the property prompt carries, what the munge charter
says — have nothing in common except the word *chain*. Bundling them behind a single
`ChainPack`-style interface would produce one type with a handful of unrelated methods, chosen
before we know whether those methods co-vary.

So: **no per-chain bundle.** Each concern is shared by whatever mechanism already fits it, and
most already exist in the codebase. This is also what the surrounding code does — the repo has
several narrow, single-purpose registries (`ECOSYSTEMS`, `project_toolchain`,
`rag_env._FACTORIES`, `KNOWLEDGE_BASES`), each mapping a tag to one concrete thing for one
concern, rather than one registry of per-chain aggregates.

If the pieces later turn out to move together, bundling them at that point is a mechanical
refactor with two real implementations to check it against. Un-bundling a wrong aggregate is
not.

### 4.2 Where each differing piece lives

| Concern | How it varies | How it is shared |
|---|---|---|
| Authoring loop, edit + compile gate, prover submission, verdict parsing, munge loop, CEX analysis | **Doesn't** | Written once, chain-neutral, in `composer/spec/cvlr/` |
| Build the verification artifact; fast-check command | `cargo certora-sbf` → `.so` vs. a wasm build | The **existing** `project_toolchain` registry (§4.3) |
| Prover CLI + conf fields | `certoraSolanaProver` (`solana_inlining`, `solana_summaries`, `process: sbf`) vs. `certoraSorobanProver` | Ordinary per-chain module in `composer/spec/cvlr/`; a value passed to the submission code, not an interface it calls back into |
| Project scaffold templates | `certora/{confs,mocks,specs}`, `certora = ["no-entrypoint", "dep:cvlr", "dep:cvlr-solana"]`, `#[cfg(feature="certora")] pub mod certora;`, `cvlr_inlining.txt`/`cvlr_summaries.txt` vs. the Soroban equivalent | A template directory per chain — the same split the front half already uses for [templates/solana/](../composer/templates/solana/) vs. [templates/soroban/](../composer/templates/soroban/) |
| `backend_guidance` (property prompt) | Solana vs. Soroban CVLR idioms | A module-level string constant, exactly as `CERTORA_BACKEND_GUIDANCE`, `FOUNDRY_BACKEND_GUIDANCE` and `SOLANA_NULL_GUIDANCE` are today. No mechanism required |
| Munge charter | mocks, feature gates, CPI stubs, SDK nondet vs. host-env and storage stubs | A template, as [munge_charter.j2](../composer/templates/munge_charter.j2) is for EVM |
| Knowledge corpus | `cvlr-solana` vs. Soroban helper sections | One `cvlr` corpus with chain-tagged sections (§5.4) |
| CVLR source access for agents | Which helper crate resolves | One chain-neutral tool set; the crates come from the project's own `cargo metadata` (§5.5) |

The top row is the point: the large, expensive, hard-to-get-right machinery does not vary at
all. What varies is a build command, a CLI name, some templates, and two strings.

**The rule for what varies:** something becomes chain-specific only when Solana and Soroban
*demonstrably* differ. Default to chain-neutral. We are unusually well placed to apply that
rule honestly, because [soroban/model.py](../composer/spec/soroban/model.py) and the Sunbeam
docs already exist and can be consulted while writing each piece.

### 4.3 Reuse the existing build-system registry

The build step should register into the **existing**
[`project_toolchain`](../composer/rustapp/toolchain.py) registry (`ChainTag` → implementation),
not a new one. That module is explicitly designed for this — "a chain's entry is shared by
every wheel targeting it (Crucible's fuzz harness and a future CVLR backend read a Cargo
manifest and build a Solana program the same way)" — and it currently has **no entries**. We
would be the first registrant, and Crucible inherits the work.

This is also a concrete argument against the bundle: the build step's natural sharing axis is
*chain*, across products, including a non-CVLR one. Folding it into a CVLR-owned per-chain
aggregate would break a sharing relationship that the codebase has already set up.

### 4.4 Ship order

Build Solana end-to-end first, and do **not** write Soroban's pieces until Solana verifies a
real property. Keeping the chain-neutral core honest in the meantime costs nothing structural
— it is a review question, asked per piece of code rather than per interface: *would Soroban
need this to be different?* If the answer isn't derivable from
[soroban/model.py](../composer/spec/soroban/model.py) and the Sunbeam docs, it belongs in the
neutral core until proven otherwise.

### 4.5 Placement of the code

In-tree Python under `composer/spec/cvlr/`, mirroring `composer/spec/source/` file-for-file
where possible (`pipeline.py`, `author.py`, `prover.py`, `artifacts.py`, `munge/`). **Not** a
Rust wheel ([rust-applications.md](./rust-applications.md), Crucible's path) and **not** a
pipeline plugin ([PLUGIN.md](../composer/pipeline/PLUGIN.md)): the goal is to reuse the EVM
authoring machinery directly, and that machinery is Python.

---

## 5. The hard parts, and where each lands

### 5.1 Compilation is in the inner loop

**The single biggest departure from the EVM backend.** CVL authoring gates every `put_cvl` /
`edit_cvl` on a sub-second `certoraTypeCheck` ([spec/certoraTypeCheck.py](../composer/spec/certoraTypeCheck.py)).
CVLR's only equivalent is a Rust build — seconds to minutes. That changes the economics of the
whole loop: a slow gate favors fewer, larger edits, which cuts directly against the
edit-small-and-often discipline CVL authoring relies on.

Design response — a **two-tier gate**:

- **Fast tier**, every write: `cargo check --features certora` on the host target. Catches
  unknown CVLR macros, type errors, macro misuse — which is most of what an LLM gets wrong in
  a language it has thin training data for.
- **Slow tier**, before a prover submission only: the full chain build (`cargo certora-sbf`).

Two things must be validated in Phase 1 rather than assumed: (a) that a host-target `cargo
check` is a faithful proxy for the SBF build under the `certora` feature (`no-entrypoint`
changes what compiles), and (b) the actual latency of both tiers on a real project.

**First measurements (public examples repo, one two-crate program, warm workdir, confined):**

| | cold graph | after one edit | no-op |
|---|---|---|---|
| Fast (`cargo check --features certora`, host) | 2.6 s | — | 0.05 s |
| Slow (`cargo certora-sbf --json`) | 3.6 s (15.7 s including platform-tools resolution) | — | 0.23 s |

Both tiers compile this fixture, so (a) has no counterexample yet — but the fixture's `certora`
feature is empty, where a real project's is `["no-entrypoint", "dep:cvlr", "dep:cvlr-solana"]`, and
that is exactly the case the question is about. Treat the answer as open until a project with a real
entrypoint has been through both. The numbers are a floor, not a forecast: two crates is not ten
thousand, and the middle column — the one the authoring loop actually pays — needs a project big
enough for an incremental rebuild to mean something.

One measured cost worth designing around: **mixing a confined and an unconfined build in one workdir
busts the incremental cache**. The private `CARGO_HOME` puts crate sources at different paths, so
fingerprints do not match and the next build is a full one. A session should not switch posture
mid-run, and the dev opt-out is therefore a per-run decision rather than a per-command one.

**Cargo-cache interaction, which is now on the critical path.** The sandbox recipe gives each
run a *private* `CARGO_HOME` under the workdir ([sandbox/recipes.py](../composer/sandbox/recipes.py),
`sandbox_cargo_home`) — deliberately, so an untrusted `build.rs` cannot poison a later run. The
documented cost is that dependencies are fetched per run, with a shared read-only index left as
a deferred optimization ([command-sandbox.md](./command-sandbox.md) §11 item 5). With a build in
the *inner* loop that cost is no longer amortized over a run — it is paid per authoring session
at minimum, and per compile if the workdir is rebuilt each time. **Requirement: one warm,
persistent sandbox workdir per authoring session, reused across every compile in that
session's loop.** If measurements in Phase 1 still show unacceptable latency, the deferred
shared-RO-cache optimization moves into scope.

### 5.2 Munging is larger, but structurally identical

Solana verification requires substantially more source transformation than EVM: mocks, feature
gates, nondet-ing SDK calls, CPI stubs, entrypoint removal. But this is *the same problem*
[source/munge/](../composer/spec/source/munge/) already solves — an editor agent making
versioned VFS edits that must still compile, with a staleness oracle for findings recorded
against older source versions.

What changes: the compile check. [munge/compile_check.py](../composer/spec/source/munge/compile_check.py)
runs `certoraRun --build_only` and verifies that every edited file was actually parsed by solc.
The CVLR analog runs the chain build and verifies every edited file was actually compiled —
same shape, same failure taxonomy (`BuildFailed` / `EditsNotCompiled` / `EditsCompiled`).

What also changes: the **munge charter** ([templates/munge_charter.j2](../composer/templates/munge_charter.j2))
needs a CVLR version, and an explicit give-up boundary. EVM munging is bounded fixup; Solana
munging can approach a rewrite, and the loop needs a stopping rule.

### 5.3 Counterexamples are only as legible as the rule's `clog!`s

Half of this is free: `certoraSolanaProver` produces the same treeView reports, so
[prover/results.py](../composer/prover/results.py) and [prover/cloud.py](../composer/prover/cloud.py)
carry over.

The other half is not a parsing problem — it is an **authoring-discipline** problem. A Solana
counterexample is interpretable only if the rule was written with `clog!` instrumentation of
the values that matter. That pushes the work into prompts and KB articles ("instrument before
you assert"), plus a lint in the compile gate that flags an assert with no logged operands.

The EVM-shaped calltrace analyzer ([analyzer/](../analyzer/)) does not port; a CVLR
counterexample analyzer is new work, and it is chain-neutral (both chains log through `cvlr`).

### 5.4 Documentation and knowledge

The published docs are thin but **complete-shaped**: Setup/Usage/CLI flags/Output/Sanity/
Troubleshooting, plus Speclanguage, Nondet & Havoc, Specs & Lemmas, Mocks & Feature Gates,
Solana Accounts, Anchor, Parametric Rules & Macros, Methodology. Sunbeam has its counterpart.

Three sources feed the corpus, in increasing order of effort:

1. **Published docs** — scraped and imported via [rag-import-format.md](./rag-import-format.md).
2. **Generated crate reference** — an agent reads [Certora/cvlr](https://github.com/Certora/cvlr)
   and [Certora/cvlr-solana](https://github.com/Certora/cvlr-solana) (rustdoc + source) and
   writes reference entries for every macro, derive, and helper. This is the "have an agent
   document CVLR" idea, scoped down to something bounded and verifiable: the output is checked
   by compiling the examples it emits.
3. **Team experience** — the §7.1 capture phase.

A corpus is *recall over curated prose*, which is the right tool for "how do I express X" and
for pitfalls. It is not the right tool for "what exactly does this macro expand to" — and for
that question CVLR, unlike CVL, has an answer available at runtime. See §5.5: the two channels
are complementary, and the corpus should not try to duplicate what the source already states
exactly.

**One corpus, not two.** Register a single `cvlr` corpus with chain-tagged sections, mirroring
the crate split (shared core + chain helpers). Two corpora would duplicate the ~80% that is
genuinely shared and would make the Soroban pack a corpus-sized effort instead of a
templates-sized one. Note this will be the **first** corpus registered in `KNOWLEDGE_BASES`
and `rag_env._FACTORIES` — both are empty today — so it lands as a three-part change (tools
module + connection + importer target), as [rag_env.py](../composer/tools/rag_env.py) requires.

### 5.5 CVLR's source ships with the build — put it in front of the agents

**This inverts the "we have less documentation" premise.** CVL's implementation is not
available to an agent: the language lives inside the Prover, so a curated manual plus RAG is
genuinely all there is. CVLR is the opposite — it is a **cargo dependency of the project under
verification**, so its complete source, every macro definition and every helper signature, is
on disk in the exact version the build resolves. We have *less prose* than EVM but *more ground
truth*.

That is the strongest available answer to the cold-start hallucination risk (§9), and it should
be treated as a first-class knowledge channel rather than a fallback:

- **The code explorer** should be able to read CVLR sources when a question is about what a
  macro does or expands to. Its Rust prompt fragment
  ([code_explorer/rust/common_fragment.j2](../composer/templates/code_explorer/rust/common_fragment.j2))
  is the chain-neutral home for that instruction, since it is true for Solana and Soroban alike.
- **The authoring agent** should read CVLR source directly when unsure of a signature or a
  macro's expansion, and be prompted that this is available and authoritative — outranking both
  RAG recall and its own priors. "Check the crate" must be a cheaper reflex than guessing.

#### Mechanics — four things that need deciding, none of them large

1. **Source reading is rooted at the project.** `SourceFields.project_root` +
   `forbidden_read` ([spec/context.py](../composer/spec/context.py)) scope every fs tool to the
   analyzed project. CVLR's source is outside it (`~/.cargo/registry/src/…`, a git checkout, or
   a path dependency), so **today the agents cannot see it at all.**

2. **Mount it as its own tool set, not as another layer of the project VFS.**
   `fs_tools_layered` ([graphcore/tools/vfs.py](../graphcore/graphcore/tools/vfs.py)) does
   support a backend stack, but layering CVLR into the project view has two problems: paths are
   backend-relative, so `src/lib.rs` in the crate and in the project collide in one flat
   namespace; and the materializer dumps every non-globally-excluded file, so crate sources
   would land in the materialized tree the prover and audit DB see. Note the combination we
   would actually want from a layer — *agent-visible but materializer-excluded* — is not
   expressible today: `forbidden_read` hides from the agent only, `global_exclude` hides from
   both. A separate read-only tool set over `DirBackend(<crate src>)` avoids all of it, and the
   tool's own name is the affordance that tells the agent what it is for.

3. **The host resolves the version; the agent never guesses it.** Read the resolved version
   from the project's own build (`cargo metadata`) and mount exactly that source tree, stating
   the version in the prompt. This matters because `RUST_FORBIDDEN_READ`
   ([ecosystem.py](../composer/pipeline/ecosystem.py)) excludes `*.lock`, so the agent cannot
   see `Cargo.lock` and has no way to know which CVLR it is writing against. Reading a
   *different* version's source than the build compiles is worse than reading no source, because
   it is confidently wrong.

   **Built** ([spec/cvlr/crates.py](../composer/spec/cvlr/crates.py)): the CVLR *family* is resolved
   from `cargo metadata` — not just `cvlr`, since a question about what `cvlr_assert!` expands to is
   answered in `cvlr-asserts` and an agent handed only the facade finds a re-export and stops — and
   each crate arrives paired with the source tree it resolved to, so there is no path that yields a
   version without the code. It also reports where the project and the *corpus* disagree: the
   knowledge base is compile-gated against a recorded reference set
   ([cvlr_reference.py](../composer/spec/cvlr_reference.py)), and a target on another line may not
   have the symbols recalled advice names. Run against the public examples this prints three gaps
   immediately — `cvlr` 0.4.1 against the corpus's 0.6.1, `cvlr-solana` 0.4.4 against 0.5.0, and
   `cvlr-solana-stake` absent entirely — which is the risk table's "reading the wrong CVLR" row
   becoming a fact the run states rather than a hazard it walks into.

4. **This is separate from the sandbox.** The confined build already gets read-only grants for
   the crate sources it needs (`shared_cargo_ro_paths` in
   [sandbox/recipes.py](../composer/sandbox/recipes.py)). Agent file reads are Python-side and
   never enter the sandbox, so the two paths are independent — granting one does not grant the
   other, and nothing here weakens confinement.

One cost to keep honest: CVLR is not tiny, and a curious agent can spend a lot of context in it.
The mitigation is that the *code explorer* is a sub-agent — it can read broadly and return a
short answer, which is exactly the division of labor it already exists for.

### 5.6 There is no AutoSetup, and we do not want one

EVM's AutoSetup exists because getting an arbitrary Solidity project to compile under the
Prover is a genuine search problem. The Solana equivalent is not: it is a **known scaffold** —
a `certora/` directory, a cargo feature, a conditional module, two `.txt` env files, a conf.
That is template work.

Everything genuinely discovery-shaped (which mocks, which summaries, which nondets) is
inseparable from rule authoring and belongs in the formalization loop. The abstraction already
supports exactly this split:

- The `PipelineBackend → preflight → Pre` link ([formalization-abstraction.md](./formalization-abstraction.md))
  is the cheap scaffold step.
- `StagedFormalizer` exists for the "every unit builds on one shared artifact" case — which is
  precisely the Solana shape: one shared certora harness module, many rules.

So: **a thin templated preflight, and no AutoSetup.** If preflight turns out to need search, we
will have learned something and can revisit — but we should not build for it speculatively.

---

## 6. Test and gate strategy

Stated up front because it constrains the phase order.

- **Deterministic first.** Phase 1 (§7.2) is entirely LLM-free and unit-testable: given a
  hand-written CVLR file, produce verdicts. Every hard integration risk — sandbox, toolchain,
  conf, submission, polling, parsing — is retired before any agent is involved.
- **Sandbox gate.** [tests/test_sandbox_escape.py](../tests/test_sandbox_escape.py) already
  proves what is and is not granted. Extend it for the CVLR build workload rather than adding a
  parallel gate.
- **Template fuzzing.** New templates must be registered in
  [template_manifest.json](../template_manifest.json) so
  [tests/test_fuzzed_templates.py](../tests/test_fuzzed_templates.py) renders them against
  generated models — this is what catches a field rename breaking a prompt.
- **Replay tapes.** A Solana smoke scenario under [test_scenarios/](../test_scenarios/) with a
  recorded tape (`generate-tape` skill) gives LLM-free end-to-end CI coverage, as with the EVM
  and Foundry backends.
- **Expensive gate.** One `expensive`-marked end-to-end test that submits a real cloud job,
  matching the existing convention (never run without `-m "not expensive"`).
- **Routine gate unchanged**: `uv run --no-sync pytest tests/ -m "not expensive" -q` and
  `uv run --no-sync pyright` must both stay clean.

---

## 7. Plan of attack

Phases 2 and 5 parallelize with the critical path (1 → 3 → 4 → 6).

### 7.1 Phase 1a — Capture (replaces the usual discovery spike)

> Detailed procedure: **[cvlr-capture-plan.md](./cvlr-capture-plan.md)**, which splits this
> phase in two: **Phase A** is purely machine-driven and blocked on nothing, ending with a
> working prototype corpus *and* a ranked ledger of questions only practitioners can answer;
> **Phase B** spends expert time burning that ledger down. Phase A can start immediately and
> unblocks the rest of this plan by itself. Summary below.

Because the team has already done real Solana verification projects, the work is extraction,
not exploration. Deliverables:

- **One reference project**, checked in (or added to AutoProverExamples), that verifies today.
  This becomes the smoke scenario, the integration-test fixture, and the scaffold's ground truth.
- **The munge diffs** from those projects, as the corpus behind the CVLR munge charter.
  Extracted from a *merge-base* diff, not a branch-tip diff — the difference is two orders of
  magnitude of noise ([capture plan](./cvlr-capture-plan.md) §3.1).
- **The conf files and `[package.metadata.certora]` blocks** actually used — these define the
  conf shape we generate, rather than our reading of the docs.
- **Failure transcripts**: what went wrong, what the prover said, what fixed it. These become
  the first KB articles and are the highest-value knowledge we have, because they are the part
  no public documentation contains.
- **A written list of "what a good CVLR rule looks like"** from the practitioners — the raw
  material for `backend_guidance` and the authoring prompt.
- **A vintage tiering of the project set.** The corpus is not a uniform sample: a few recent
  projects reflect current CVLR/Prover practice, many older ones use superseded methods. Older
  projects vote on the *problem set*; recent ones decide the *solution form* ([capture
  plan](./cvlr-capture-plan.md) §2.1). Ranking captured idioms by raw popularity would promote
  the obsolete, since the obsolete is the more numerous.

This phase is short but blocking: Phases 2, 3, and 4 all consume its output.

### 7.2 Phase 1b — Deterministic plumbing (no LLM)

Solana backend skeleton · `project_toolchain` registration · sandboxed build,
two-tier · warm-workdir lifecycle · conf generation · `certoraSolanaProver` submission ·
cloud polling · treeView → verdicts · provider selection (launcher default, explicit
`none` opt-out, production assertion) · resolving the CVLR crate versions and source
locations from `cargo metadata` (§5.5), which is pure plumbing and unblocks Phase 2.

**Exit criterion:** given the Phase 1a reference project and a hand-written CVLR rule, the
system produces verdicts, with measured latency for both compile tiers.

This is the riskiest infrastructure in the project (first Python-side use of the sandbox) and
it is fully testable without an LLM. It goes first.

**Met.** Against the public examples' `first_example` — confined build, real cloud submission, no
agent anywhere — all seven rules matched the project's own `expectedDefault.json`, the failing rule
and the sanity-failing rule included. 2 min 13 s wall clock, of which 3.6 s was the build. The
assertion is equality with the fixture's expected verdicts, not "results came back", so the treeView
parse is confirmed to carry over to SBF rather than merely to run.

#### 7.2.1 What is built

Two packages, split on the line §4.3 draws — Cargo is *chain* knowledge shared across products, and
only what speaks to the Prover is CVLR's:

| Module | Role |
|---|---|
| [cargo/metadata.py](../composer/cargo/metadata.py) | `cargo metadata`, typed: the crate owning a file, and the version every dependency resolves to |
| [cargo/session.py](../composer/cargo/session.py) | The warm workdir + the fast tier. Confined `cargo check`, unconfined `cargo fetch` |
| [cargo/sbf.py](../composer/cargo/sbf.py) | The slow tier (`cargo certora-sbf`), its build manifest, and the build script handed to the prover |
| [cargo/toolchain.py](../composer/cargo/toolchain.py) | `PROJECT_TOOLCHAINS["solana"]` — the registry's first entry |
| [spec/cvlr/conf.py](../composer/spec/cvlr/conf.py) | Reading a project's conf (JSON5) and layering a run onto it |
| [spec/cvlr/crates.py](../composer/spec/cvlr/crates.py) | §5.5: which CVLR the build resolves, and where it disagrees with the corpus's reference set |
| [spec/cvlr/prover.py](../composer/spec/cvlr/prover.py) | Build → configure → submit, on the shared `run_prover` |

Three changes to shared code rather than copies of it. `ProverOptions` gained an `app` field and the
subprocess wrapper an app argument, so all three Prover CLIs reach the same submission path —
`run_solana_prover` and `run_certora` have the same signature, which is the whole reason cloud
polling and the treeView parse carry over untouched. `UnanalyzedCexHandler` is a no-LLM
`CexHandler`, because `run_prover`'s handler hook was the one place an agent was unavoidable and a
deterministic caller has nothing for it to do.

Tests: [test_cvlr_plumbing.py](../tests/test_cvlr_plumbing.py) (routine gate — no toolchain, no
network, no LLM) and [test_cvlr_end_to_end.py](../tests/test_cvlr_end_to_end.py) (`expensive`).

Run the expensive one with **`env -u CERTORA`**. `$CERTORA` is a normal EVM development setting that
points the CLI at a local Prover checkout ([certora_env.py](../composer/certora_env.py)); such a
build reports itself as "no package installed", so the submission is refused before upload unless it
also names a `prover_version` — and a Prover branch need not produce the verdicts the fixture's
expected file was recorded against. The test skips on it rather than failing, since the failure names
neither the variable nor the file it would invalidate.

#### 7.2.2 The build is ours, and the prover reruns it

The one design decision here that is not mechanical. `certoraSolanaProver` offers two ways in, and
they are not symmetric:

- **`files: ["…so"]`** — hand it a finished artifact. It then copies the `.so` and *nothing else*:
  `set_rust_build_directory` only collects Rust sources on the other path, so `.certora_sources`
  would carry no source for the report or the counterexample analyzer to read (§5.3).
- **`build_script`** — it runs a script and reads a build manifest from its stdout. Sources are
  collected. But the CLI's *own* from-sources path shells out to `cargo certora-sbf` inside its
  process, unconfined, which §3 item 2 does not permit.

So: **the backend builds, confined, as its pre-submission gate, and hands the prover a generated
build script that reruns the same command under the same confinement.** The second run is a warm
cargo no-op (0.23 s measured) and it is what keeps the manifest describing a build that exists on
disk right now, rather than a recording that goes stale when anything moves. The confinement reaches
the script as an opaque `argv_prefix` from `SandboxConfig.backend_spec` — the same mechanism a Rust
wheel is handed — so the script names no sandbox.

Two smaller things fall out. `cargo certora-sbf` registers a rustup toolchain link around every
build, which writes to a read-only `RUSTUP_HOME`, so `--no-rustup` is not optional. And a confined
build cannot download missing platform tools, so an absent `cargo_tools_version` is raised as its
own error naming the version and the one-line install — the failure otherwise surfaces inside the
toolchain naming neither.

### 7.3 Phase 2 — Knowledge (parallel with 1b)

The `cvlr` RAG corpus (docs import + generated crate reference), its three-part registration,
and the first KB articles seeded from Phase 1a's failure transcripts. Also the
`backend_guidance` text, which unblocks better property extraction immediately — before any
authoring code exists.

Plus the CVLR-source tooling of §5.5: the read-only tool set over the resolved crate sources,
wired into the code explorer's env, and the prompt addendum in the shared Rust fragment. Worth
doing here rather than in Phase 4 — it is independent of the authoring loop, it makes the
generated crate reference (item 2 above) cheap to produce and to verify, and it is the piece
that most directly attacks the hallucination risk.

### 7.4 Phase 3 — Preflight scaffold

Templated `certora/` project shape, cargo feature wiring, conf and env files. Agent-assisted
only where a template genuinely cannot decide. Validated by scaffolding a project that was
*not* the Phase 1a reference.

**Derive the scaffold from the public examples repo, not from a vote over the client projects.**
The project survey ([capture plan](./cvlr-capture-plan.md) §3) found twelve distinct layouts
across fourteen projects, and layout tracks the client/team rather than the vintage — two
engagements for one client match each other. A majority vote would therefore reproduce somebody's
house style. A public examples repo instead carries two complete minimal projects that *are* the
intended shape, plus canonical `*_core` inlining and summaries files and a conf variant pair
(a default conf versus a multi-assert one, each with its own expected-verdict file). Treat the
client layouts as evidence of what varies in the wild — useful for making the scaffold tolerant —
not as candidates for what it should emit.

### 7.5 Phase 4 — The authoring loop

The `Formalizer`, mirroring [source/author.py](../composer/spec/source/author.py): author →
fast compile gate → full build → prover → analyze → revise, with the property-feedback judge
and skipped-property accounting reused. `clog!` discipline enforced in prompt and lint. The
authoring env binds the CVLR-source tools from Phase 2, and the authoring prompt states that
the crate source is present and authoritative — check it rather than guess.

### 7.6 Phase 5 — Munge (parallel with 4)

Reuse the munge agent spine with a CVLR compile check and a Solana munge charter, plus the
give-up boundary.

### 7.7 Phase 6 — Counterexamples and report

A CVLR counterexample analyzer (chain-neutral) and the report path; the shared report assembly
is already domain-neutral.

### 7.8 Phase 7 — Productionization

`console-solana` / `tui-solana` entry points (following the `console-foundry` / `tui-foundry`
pattern), the Docker image gaining the Rust + Solana platform-tools toolchain, the replay tape
and smoke scenario, the expensive-gate test, and user-facing docs.

### 7.9 Phase 8 — Soroban

Only after Solana verifies end-to-end. Expected content: build recipe, conf shape, prover CLI,
scaffold templates, `backend_guidance`, munge charter, corpus sections. Expected *non*-content:
anything in the chain-neutral core. **If Phase 8 requires changes there, that is information
about where the real chain boundary lies — record it, and let it (not a guess made now) decide
whether any of these pieces deserve to be bundled after all.**

---

## 8. Decisions

### Settled

| Decision | Choice |
|---|---|
| Backend implementation | In-tree Python, `composer/spec/cvlr/`, mirroring `spec/source/` |
| Chain sharing | Piecemeal — no per-chain bundle; each concern shared by the mechanism that already fits it; default chain-neutral |
| Build-system seam | Register into the existing `project_toolchain` registry |
| Prover | Cloud only |
| Builds | Local to AutoProver, always sandboxed |
| macOS | Unconfined local builds allowed for dev via an explicit opt-out; never in production or CI |
| Compile gate | Two tiers (fast `cargo check` per edit, full build per submission) |
| Conf style | From-sources, through a `build_script` the backend generates — it builds inside the sandbox and the prover reruns that same command (§7.2.2) |
| Setup | Templated preflight; no AutoSetup |
| RAG | A single `cvlr_kb` corpus with chain-tagged sections, fed by two manifests: a public docs+crate-reference one built in this repo, and a project-derived one from a separate private repo ([capture plan](./cvlr-capture-plan.md) §8) |
| CVLR source | Mounted read-only as its own tool set (not a project-VFS layer), version resolved by the host from `cargo metadata`, available to the code explorer and the author, and named as authoritative in the prompt |
| Ship order | Solana end-to-end first; Soroban pack after |

### Open — to close during Phase 1

1. **Is a host-target `cargo check --features certora` a faithful proxy for the SBF build?**
   Measure; if not, the fast tier becomes a cheaper SBF-target check. *Partially answered* (§5.1):
   both tiers accept the public examples, but that fixture's `certora` feature is empty, so the
   `no-entrypoint` case the question is actually about is still untested.
2. ~~**Conf style: `[package.metadata.certora]` from-sources, or explicit pre-built `.so`?**~~
   **Settled: from-sources, through a build script the backend owns** (§7.2.2). The capture survey
   found `build_script` in 15 of 16 projects and `[package.metadata.certora]` with exactly three
   keys, and the implementation found the harder half of the reason: the `files` path collects no
   Rust sources into `.certora_sources`, and the CLI's own from-sources path builds unconfined.
3. **Warm-workdir lifetime** — per authoring session (planned) vs. per unit vs. per run, and
   whether the shared read-only cargo cache optimization is needed on day one. One constraint
   discovered while measuring: a workdir may not switch confinement posture mid-run without losing
   its incremental cache (§5.1).
4. **Where the munge give-up boundary sits**, in edits or in wall-clock.
5. **Rule granularity per property** — one `#[rule]` per property, or parametric rules /
   `cvlr_rules!` batching, which changes both prover cost and CEX attribution.

---

## 9. Risks

| Risk | Why it matters | Mitigation |
|---|---|---|
| **Loop latency** | A build-gated loop may be 10–50× slower per iteration than CVL's typecheck gate; it compounds with retries | Measured in Phase 1b before any agent work; two-tier gate; warm workdir; shared RO cache in reserve |
| **Cold-start hallucination** | Thin CVLR training data means invented macros and helpers | Two independent defenses: the crate source is readable and authoritative (§5.5), which prevents the guess; and the compile gate catches whatever slips through — but only if the gate is in the loop, which loops back to the latency risk. That coupling is the central tension of the project, and source access is the cheaper half of the answer |
| **Reading the wrong CVLR** | Source that disagrees with the version the build resolves is confidently wrong — worse than no source | The host resolves the version from `cargo metadata` and mounts exactly that tree; the agent never picks a version, and the version is stated in the prompt |
| **Unbounded munging** | Solana munging can approach a rewrite | Explicit give-up boundary; charter derived from real diffs, not imagination |
| **Illegible counterexamples** | Verdicts the agent cannot act on stall the loop | `clog!` discipline enforced at authoring time, not diagnosed at analysis time |
| **Premature Soroban abstraction** | An interface designed against a guess costs more than a later refactor, and is harder to undo | No per-chain bundle (§4.1); Soroban's pieces deferred to Phase 8; "would Soroban need this different?" asked at review time against the existing Soroban model |
| **The dev opt-out leaking into production** | An unconfined build runs untrusted `build.rs` and proc-macros with the developer's full environment; if it becomes the silent default, the whole confinement story is decorative | Explicit env-var opt-out only, never an automatic degrade; production/CI assert the launcher; unconfined runs marked in output and report |
| **Unconfined dev runs behave differently** | Passthrough means no policy at all — so no forced `CARGO_NET_OFFLINE`, no private `CARGO_HOME`, no filesystem grants. A build that works on a Mac may fail confined on Linux for reasons unrelated to the code | Treat Linux-confined as the reference environment; the smoke scenario and expensive gate both run confined |

---

## 10. Sources

- [The Certora Solana Prover](https://docs.certora.com/en/latest/docs/solana/index.html)
- [Using the Certora Solana Prover](https://docs.certora.com/en/latest/docs/solana/usage.html)
- [CVLR — Certora Verification Language for Rust](https://docs.certora.com/en/latest/docs/solana/speclanguage.html)
- [Rule Sanity Checks (Solana)](https://docs.certora.com/en/latest/docs/solana/sanity.html)
- [Certora/cvlr](https://github.com/Certora/cvlr) · [Certora/cvlr-solana](https://github.com/Certora/cvlr-solana)
