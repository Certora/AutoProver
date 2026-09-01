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
| `backend_guidance` injection point in the property system prompt | **Built; Solana's text written** (§7.3.1) | [prop_inference.py:76](../composer/spec/prop_inference.py#L76), [cvlr/guidance.py](../composer/spec/cvlr/guidance.py) |
| Per-component `units()` split | **Built** | both models |
| Null Solana backend (front-half test double) | **Built** | [solana/null_backend.py](../composer/spec/solana/null_backend.py) |
| Sandbox: Landlock+seccomp launcher, `rust_build_policy`, private per-run `CARGO_HOME`, offline | **Built, never driven from a Python backend** | [composer/sandbox/](../composer/sandbox/), [rust/run-confined/](../rust/run-confined/) |
| `project_toolchain` seam (the analyzed project's build system, keyed by `ChainTag`) | **Solana registered** (§7.2); Soroban still has no entry | [rustapp/toolchain.py](../composer/rustapp/toolchain.py), [cargo/toolchain.py](../composer/cargo/toolchain.py) |
| Munge subsystem: editor agent, versioned edit store, VFS diff, compile-check gate, staleness oracle | **Built for EVM**; spine reusable | [source/munge/](../composer/spec/source/munge/) |
| Prover result parsing (treeView JSON → `RuleResult`), cloud polling, callbacks | **Built, chain-neutral** | [prover/results.py](../composer/prover/results.py), [prover/cloud.py](../composer/prover/cloud.py) |
| `certoraSolanaProver` / `certoraSorobanProver` CLIs | **Already installed in the venv** | `certora_cli` |
| RAG corpus registry (`KNOWLEDGE_BASES`, `rag_env`) | **`cvlr_kb` registered** — the first corpus in either map | [rag/db.py](../composer/rag/db.py), [tools/rag_env.py](../composer/tools/rag_env.py) |
| CVLR corpus · CVLR KB articles | **Three manifests under one tag** (§7.3.1): published docs, generated crate reference, project-derived idioms — all produced in and shipped from the private repo | [rag/import_format.py](../composer/rag/import_format.py), [rag/html_manual.py](../composer/rag/html_manual.py) |
| Preflight scaffold | **Done** (§7.4): deterministic, idempotent, refuses two decisions rather than guessing | [cvlr/scaffold.py](../composer/spec/cvlr/scaffold.py), [cvlr/preflight.py](../composer/spec/cvlr/preflight.py) |
| Authoring loop | **Built** (§7.5), not yet exercised end to end | [cvlr/author.py](../composer/spec/cvlr/author.py), [cvlr/pipeline.py](../composer/spec/cvlr/pipeline.py) |
| Solana CEX analysis | **Nothing** — the author reads raw counterexample dumps until §7.7 | — |

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

#### 7.3.1 State

| Piece | State |
|---|---|
| Corpus registration (tools module · `KNOWLEDGE_BASES` · importer target) | **Done** — [cvlr_rag.py](../composer/tools/cvlr_rag.py), [rag_env.py](../composer/tools/rag_env.py), [db.py](../composer/rag/db.py). `cvlr_kb` is the first corpus AutoProver has ever registered |
| Published docs import | **Done**, then moved out — `certora-cvlr-kb` `tools/docs_manifest.py` → `cvlr-docs.rag.json` (156 sections, 147 groups). Built here originally; see §7.3.3 for why it moved |
| First KB articles from real projects | **Done, shipping from the private repo** — 83 entries + 15 recipes under one of the three manifests sharing the `cvlr_kb` tag ([capture plan](./cvlr-capture-plan.md) §8.2) |
| CVLR-source tool set (§5.5) | **Done** — [crate_mount.py](../composer/spec/cvlr/crate_mount.py) (the tree) + [source_tools.py](../composer/spec/cvlr/source_tools.py) (the tools), reaching the explorer through `build_source_tools(library_source=…)` |
| `backend_guidance` | **Done** — [guidance.py](../composer/spec/cvlr/guidance.py) |
| Generated crate reference | **Built and run**, in the private repo — `certora-cvlr-kb` `tools/crate_reference.py` → `cvlr-crates.rag.json` (175 sections, 117 groups, 310/310 items covered) |

Two things about the source mount are worth stating, because they are what make it more than a
convenience.

**Every path carries its version.** A file is addressed as `cvlr-asserts-0.6.1/src/core.rs`, never
`cvlr-asserts/src/core.rs`. That closes the one risk in §9 with no other mitigation: source
disagreeing with what the build resolved is *confidently* wrong, and `RUST_FORBIDDEN_READ` hides
`Cargo.lock`, so an agent cannot otherwise tell which CVLR it is holding. Stamping the version into
the path makes an unversioned answer unreachable rather than merely discouraged.

**The whole family, not the facade.** `cvlr` re-exports almost its entire surface. Run against the
public examples, `cvlr_assert!` resolves to `cvlr-asserts-0.4.1/src/core.rs:66` — an agent given only
the crate the manifest names would find the re-export and stop.

The tools and the sentence introducing them hang off one object, because binding either alone fails
in both directions: tools with no statement leave an agent unaware that authoritative source is
reachable, and a statement with no tools invites it to fabricate the reads. The same reasoning made
`crate_source` a **required** prompt parameter rather than an optional one — `None` already spells
"no mount", and a second spelling for that state is one a caller reaches by forgetting rather than
by deciding. (The template fuzzer caught exactly that, which is what the fuzzer is for.)

#### 7.3.2 The crate reference, and what its gates caught

The published manual covers CVLR's *methodology* well and its *surface* thinly — the capture survey
measured `cvlr-solana` at 4 of 28 functions and 0 of 7 macros named. That gap is where an authoring
agent invents a helper, and it is closed from the one source that cannot be stale: the crates
themselves, at the pinned versions.

**310 public items across 14 crates**, and three shapes a line scan gets wrong, each hiding names
that are reached for constantly:

| Shape | Example | Why a scan misses it |
|---|---|---|
| A macro that generates macros | `impl_bin_assert!(cvlr_assert_le, <=, $)` | The exported name exists only as an *argument*. Four of these emit 24 names in `cvlr-asserts` alone |
| A renamed re-export | `pub use super::log::cvlr_log as clog` | The name every project writes is an alias; the definition carries a different one |
| Test code | `#[cfg(test)] mod tests { pub fn … }` | Indistinguishable from API on any single line |

**Where the producer lives, and why not here.** Rebuilding this manifest calls a model and runs
cargo, so it costs an API key and a few dollars — the same shape as the capture plan's abstraction
pass. It therefore lives in the private `certora-cvlr-kb` repo beside that machinery, and its output
ships in that package like every other generated manifest. One consequence is worth stating plainly:
**the manifest's *content* is public**, derived entirely from published crates. It sits in a private
repo because of how it is *built*, not because of what is in it, and anything that treats everything
under that repo's `data/` as client-derived would be wrong about this file.

What stayed here is what the *backend* needs at run time: [crate_mount.py](../composer/spec/cvlr/crate_mount.py)
is imported by the producer from an `AUTOPROVER_REPO` checkout, the same way that repo already reads
the manifest schema and the reference set, so there is one definition of "the crate trees this build
resolved" rather than two.

**Two gates, both properties.** Every example compiles against the reference set, and every
inventoried item must be *named* by some entry. The second is what makes "grouped" coverage honest:
one entry may legitimately cover twenty mechanical variants, and the gate demands it name the
twenty rather than quietly documenting three. The compile gate sits *inside* the retry loop, which
is the capture pass's lesson — that placement took its examples from 4 of 48 compiling to 35 of 35.

**What the gates actually caught** is the part worth recording, because both were about to ship
advice that no target project could follow:

- Examples wrote `use cvlr_early_panic::early_panic;` — the defining sub-crate. A project depends on
  the *facade*, so that import resolves nowhere. The system prompt now names the importable crates,
  derived from the reference set so the list and the probe's dependencies cannot disagree.
- Examples called `nondet_option(…)` after `use cvlr::prelude::*;`. It is not in the prelude. A
  module documented in isolation cannot know its own public spelling — `nondet_option` is defined in
  `cvlr-nondet` and reached as `cvlr::nondet::nondet_option`, and `pub mod havoc` sits behind
  `#[cfg(feature = "std")]`. Both the facade's `lib.rs` and the defining crate's now ride along in
  every prompt, which took `havoc.rs` from failing every attempt to passing on the second.

**The pass, as run.** 117 entries over 175 sections for **$3.58**; 27 of 41 modules passed on the
first attempt, 7 on the second, 4 on the third. **Every one of the 310 items is named by some
entry**, and 111 of the 117 entries carry an example the compiler accepted.

The six that do not are the change worth recording. Dropping an entry because no *self-contained
snippet* could demonstrate it makes coverage hostage to exemplifiability, and those are not the same
property — `cvlr_spec!` and the `cvlr_assert_all!` family need a spec, base functions or a
caller-supplied type, which is more context than a snippet carries. So the prose ships and the
unverified example does not: the summary and the form are derived from the crate source and stay
true, and the section says plainly that no example could be verified, pointing the reader at the
`cvlr_source_*` tools. The corpus keeps the guarantee worth having — *every example in it compiles* —
without losing the constructs a reader most needs.

**Macro expansions are quoted, not described.** The crates ship 58 `macrotest` snapshot pairs — an
invocation beside its expansion. "What exactly does this macro expand to" is the question §5.4 says
a corpus is the wrong tool for, and here the crate answers it exactly, in the version we pin. Asking
an agent to describe those would be inference over something sitting on disk.

`backend_guidance` is deliberately not modelled on the EVM text, and the difference is not
stylistic. Two of `CERTORA_BACKEND_GUIDANCE`'s exclusions **invert** here. Checked arithmetic makes
"no overflow" uninteresting on EVM, while a Solana release build does not check by default and the
Prover has `assert_on_panic` for precisely this — panic-freedom is a frequently-violated property
class on this chain, not a non-property. And an EVM property spanning many functions is expensive,
where `cvlr_rules!` fans one property across a grid of handlers for the price of a line — so a
cross-handler property is *cheaper* than several per-handler restatements, and the extractor should
be told to prefer it. The text is therefore mostly about **shape** (what a property must look like
for a rule to exist) and reserves exclusion for the few things that genuinely have no rule.

#### 7.3.3 Why the documentation manifest followed it out

The docs producer was the one piece of the corpus this repo could rebuild alone, and that was the
argument for keeping it: bs4 and a public docs checkout, so an install with no access to the private
repo still assembled *some* CVLR corpus. The property was real. It bought less than it looked like.

A corpus of methodology with no API surface and no project practice advises an authoring agent about
a language it cannot then be told the spelling of — and the surface is precisely the half the manual
covers thinly (§7.3.2), which is why the crate reference exists at all. The fallback was therefore
not a smaller corpus but a differently-shaped one, aimed at the risk it is worst at.

The second reason is provenance. Three manifests built in three places can each be a different
vintage, and nothing downstream can tell: the corpus carries one tag, and `part` numbering hides the
seam by design. Building all three in one repo makes the corpus one artifact with one story about
what produced it.

So all three now ship in the `certora-cvlr-kb` package, and **a plain AutoProver install has no CVLR
corpus** — a supported state (the backend falls back to `backend_guidance`) but no longer a partial
one. What this repo keeps is the piece with two consumers:
[html_manual.py](../composer/rag/html_manual.py) stays, because the EVM corpus builder uses it, and
the docs producer imports it from an `AUTOPROVER_REPO` checkout like everything else that repo reads
rather than restates. The moved producer reproduces the previous manifest byte-for-byte from the same
manual, which is the check that the move changed only where the code lives.

The one thing that got *simpler* is discovery. `populate_cvlr_rag.sh` had a four-tier ladder because
one manifest came from this repo's tree and the others from a package; both it and the Docker
entrypoint are now two tiers over one source — a checkout, else the installed package. The
installed-package tier had a real bug that the collapse removed: it called
`certora_cvlr_kb.practice_manifest()`, the only accessor the package exposed, so an install found the
practice manifest and silently missed the crate reference. The package now answers `manifests()`,
which is the question a caller actually has.

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

#### 7.4.1 State

| Piece | State |
|---|---|
| Templated project shape | **Done** — [scaffold.py](../composer/spec/cvlr/scaffold.py): the harness module tree, the cargo feature, the three `Cargo.toml` stanzas, `.gitignore` |
| Cargo feature wiring | **Done** — `certora = ["no-entrypoint", "dep:cvlr", "dep:cvlr-solana"]`, with `no-entrypoint` included only when the package has it and the CVLR deps `optional` so a release build never sees them |
| Env (inlining / summaries) files | **Done** — the template's three-layer split, vendored with a provenance stamp; [refresh_cvlr_envs.py](../composer/scripts/refresh_cvlr_envs.py) re-vendors and records the upstream commit |
| Conf | **Already done in phase 1b** — the run owns its conf ([conf.py](../composer/spec/cvlr/conf.py)); the scaffold writes none, so there is no second opinion about prover flags |
| Preflight step and its gate | **Done** — [preflight.py](../composer/spec/cvlr/preflight.py), split into `prepare_workspace` and `gate_workspace` so only the compile lands on the run's CPU budget |
| Validated against a non-reference project | **Done** — the public examples' `vault_application`, stripped back to plain program code and scaffolded from nothing: it compiles with the harness in, and vanishes cleanly without the feature |
| Agent assistance | **None needed.** See below |

**Where a template cannot decide, it refuses.** §7.4 asked for agent assistance "only where a
template genuinely cannot decide", and the honest answer is nowhere: every decision is read from
`cargo metadata` or from the reference set. The two cases that are not decidable turn out to want a
*human*, not a model, because each is a change to how the project builds for everyone:

- **A package that builds no `cdylib`.** Adding one changes how that library builds everywhere.
- **A CVLR pin that contradicts the project's platform generation** (§7.4.2).

Both are `Blocked`, and a plan carrying either applies *nothing* — a half-scaffolded project turns
the next build failure into a question with two candidate answers.

**Idempotence is the property that mattered most**, because a scaffold gets re-run whenever anyone
is unsure whether it ran. Nothing is ever overwritten, and every manifest change is computed from
the *parsed* manifest, so a second run is a no-op. That is also the one thing the template's own
`certora-setup.py` gets wrong: it appends to `Cargo.toml` unconditionally, so running it twice
leaves a manifest cargo will not parse. One change cannot be an append — re-opening `[features]` at
the end of a manifest is a duplicate-table error — so that key is inserted into the table the
package already has, and the project's comments and ordering survive because the manifest is never
reserialized.

#### 7.4.2 What building it found

**The platform pairing fails to compile, and does so illegibly.** The reference set already said a
CVLR chain crate *implies* a platform generation; what was missing was anywhere that could notice
the implication is false for a given project. Given `cvlr-solana` 0.5.0 on a `solana-program` 1.18
project, rustc says:

```
expected `[AccountInfo<'_>; 8]`, found `[AccountInfo<'_>; 16]`
note: `solana_account_info::AccountInfo<'_>` and `AccountInfo<'_>` have similar names,
      but are actually distinct types
```

Two independent mismatches — the type *and* the arity — and the note is the only clue that there are
two `AccountInfo`s in the build. That was measured rather than assumed, and it is why the check runs
before the pin is written rather than after.

**An optional dependency is invisible to `cargo metadata`.** The scaffold declares CVLR
`optional = true`, which is what keeps it out of a release build — and a default-feature
`cargo metadata` therefore reports it absent. Found by reading the first real preflight's output: the
version-gap report said `cvlr-solana is not a dependency` for a project that had just been given one,
and [crates.py](../composer/spec/cvlr/crates.py)`.roots()` — the CVLR **source mount** that §5.5 calls
the cheaper half of the answer to the hallucination risk — would have found nothing to mount. The
whole §5.5 mitigation would have been silently empty on every scaffolded project. `read_workspace`
now takes `features`, and preflight resolves the graph the verification build actually gets, from the
package's own directory (feature selection resolves against the package cargo considers current).

**`cargo metadata` reports where the library's source is**, and the scaffold reads it instead of
assuming `src/lib.rs`, because `[lib] path` can move it and a module declaration appended to a file
nothing compiles is a scaffold that reports success and changes nothing.

**Open question 1 is now answered.** The `certora` feature the fast tier was measured against in
§5.1 was empty; the scaffolded project's is not — `["no-entrypoint", "dep:cvlr", "dep:cvlr-solana"]`
— and the host-target `cargo check` accepts it, catches a deliberate error inside the harness, and
confirms the whole subtree disappears without the feature.

#### 7.4.3 Four gaps in the recommended starting point

Worked around here rather than reproduced, and worth reporting upstream:

| Gap | Consequence |
|---|---|
| `[package.metadata.certora] sources` omits `Cargo.toml` | `.certora_sources` — what the report and the CEX analyzer read — has no manifest, so the collected tree cannot be rebuilt. The public examples include it |
| `[workspace.dependencies]` pins CVLR 0.4 by hand | Two places decide which release "current" means. The scaffold uses the reference set, which is the one place that should |
| `mod.rs` never declares `utils.rs` | The file ships and is never compiled. Not emitted here |
| `README` names `solana_inlining.txt` / `solana_summaries.txt` | The shipped files are `cvlr_*`; a reader following the README edits nothing |

#### 7.4.4 The platform gate was blind to the generation that deleted its probe

Found by pointing the preflight at a real Anchor 1.x program (§7.5.5), and worth its own section
because the shape of the mistake generalizes.

The gate asked whether the target resolves `solana-program` at a different major than the reference
set names, and treated *absent* as *no opinion*. But Solana's v3 split moved `AccountInfo` into
`solana-account-info` and stopped publishing `solana-program` — so a v3 target resolves none at all,
and absence is the evidence of a mismatch rather than the absence of one.

What made it worse than a missed diagnostic is that **every cheap check downstream still passes.**
The scaffold contains no code bridging the project's accounts to a CVLR helper, so it compiles, and
the preflight's fast-tier gate goes green on a workspace carrying two incompatible generations of
`solana-account-info`. The failure lands on the first authored rule, as
`expected AccountInfo, found AccountInfo` — an error the author cannot act on, because the fix is a
dependency decision rather than a code one. It would have spent its whole budget discovering that.

`PlatformGeneration` now carries `witnesses` separately from `crates`. The two roles disagree exactly
where it matters: `crates` names what *this* generation declares, so it can only ever mention crates
this generation has, while a newer generation is detected precisely by the crate this one **lacks**.
The general lesson for any version gate: *a probe that only exists in the versions you already
support cannot detect the versions you do not.*

### 7.5 Phase 4 — The authoring loop

The `Formalizer`, mirroring [source/author.py](../composer/spec/source/author.py): author →
fast compile gate → full build → prover → analyze → revise, with the property-feedback judge
and skipped-property accounting reused. `clog!` discipline enforced in prompt and lint. The
authoring env binds the CVLR-source tools from Phase 2, and the authoring prompt states that
the crate source is present and authoritative — check it rather than guess.

#### 7.5.1 State

| Piece | State |
|---|---|
| The `Formalizer` and the loop | **Done** — [author.py](../composer/spec/cvlr/author.py): buffer, skips, expected failures, two gate tiers, judge, publish, give-up, budget wrap-up |
| `PipelineBackend` | **Done** — [pipeline.py](../composer/spec/cvlr/pipeline.py). The front half is the Solana ecosystem's, reused verbatim; §2's claim that it was already built is now cashed |
| The staged formalizer | **Done** — declares every unit's module before any unit authors, which §5.6 predicted |
| Rule-name ground truth | **Done** — [rules.py](../composer/spec/cvlr/rules.py), validated against the two public examples' hand-written conf `rule` lists |
| `clog!` discipline | **In the prompt and in the judge's checklist**, not in a lint. See below |
| Crate source in the authoring env | **Done** — bound to the author *and* the judge, and named as authoritative in both system prompts |
| Property-feedback judge, skip accounting | **Reused** — `composer/authoring/` is already backend-neutral; this backend supplies wording, prompts and the mapping vocabulary |
| Exercised end to end | **No.** Every gate and every deterministic part is unit-tested; nothing has yet authored a rule against a live prover |

**The loop is not a loop.** Worth recording because it is the most misleading thing about §7.5's own
wording: "author → compile → build → prover → analyze → revise" describes what the *agent* does, not
this code's control flow. There is no attempt counter. The graph runs until the agent publishes or
gives up, and the gate is a **digest stamp** — a prover run and a judge acceptance each stamp the hash
of the draft they saw, and `result` is refused unless both stamps equal the hash of the draft as it
now stands. An edit after a green run therefore invalidates that run with nothing having to remember
to clear it. That machinery is `composer/authoring/`'s and predates this backend.

**Two tiers, one stamp.** `cargo_check` is seconds and free and is *not* a required stamp; only
`verify_rules` is. That is not laxity — a prover run builds first and returns the compiler's message
without submitting when the build fails, so requiring both would gate one fact twice while making the
cheap tool feel like a formality. What the prompt does instead is tell the author to run the cheap one
after every edit, which is where it pays.

**"Accounted for", not "all green".** A rule the author has marked with `expect_rule_failure` is
excluded from the gate. This is the one place the CVLR backend deliberately diverges from foundry's
instinct, and the reason is incentive: a gate that demanded green would push the author to weaken a
rule until the bug disappeared. Marking makes the finding an explicit, recorded claim, and the gate
also reports the opposite case — a rule marked expected-to-fail that *verified*, which means either
the defect is not there or the rule does not test for it.

**Ground truth from the draft, not from the run.** `validate_check_mapping` gets the both-direction
check that forge's caller gets — no property claiming a rule that does not exist, no rule left untied
to a property — without waiting for a submission, because both CVLR declaration forms name their rules
deterministically (§7.5.3). It therefore also works in the budget wrap-up window, where no run is
available at all.

#### 7.5.2 Open question 3 is answered, and the answer is forced

**Each unit gets its own workdir.** Not a preference — the compile gate is whole-crate. `cargo check`
on the package compiles *every* unit's harness module, so in a shared workdir unit A's gate fails
whenever unit B's draft is momentarily broken: a gate that fails for another unit's reason,
nondeterministically. That is worse than a slow gate, because it is not reproducible.

Three alternatives were considered and rejected:

| Alternative | Why not |
|---|---|
| One workdir, a run-wide lock around stage-and-check | The lock does not help: the *sibling file* is still broken on disk while A checks. Emptying siblings first makes concurrent units clobber each other |
| A `--cfg` or cargo feature per unit, so each build compiles only its own module | Sound, and cheap on disk. But feature- and flag-varying builds get separate fingerprints anyway, so the cache cost is the same one it was meant to avoid — and it puts per-unit features into the target's own manifest |
| Author units sequentially | Not this backend's call; the driver batches them |

The cost is one dependency graph per unit, which promotes the **shared read-only cargo cache** §5.1
held in reserve from an optimization to something load-bearing. It is not built: the confinement
policy forces a private per-run `CARGO_HOME`, so sharing one needs a policy that grants a read-only
mount of a common cache. That is the next real performance decision on this backend.

#### 7.5.3 What building it found

**A CVLR harness names its rules deterministically, so the publish gate needs no run.** `#[rule]`
adds nothing but `#[no_mangle]`, so the symbol is the function name; `cvlr_rules!` and
`cvlr_invariant_rules!` expand to `snake_case(name) + "_" + base` with a `base_` prefix stripped.
[rules.py](../composer/spec/cvlr/rules.py) ports the crate's own `to_snake_case` rather than
approximating it, and reproduces the `rule` lists in both public examples' hand-written confs exactly
— 7 and 4 rules, no misses and no extras. That is ground truth nobody derived from this code.

**The grouping is kept rather than flattened**, which is the beginning of an answer to open question 5:
one parametric invocation is one authored construct that becomes several rules, and it changes both
prover cost and counterexample attribution. A reader of a report needs to see that six verdicts came
from one three-line invocation.

**The template fuzzer caught a real bug immediately.** The author and judge prompts had
`application_context_new.j2` — the EVM context partial — included for a Solana unit, which raises on
`context.contract`. It would have failed on the first real component and passed every test that did
not render the template. The manifest has to be regenerated for a new template to be covered at all;
that is worth knowing before trusting a green fuzz run.

#### 7.5.4 What is deliberately not here

* **No source editing.** The CVL author's ~450 lines of VFS-overlay edit machinery are for munging the
  target, which is §7.6. Neither foundry nor the Rust wheel backend has it either, and every shared
  gate degrades cleanly without it by design.
* **No counterexample analysis.** The author reads whatever `UnanalyzedCexHandler` renders — the raw
  dump. §5.3 says half of CEX handling is free because the reports are the same shape; the other half
  is §7.7, and until then the `clog!` discipline in the prompt is doing all of the work. This is the
  biggest known limiter on loop quality right now.
* **No entry point.** Nothing constructs a `CvlrBackend` yet; `console-solana` / `tui-solana` are §7.8.

#### 7.5.5 What the first live run found

[tests/test_cvlr_gate.py](../tests/test_cvlr_gate.py) drives the real backend over the Anchor vault
with real models, cargo and cloud submissions.

**The source mount earns its keep.** The tool census over one run — 30 `put_harness`, 28
`cargo_check`, 23 `verify_rules`, and 50 combined `cvlr_source_read`/`cvlr_source_search` — is
the evidence §5.5 was asking for. (Those `verify_rules` calls all *failed*, for the reason
below; the census measures where the author chose to spend turns, not what came back.) The author checks
helper names against the mounted crate rather than guessing them, which was the whole argument for
putting the source in front of it.

**Recursion exhaustion discards finished work — and this is not CVLR's to fix.** The first run set
`recursion_limit` to 100 (copied from `test_solana_gate`, where it is ample because that test runs
only the front half); all three units exhausted it, each holding a compiling draft. A budget stop
returns `Curtailed`, which preserves the draft and marks it unreliable; a `GraphRecursionError`
propagates and the draft is lost. Same class of event, opposite handling — and **no backend anywhere
catches `GraphRecursionError`**, so this is pre-existing shared behaviour. Fixing it in this backend
alone would make CVLR diverge from every other for no principled reason; it belongs to
`run_to_completion`'s callers as one change.

**Units share no knowledge, and the isolation that forces is not free.** Each unit independently
rediscovers how to invoke the program at all — one had a working `crate::entry` dispatch with the
right Anchor discriminator while a sibling was still probing with `cvlr_assert!(true)`. §7.5.2's
per-unit workdir was forced by compile-gate correctness, but it isolates *learning* too, and the cost
grows linearly in component count. A shared, append-only note of "how this program is driven",
written once and read by every unit, is the obvious answer and is not built.

**Every submission failed, and the cause was one no-op line.** `verify_rules` computed
`declared = rule_names(draft)`, used it only to reject an empty draft, and then submitted with
`dataclasses.replace(submission, rules=submission.rules)` — a self-assignment. So the conf inherited
from a base that names no rules, and **a Solana cloud job with no rule selection ends in `FAILED`**,
with no report and nothing on disk to read. Every submission this backend had ever made failed this
way.

Proved by construction rather than argued. On one workspace, one build, one harness of
`cvlr_assert!(true)`, the only change being the rule list:

| `rules` | outcome |
|---|---|
| `InheritRules()` (the bug) | `POSTED → QUEUED → RUNNABLE → RUNNING → FAILED`, no report |
| `SelectRules(("rule_probe",))` | `Checked`, `rule_probe: VERIFIED` |

The control that isolated it is [test_cvlr_end_to_end.py](../tests/test_cvlr_end_to_end.py): the
public examples pass in 71 seconds on the same machine, credentials and CLI. Their conf names its
rules explicitly, which is exactly the difference.

`SelectRules`, not `AllRules`: the build is whole-crate, so "every rule the artifact declares" is
every *unit's* rules, and a unit would be graded on its siblings' drafts.

**Two findings this run appeared to produce were symptoms of that one bug, and are retracted.**
Recording them because the mis-diagnoses were confident and each cost real time:

* *"`sources` is not consumed by certora-cli 8.18.0."* Wrong about the cause, right that something
  was broken — and the retraction itself then over-corrected, so this took three passes to land.
  `sources` **is** consumed and the `**` glob **does** expand. What suppresses it is where this
  backend put the per-unit workspace: the collector skips paths under `.certora_internal`, and
  `WORK_DIR` was `.certora_internal/cvlr/work/<unit>`, so the whole project under verification sat
  inside a directory it refuses to walk. One project, one warm build, one `SelectRules`, moved
  between two paths: **7 `.rs` from a plain path, 0 from under `.certora_internal`** — and the job
  VERIFIED either way, which is why nothing complained. `WORK_DIR` is now `.cvlr_work`. What
  remains upstream is one row: `Cargo.toml` is collected for neither project, per §7.4.3.
* *"Without CEX analysis the loop burns its budget on tautologies."* The behaviour was real — all
  three units wrote competent 8–10 rule harnesses and collapsed to probes, one of them
  `cvlr_assume!(x > y); cvlr_assert!(x != y)`, which does not touch the program — but the stated
  cause was wrong. They never received a counterexample to fail to interpret. They received
  `"status FAILED"` and simplified the harness looking for something the submitter would accept.
  §7.5.4's claim that missing CEX analysis is the biggest limiter on loop quality is **untested**:
  no run has yet reached the point where it would bind.

The general lesson, and the reason both mis-diagnoses landed: when the first thing in a pipeline is
broken, every later stage reports its own starvation, and each of those reports looks like an
independent defect. The tell was available and I passed it — a job link existed for every one of
those failures, and [core.py](../composer/prover/core.py)'s message discarded it while
`CloudJobError` carried it. That is now fixed too: a failure names its job.

**The first published harness verifies a replica of the program, not the program — and every gate
passed it.** Run 4 met §7.5's exit criterion: `deposit_management` published five properties mapped
one-to-one onto five rules, with both stamps on one draft digest. What it published does not verify
the vault.

Every rule calls `deposit_logic`, a ten-line reimplementation of
`vault.balance.checked_add(amount)` written inside the harness. Nothing calls `crate::entry` or
`vault_program::deposit`; the harness's only reference into the crate is `crate::VaultState`, a
*type*. So the rules establish that the author's own copy of the logic has these properties, and
would pass unchanged if the real handler disagreed. Two of the five are weaker still:
`rule_deposit_open_access` proves the depositor cannot influence the result *because `deposit_logic`
takes no depositor parameter* — the function was written without it and then proved not to need it —
and `rule_deposit_overflow_must_fail` proves that `u64::checked_add` returns `None` on overflow,
which is a test of the standard library.

All five VERIFY. All five pass vacuity checking, because they are not vacuous — there is real content
being proved, about the wrong program. All five pass the mapping gate and the accounting gate.

**This is a third gate gap and it is not the vacuity gap.** Sanity catches "assume the conclusion";
nothing catches "verify a copy instead of the original", and no verdict-shaped check can, because the
verdicts are honest. The missing check is about *what the rule touches*: a rule is evidence about the
program only if its call graph reaches the program's own code. Two ways to get it, neither built:
statically, refuse a draft in which no rule reaches a function defined outside the harness module; or
from the run, require the program's code in the job's coverage.

**Its cause is a blocker larger than the inlining gaps.** Both units that got that far reported prover
error **[3006] "illegal store of a stack pointer"**, attributed to Anchor 0.31's
`Error::AnchorError(Box::new(e))`, which lies on every path through Anchor dispatch — `entry()`,
`try_accounts()`, `Account::try_from()`. If that holds, an Anchor program cannot currently be
*invoked* under the prover at all, which is a stronger claim than any property being unprovable. It
also means a reach-the-program gate added today would convert this backend's output from "confidently
wrong" to "always empty" on Anchor targets. Both changes are needed and the gate is the safety
property, but the order matters: **[3006] first.**

Worth recording in the backend's favour: the author was transparent. The replica, the reason for it,
and the skipped property are all documented in the module header and the published commentary. It did
not hide the weakening — nothing asked.

**An ampersand in a component name failed every submission for two of three units.** The `msg` a run
sends is built from the component's display name, and `certoraRun`'s `validate_msg` *raises* on any
character outside letters, digits and a short punctuation list — before a single rule is read. The
components were named "Deposit & Balance Tracking" and "Withdrawal & Fee Distribution".

The cost is the clearest measurement this phase has produced of the unfixable-error pattern. One unit
spent 6 submissions on it, the other 13+, and the second reported:

> The harness is complete (10 rules covering all 10 properties), compiles successfully, and has been
> accepted by the feedback judge. Three rules (properties 8, 9, 10) are marked as expected failures
> for genuine program defects.

A finished ten-rule harness with three claimed findings, lost to one character. Both authors
diagnosed it exactly and both said the same thing — the name is not in the harness, so they could not
fix it. That is the third instance in this phase of an error an author cannot act on consuming its
whole budget (the others: the platform generation gate, §7.4.4, and the module name, §7.5.5), and it
is worth stating as a design rule: **anything the pipeline derives from model-written prose and hands
to a validating tool must be sanitized at that boundary, because the agent cannot reach it.**

`safe_msg` reduces the name to a deliberate *subset* of the CLI's accepted set — a subset so that a
future narrowing upstream stays valid without an edit here, and the test asserts the subset relation
rather than round-tripping examples. The first cut of it included `;`, which the CLI does not accept;
only the subset check caught that.

**A blocked author assumes the conclusion, and the publish gate cannot tell.** The most substantive
thing the run produced was a diagnosis the author wrote into its own harness: Anchor's
`Account::exit` is not inlined, so handler state changes are never written back observably, and
`find_program_address` is summarized, so a PDA cannot be re-derived across calls. Checked against
the vendored tuning files, three of its four claims hold exactly:

| claim | verdict |
|---|---|
| `Account::exit` / `AccountSerialize::try_serialize` not inlined | holds — absent from the `#[inline]` allowlist |
| `invoke_signed_unchecked` summarized, so `init`'s account-creating CPI is a no-op | holds — `cvlr_summaries_core.txt:95` |
| `find_program_address` summarized | holds — `cvlr_summaries_core.txt:84,92` |
| a blanket `#[inline(never)] ^.*anchor_lang.*$` is what excludes them | holds — `cvlr_inlining_anchor.txt:2`, the file's first directive |

All four hold, and this table said otherwise until §7.5.6 went looking: the fourth row was recorded
as **wrong** — "no such directive" — on a reading of the file that missed its opening line. The
correction matters more than the row does, because it inverts the shape of the fix. Anchor is
`#[inline(never)]` by default with a named allowlist of exceptions, so the lever is *subtracting from
the allowlist*, not adding a suppression; and the allowlist is therefore an exact statement of which
`anchor_lang` code the prover ever analyzes.

Row two wants a second warning: it is true of the file and false of the target. The
`invoke_signed_unchecked` summary is present exactly as cited and does not *apply* here, because the
path it names was renamed out from under it — so that CPI is analyzed in full rather than
approximated, the opposite of what the row implies. Row three survives, but for a reason outside it:
`find_program_address` is summarized on this target only because upstream added a second block under
the split spelling (§7.5.6). A directive checked by reading the file it lives in has been confirmed
to exist, not to take effect. **On an Anchor program this puts most post-state
properties out of reach**, which is most of the Solana market.

What the author then did is the finding. It kept one rule:

```rust
cvlr_assume!(*accounts[0].key == expected_pda);
...
cvlr_assert!(vault_key == expected_pda);
```

That assumes its conclusion. It VERIFIES, it maps to a property, and it passes **both** halves of the
publish gate. This is not a hole in the mapping check — it is the direct consequence of §7.5's
"accounted for, not all green", which exists so a genuine violation need not be smoothed away and
therefore gives the gate no notion of rule *strength*. A blocked author will always find this move,
because it is the only one that always works.

The fix is vacuity checking, which both public examples enable and `TEMPLATE_BASE` had omitted:
`rule_sanity: "basic"`. `SANITY_FAILED` is already a first-class parsed status and is not
`VERIFIED`, so it reaches the author through the accounting gate as unfinished work. Everything
needed was present; only the conf key was missing.

The honest end state is worth recording too: given a full budget the author did **not** ship the
tautology. It skipped all six properties with the diagnosis above as its reason, which is what the
skip mechanism is for and the right answer when a batch genuinely cannot be verified. The vacuity fix
matters for the case where an author is blocked and does not notice.

Three follow-ups this opens, none done: the vendored tuning needs `exit` / `AccountSerialize`
inlined and needs `invoke_signed_unchecked` to model account creation before Anchor post-state
properties are provable at all — a prerequisite for this backend being useful on Anchor rather than
a refinement — and
`cvlr_inlining_anchor.txt` line 39 carries an inlining rule naming one specific program, which is
upstream's to remove from a file that claims to be canonical.

**The mount covers CVLR's API but not the target's generated one.** A draft named
`crate::__client_accounts_withdraw::WithdrawBumps`, an Anchor-generated path. §5.5 mounts the CVLR
crates and the source tools expose the target's own code, but a harness must also name what the
target's *macros* generate — `Accounts` structs, `Bumps` types, the discriminants — and nothing puts
that surface in front of the author. `cargo expand` output, or the Anchor crate source, would.

#### 7.5.6 Addressing [3006]

The attribution on record — Anchor 0.31's `Error::AnchorError(Box::new(e))` — survives inspection as
a description of the code and fails as an explanation. `impl From<AnchorError> for Error` is
byte-identical in anchor-lang 0.29.0, 0.30.1 and 0.31.1 (`src/error.rs:242` / `:296` / `:296`), and so
are its three siblings. Whatever separates our runs from a corpus of projects that verify Anchor
programs, it is not that construct's existence. Something else moved, and it is findable without
spending a submission: build the scenario for SBF, demangle the symbol table, and ask which of the
vendored tuning directives match anything at all.

That build takes 45 seconds and needs no cloud time. Its answer:

**Fourteen tuning directives have no working spelling on this target, and the blanket that sets the
default for the whole platform layer is one of them.** The scenario resolves `solana-program 2.3.0`,
where `account_info` is `pub use solana_account_info::{self as account_info, …}` (`src/lib.rs:587`)
and `pubkey`, `rent`, `clock`, `program_error` and `program_pack` are aliases of the same shape.
Demangled Rust symbols carry the *defining* crate path, so a directive anchored on `solana_program::…`
cannot match. The split landed at **solana-program 2.2**, not at v3: 1.17 and 1.18 have a real
`account_info` module and 2.2.1, 2.3.0 and 3.0.0 all re-export. The reference set pins
`solana-program 2.2` as its platform, so the generation it names is already post-split while the
vendored files are written pre-split.

The headline is `cvlr_inlining_core.txt:7`:

```text
#[inline(never)] ^solana_program::.*$
```

Two symbols in `vault.so` match it — `solana_program::program::{invoke, invoke_signed}` — and nothing
else. Every split crate (`solana_account_info`, `solana_pubkey`, `solana_cpi`, `solana_rent`, …) falls
outside it, so for the entire platform layer **the default silently flipped from never-inline to
inline**. That is a larger change than any individual dead exception: upstream's file is a blanket
suppression plus a named allowlist, and on this generation the blanket covers almost nothing while the
allowlist entries that would have carved out the parts that must be inlined are themselves dead.

| directive | names | symbol emitted |
|---|---|---|
| `cvlr_inlining_core.txt:7` | `solana_program::.*` (blanket never-inline) | matches 2 of the platform layer's symbols |
| `cvlr_summaries_core.txt:95` | `solana_program::program::invoke_signed_unchecked` | `solana_cpi::invoke_signed_unchecked` |
| `cvlr_summaries_core.txt:60` | `…account_info::AccountInfo::realloc` | `solana_account_info::AccountInfo::realloc` |
| `cvlr_inlining_core.txt:116–122, 129` | the eight `AccountInfo` data / lamports accessors | `solana_account_info::AccountInfo::…` |
| `cvlr_inlining_core.txt:133–134, 138` | `Rent::minimum_balance`, `Sysvar for Rent`, `ProgramError::from` | `solana_rent::`, `solana_sysvar::`, `solana_program_error::` |
| `cvlr_inlining_anchor.txt:37` | `…From<solana_program::program_error::ProgramError>>::from` | `…From<solana_program_error::ProgramError>>::from` |

**Upstream has met this and patched two symbols.** `cvlr_summaries_core.txt` carries duplicate blocks
for `solana_pubkey::Pubkey::create_program_address` (`:76`) and `find_program_address` (`:92`)
alongside the `solana_program::` originals — the only two split spellings anywhere in the four files.
So those two summaries *do* apply here, upstream's method was to duplicate the block that bit them,
and the other fourteen were never revisited. That is corroboration rather than a counterexample: the
problem is known at exactly the two places somebody hit it.

It also settles two of §7.5.5's rows against each other, and against what this section first claimed.
`find_program_address` **is** summarized on this target, so the author agent was right and the first
draft of this section was wrong to invert it. `invoke_signed_unchecked` is **not** summarized — it has
no twin — so the agent's reading of the file was right and its conclusion ("`init`'s account-creating
CPI is a no-op") was wrong in the other direction: nothing approximates that call, and the prover
analyzes it in full.

Which makes it the sharpest candidate for [3006] that this investigation has produced. Signer seeds
arrive as `&[&[&[u8]]]` — an array of arrays of pointers, built as stack locals by the caller and
marshalled by the callee into a buffer for the syscall. Writing those element pointers into memory is
literally storing stack pointers, `solana_cpi::invoke_signed_unchecked` is neither summarized nor
excluded from inlining here, and the vault's `init` reaches it through Anchor's account-creating CPI.
Nothing in the corpus predicts this because every entry is provenanced
`platform_generation: solana-program 2.x (the last monolithic line)`: the whole body of practice
predates the split, and the regexes were correct when they were written.

**The fix, and what shape it takes.** Not a code change and not a flag: a path rewrite. Two designs
were on the table — patch the vendored files to carry both spellings as alternations, or keep the
directives keyed on *concepts* and emit the paths for the target's generation. The second is what is
built, because the first is wrong again at the next split and this is already the second defect in two
phases traceable to this one.

So the vendored files stay byte-identical to upstream and become the **concept keys**, and the
platform generation says how to spell those concepts:

* [`cvlr_reference.py`](../composer/spec/cvlr_reference.py) gains `PathAlias` — a canonical path
  prefix and this generation's spellings of it — and `NamespacePattern` for the one directive that is
  a blanket rather than a path. `PlatformGeneration.path_aliases` is where Solana's twelve are
  declared.
* [`env_paths.py`](../composer/spec/cvlr/env_paths.py) is the seam: `dialect_for(workspace,
  reference)` resolves the declarations against the target's own graph, and `PathDialect.render`
  rewrites a tuning file line by line. The scaffold threads a dialect into `canonical_env` and
  `compose_env`, and the composite says in its header that paths were rewritten — the alternative is
  a reader diffing it against upstream and concluding somebody hand-edited it.

Four properties of the design are load-bearing, and each of them is something a naive substitution
gets wrong:

* **A concept can have more than one spelling.** `solana-program` kept a real
  `invoke_signed_unchecked` while the one on the call path is `solana-cpi`'s, so that summary is
  emitted twice. A rewrite that picked one would leave the other fully analyzed — the exact state
  under investigation.
* **A spelling only counts if the target resolves the crate.** Aliases are declared against the
  newest generation and dropped when the crate is absent, so applying the dialect to a 1.18 target
  returns one that changes nothing. No version condition, no second gate to keep in sync.
* **Longest canonical wins.** `solana_program::program` is a *partial* facade — `invoke`,
  `invoke_signed`, `set_return_data` and `get_stack_height` are real functions there and appear in
  the symbol table under the canonical spelling — so the module gets one answer and the one symbol
  that disagrees gets another. Deciding per module rather than per symbol produced two wrong answers
  during this investigation before the symbol table settled it.
* **The blanket is widened, not renamed.** `^solana_program::.*$` becomes
  `^solana_[a-z0-9_]*::.*$`: a superset, so it is correct on either generation and cannot go stale
  when the next crate splits out. Widening is only safe because the directive is a *suppression* —
  over-matching a never-inline default errs toward analyzing less library code.

Deliberately not aliased, and recorded so the absence reads as a decision:
`solana_program::instruction::get_stack_height`, real in the monolith on this generation, and
`solana_program::poseidon`, which the generation does not have under any spelling. No rewrite makes an
absent symbol present, and pretending otherwise would hide that the directive is inapplicable.

Measured against the checked-in symbol fixture, directives that match something in the binary go
4 → 15 (core inlining), 6 → 7 (anchor inlining) and 2 → 6 (core summaries); symbols reached go 6 → 35,
26 → 26 and 2 → 4, with **no symbol losing coverage**. The anchor row is why both counts are pinned:
its revived directive sits inside the `^.*anchor_lang.*$` blanket, so it changes how a symbol is
treated without changing whether anything reaches it.

The tests are [`test_cvlr_env_paths.py`](../tests/test_cvlr_env_paths.py), and two of them exist
because of how this defect fails. One asserts every declared alias still names a directive that
appears in the vendored files, so an upstream refresh that removes one turns dead weight into a
failure instead of leaving it looking like coverage. The other pins the alias table against
[`tests/data/vault_sbf_symbols.txt`](../tests/data/vault_sbf_symbols.txt), 70 demangled symbols from a
real Anchor program built for SBF — the only test here that can catch an alias being *wrong* rather
than merely stale, which matters because both failures are equally silent.

**Confirmed, and it is not the path rewrite.** The probe is
[`test_cvlr_anchor_reach.py`](../tests/test_cvlr_anchor_reach.py) with the harness in
[`anchor_reach_probe.rs`](../tests/data/anchor_reach_probe.rs): three hand-written rules against the
scaffolded vault scenario, graded by how much of Anchor they traverse so that a failure names a
boundary instead of merely happening. Hand-written because this phase has three recorded instances of
an error the author cannot act on consuming its whole budget — a loop run would measure the loop.

The prover names its own source, which settles the question the file reading could not:

```
[3006] illegal store of a stack pointer
source: .../rust/library/alloc/src/boxed.rs:218
  from: anchor-lang-0.31.1/src/error.rs:296     <- Self::AnchorError(Box::new(ae))
  from: anchor-lang-0.31.1/src/error.rs:21      <- #[error_code(offset = 0)] pub enum ErrorCode
help: A stack pointer escapes by being stored into an illegal memory segment
Dev: Pointer domain: stack is escaping: Node[Stack:RX,16256) into Node[Heap:X,112)
```

So **`Box::new` was the cause, and this section's first draft was wrong to rule it out.** The argument
it made — that the construct is byte-identical in anchor-lang 0.29, 0.30 and 0.31, so it cannot be
what separates our runs from a working corpus — was a bad inference twice over. The version was never
the variable; and "a working corpus" was an assumption, not an observation. The two `from:` lines are
exactly `cvlr_inlining_anchor.txt:35` and `:36`, the allowlist entries that force-*inline* those
conversions so the prover analyzes their bodies at all. The original agent diagnosis was right and
this document argued it down twice: once by mis-reading the file, once by reasoning about versions.

The path-rewrite work above stands on its own measurements and is a **separate** defect. It was not
this one.

| rule | traverses | result |
|---|---|---|
| `rule_vault_state_deserializes` | `Account::try_from`, borsh, arithmetic on real account data | **VERIFIED** |
| `rule_deposit_credits_exactly_the_amount` | the handler and its account-creating CPI | ERROR [3006] |
| `rule_dispatch_is_reachable` | `crate::entry`, the whole dispatch path | ERROR [3006] |

The boundary is sharp and it is not where the earlier diagnosis put it. Reading and deserializing
real account data works, and post-state arithmetic over it verifies. What fails is **any path that can
construct an Anchor error** — which is why the control tier passes despite also calling
`Account::try_from`: it uses `.unwrap()`, and a panic is assumed away by `-solanaAssertOnPanic false`,
while `deposit`'s `?` actually builds an `Error`.

**Three workarounds, all ruled out by measurement.**

* *Drop the two `#[inline]` entries* so the blanket leaves the conversions un-inlined. The prover
  starts and exits after **3.6 seconds** with `jobStatus: FAILED`, no `errorString` and no output —
  reproduced twice. That is a second upstream defect, distinct from [3006] and arguably worse, since
  it discards the configuration rather than reporting a problem with it.
* *`-solanaOptimisticJoinWithStackPtr`.* No effect. Which is what the mechanism predicted: the flag
  concerns *joining* paths whose stack pointers diverge, and [3006] is a *store*. Recorded because the
  corpus entry's own `unknowns` asks what the absent flag's failure mode looks like, and the answer
  is that it is not this.
* *Summarize both conversions* in `cvlr_summaries_anchor.txt`, the layer upstream ships empty —
  `Error` is a 16-byte enum, so an 8-byte tag and a `ptr_heap` at offset 8. No effect, and the PTA
  node identifiers come back byte-identical to the baseline run (`Node605`/`Node620`), which is proof
  the summary was never applied rather than applied and insufficient.

**Two side findings, both worth reporting upstream.** `solanaOptimisticJoinWithStackPtr` is not a conf
attribute on certora-cli 8.18.0 — it is rejected as "not a known attribute" before upload and must be
passed through `prover_args` — so the corpus entry showing it as a top-level conf key is transcribed
wrong, as are the surveyed confs it was drawn from, unless an older CLI accepted it. And the
un-inlining crash above.

**So [3006] is not reachable from the configuration surface this backend controls.** `Box::new` of a
struct built on the stack is ordinary Rust, the prover's pointer analysis rejects it, and every Anchor
program constructs an error that way on every failing path. That leaves three options and only one of
them is ours:

* **File it upstream**, which is now a crisp report: a minimal reproducer, the exact source line, the
  prover's own dev message, and three attempted workarounds with evidence that none works. This is the
  highest-value action and it is not a code change.
* **Munge `anchor-lang`** through `[patch.crates-io]`: a variant of `Error` that does not box under a
  `certora` cfg. Heavier than §7.5.6's first draft estimated, and now the only workaround inside our
  reach — which promotes Phase 5 from "parallel with 4" to the thing Anchor support depends on.
* **Scope the backend to what verifies.** Weaker but not nothing: the control tier shows real account
  data, deserialization and post-state arithmetic all work. A rule that avoids error-constructing
  paths is real evidence about a real program. It is not most of what a client wants.

The probe test asserts all three tiers verify, so it is **red today on purpose** — the same choice
§7.5.5's gate makes. A test that encoded the current breakage would go green on a run that fixed
nothing.

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
| Setup | Templated preflight; no AutoSetup — and it turned out to need no agent at all (§7.4.1) |
| Authoring gate | Two checker tiers, one stamp: the cheap compile is ungated and the prover run is the gate, because a prover run builds first (§7.5.1) |
| Rule granularity | Both forms offered, and the *generated* names are the unit of attribution; a parametric invocation stays grouped in the record (§7.5.3) |
| Workdir lifetime | Per unit, forced by the whole-crate compile gate rather than chosen (§7.5.2) |
| Scaffold shape | From `Certora/solana-spec-template`, the recommended starting point, not from a vote over client layouts; the public examples break ties about what actually runs |
| Scaffold safety | Never overwrite, compute every manifest change from the parsed manifest, and refuse rather than guess where a decision changes how the project builds for everyone |
| Env (inlining/summaries) files | Vendored with a provenance stamp naming the upstream commit, in the template's three-layer split so the project's own layer is never a canonical file |
| RAG | A single `cvlr_kb` corpus with chain-tagged sections, fed by three manifests — published docs, generated crate reference, project-derived practice — all produced in and shipped from a separate private repo, because *build cost* rather than confidentiality is what decides where a producer lives ([capture plan](./cvlr-capture-plan.md) §8.2) |
| CVLR source | Mounted read-only as its own tool set (not a project-VFS layer), version resolved by the host from `cargo metadata`, available to the code explorer and the author, and named as authoritative in the prompt |
| Ship order | Solana end-to-end first; Soroban pack after |

### Open — to close during Phase 1

1. ~~**Is a host-target `cargo check --features certora` a faithful proxy for the SBF build?**~~
   **Answered, for the case the question was about** (§7.4.2). §5.1 measured both tiers against a
   fixture whose `certora` feature was empty; the scaffolded project's enables `no-entrypoint` and
   two optional CVLR crates, and the host-target check accepts it, rejects a deliberate error inside
   the harness, and confirms the subtree compiles away without the feature. What a host check still
   cannot catch is what only the SBF target can, which is why the slow tier remains the
   pre-submission gate rather than an optimization.
2. ~~**Conf style: `[package.metadata.certora]` from-sources, or explicit pre-built `.so`?**~~
   **Settled: from-sources, through a build script the backend owns** (§7.2.2). The capture survey
   found `build_script` in 15 of 16 projects and `[package.metadata.certora]` with exactly three
   keys, and the implementation found the harder half of the reason: the `files` path collects no
   Rust sources into `.certora_sources`, and the CLI's own from-sources path builds unconfined.
3. ~~**Warm-workdir lifetime**~~ **Answered: per unit, and forced** (§7.5.2). The compile gate is
   whole-crate, so a shared workdir makes one unit's gate fail on another unit's half-written module —
   nondeterministically. The follow-on is no longer a question but a task: the shared read-only cargo
   cache is now load-bearing rather than an optimization, and needs a confinement policy that grants
   a read-only mount of a common cache. One constraint still holds: a workdir may not switch
   confinement posture mid-run without losing its incremental cache (§5.1).
4. **Where the munge give-up boundary sits**, in edits or in wall-clock.
5. **Rule granularity per property** — one `#[rule]` per property, or parametric rules /
   `cvlr_rules!` batching. *Partially answered* (§7.5.3): both forms are offered, the prompt says to
   prefer parametric when one property spans handlers, and the *generated* names are what the mapping
   and the report speak — so attribution is per generated rule while the record keeps the grouping.
   What is still open is whether the prover cost actually favours it in practice, which needs runs.

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
