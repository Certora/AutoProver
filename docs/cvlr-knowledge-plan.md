# Plan — CVLR knowledge: where it belongs, and the Solana property corpus

> Two pieces of work, one document, because the second only makes sense once the first is settled.
>
> **Part I** (AutoProver, **done**) records where CVLR *practice* knowledge lives — a knowledge
> bundle and a recipe set, written by hand — and the rules for drawing on verification engagements
> when writing it. It replaces the capture pipeline that was going to generate that knowledge into
> the `cvlr_kb` corpus, and the private repository that pipeline ran in, `certora-cvlr-kb`, which is
> being retired.
>
> **Part II** (**certorag**, not started) extends the property corpus — "what did we prove about a
> component like this one" — from CVL/Solidity to CVLR/Solana. This is the one knowledge asset on
> the CVL side with no CVLR counterpart at all.
>
> Related: [cvlr-api-docs-plan.md](./cvlr-api-docs-plan.md) (the two CVLR corpora, which hold
> reference material rather than practice), [ecosystem-abstraction.md](./ecosystem-abstraction.md)
> (the seam Part II rides on), [cvlr-backend-plan.md](./cvlr-backend-plan.md) (the consumer),
> [cvlr-todo.md](./cvlr-todo.md) (the leads the capture survey left, and U6). Paths below are
> relative to the repository root unless marked `certorag:` or `certora-cvlr-kb:`.

---

## 0. The one-paragraph version

On the CVL/Solidity side, exactly two knowledge assets are machine-derived: the CVL manual (sphinx →
`ragbuild`) and certorag's property corpus. **Every piece of CVL *practice* knowledge is
hand-authored**, and it is delivered as an always-in-context bundle plus a trigger-indexed recipe
set, not as retrievable corpus prose. CVLR now has the same shape: `cvlr_baseline_facts.md` in a
CVLR bundle, read by every CVLR agent that can act on it, and two recipes. Corpus search holds only
reference material — the manual and the generated API — because a corpus is allowed to be absent
at run time, and anything load-bearing must be in the prompt or the bundle. What CVLR still lacks
is a property corpus, which is Part II.

---

# PART I — Where CVLR practice knowledge belongs (done)

## 1. The CVL knowledge stack

`composer/kb/kb_context.py` assembles the CVL context documents and injects them into an agent's
*initial prompt* via `with_cvl_context`, behind a `CacheMarker`:

| Layer | Artifact | Delivery |
|---|---|---|
| Baseline facts | `composer/kb/resources/cvl_baseline_facts.md` | always in context |
| Topic KBs | `cvl_summarization_rag_draft.md`, `cvl_invariants_quantifiers.md` | always in context |
| Situation recipes | `cvl_recipes_index.yaml` + bodies under `recipes/` | **index** always in context; body on demand via `get_cvl_recipe` |
| Backend guidance | `CERTORA_BACKEND_GUIDANCE` in `composer/spec/prop_inference.py` | cached system prefix of property inference |
| Reference manual | sphinx → `composer/scripts/ragbuild.py` → RAG db | searched |
| Property corpus | `certorag` | analogy probe at property inference |

Two facts about it decided Part I. The practice layers were written by an expert in one commit
(`b6320237`); there is no extraction pipeline behind them, no ranking and no ledger. And the
bundle is shared: `with_cvl_context` has seven call sites in this repo and an eighth in certorag.
The knowledge is a *bundle*, deliberately separable from any one agent's prompt.

## 2. The CVLR side, as built

| Artifact | What it covers |
|---|---|
| `composer/kb/resources/cvlr_baseline_facts.md` (396 lines) | What a rule is; assumptions and vacuity; nondeterminism as the quantifier; loop bounds and the `optimistic_loop` ladder; arithmetic and why panic-freedom is off the menu; the nonlinear ladder; `clog!` legibility; when the Prover cannot follow the program's own data. Factored out of the author's system prompt (§4.2). |
| `composer/kb/resources/cvlr_recipes_index.yaml` + `recipes/cvlr-k*.md` | Two recipes, K1 and K2 (§4.3) |
| `composer/templates/cvlr_property_generation_system_prompt.j2` (479 lines) | The author's own contract: output shape, its tools, the publish stamps, the workflow, Anchor entry points and the worked example |
| `composer/spec/cvlr/guidance.py` — `SOLANA_CVLR_GUIDANCE` | `backend_guidance` for property inference, written from the manual (§4.4) |
| `composer/spec/cvlr/envs/` | The starting layers of the two tuning files, maintained in AutoProver since `596ec0de` |
| `composer/templates/cvlr_munge_editor_system.j2` / `_review_system.j2` | The munge editor's charter and its reviewer |
| `composer/tools/cvlr_rag.py`, `composer/tools/cvlr_api_rag.py`, `composer/spec/cvlr_research.py` | Reference, not practice: the manual (`cvlr_kb`), the API generated from the crates (`cvlr_api_kb`), and the research sub-agent the author and the judge reach both through ([cvlr-api-docs-plan.md](./cvlr-api-docs-plan.md)) |

## 3. Why the capture pipeline's entries did not become corpus content

The capture survey of the Solana engagements produced 83 abstracted entries: 27 `rule_shape`, 18
`prover_flag`, 17 `conf_key`, 11 `env_inlining`, 6 `munge_form`, 3 `env_summaries`. They were
headed for `cvlr_kb`. Against the CVLR side as it already stood:

- **`prover_flag`, `conf_key` (35)** — outside the author's action space. `adjust_prover_config`
  moves the loop bound and `optimistic_loop`, nothing else, and the run's actual conf is injected
  into the prompt verbatim.
- **`env_inlining`, `env_summaries` (14)** — restating the starting layers. The real problem there is
  path spelling across platform generations, which `env_paths.py` owns. One exception became lead L1
  in `cvlr-todo.md`.
- **`munge_form` (6)** — owned by the munge editor's charter.
- **`rule_shape` (27)** — the only candidates, triaged into recipes (§4.3).

It is what happens when a machine pass and an expert work the same seam in parallel and the expert
finishes first. The evidence was good; the entries were the wrong output shape.

## 4. Decision

**CVLR practice knowledge is written by hand and delivered through the bundle and the recipes, not
through corpus search.** Corpus search holds reference material only: `cvlr_kb` is the Solana
manual and nothing else (`26f7b9c9`), and `cvlr_api_kb` is the API generated from the crates. The
capture pipeline's lasting output is evidence for whoever writes the knowledge, and what survives of
it is the list of leads in `cvlr-todo.md`.

### 4.1 The bundle (`with_cvlr_context`)

`kb_context.py` was generalized into a `KnowledgeBundle[C]` record (`2ffc1243`): a label, its
`ContextSpec`s, and an optional `RecipeSet[C]` (tool name, index resource, parser, index template).
The channel vocabulary is a type parameter, so a CVLR recipe cannot name a CVL channel. Both
families share one index template, `kb_index.j2`, and `kb_tools(bundle)` builds either family's
recipe tool. The one change CVL agents see is that the channel legend now renders with the index.

Each CVLR reader gets what it can act on, which is why there are two bundles:

- **`with_cvlr_context`** (`CVLR_BUNDLE`, the facts plus the recipe index) — the author and the
  judge, the two agents holding `get_cvlr_recipe`. The recipe tool is added in
  `composer/spec/cvlr/entry.py`.
- **`with_cvlr_facts`** (`CVLR_FACTS_BUNDLE`, the facts alone) — the munge reviewer and
  `cvlr_research`, which cannot retrieve a recipe.
- **Nothing** — the munge editor. Its charter forbids reasoning about the Prover's internals, which
  are the bundle's subject.

The counterexample analysis needs no wiring of its own: it runs over the author's message list, and
the bundle is at its head. The bundle is a cached prefix, so the per-run material — the injected
conf, the worked example and the Anchor section it runs through — stays in the author's prompt.

### 4.2 Populating it: the author's prompt, factored

The bundle was not written from scratch or from the entries. The author's 754-line system prompt was
split along one line (`f4c953fd`): this agent's contract stays in the prompt, and CVLR and Prover
facts true for any CVLR agent move to the bundle. 388 lines moved. The judge now cites the bundle's
sections instead of restating them, and a test checks each cited heading against the bundle
(`tests/test_cvlr_knowledge.py`, `tests/test_cvlr_bundle.py`).

### 4.3 Recipes, with the channels taken from the action space

A channel tells a stuck agent whether the fix is in its action space, so the channels are the
author's tools (`e04f2dea`), as `CvlrChannel` in `kb_context.py`:

| Channel | Tool | Note |
|---|---|---|
| `RULE` | `put_harness` | the default |
| `MOCK` | `summarize_for_prover` | unsound by construction; invalidates the prover stamp |
| `EDIT` | `code_editor` | the agent describes the problem, not the edit |
| `CONF` | `adjust_prover_config` | **only** the loop bound and `optimistic_loop`; anything else is a `record_skip` |
| `SKIP` | `record_skip` | the honest terminal channel, with no CVL peer |

There is no channel for env files or scaffolding: the starting env files are maintained here and the
composites are generated, so a recipe whose action is "add an inlining line" names something the
author cannot do. A trigger may be a symptom, a code situation or a verification goal, as the CVL
recipes' are.

The 27 `rule_shape` entries were triaged against the factored bundle, and two survived:

- **K1** — satisfy rules for reachability and witnesses, and the acceptance property they must not
  stand in for. The prompt and the judge mentioned `cvlr_satisfy!` only as a thing not to do.
- **K2** — `clog!` before an assertion over an enum or a bitfield. Logging gives the value a use
  site, which stops the comparison chain being folded into a bitmask: a solver workaround, not a
  legibility habit.

The rest restated the bundle. One was dropped for a reason worth keeping: "end every rule with a
vacuity check" is attested but does not transfer, because `cvlr_vacuity_check!` is gated on a cargo
feature AutoProver never enables. More recipes come from what runs show the author missing; there
is no pipeline behind them.

### 4.4 Rules for drawing on engagements

Whoever writes the next fact or recipe will be reading verification engagements for evidence. The
capture survey paid for these rules, and they outlive it.

- **The manual is the reference; the projects are the evidence.** Where the published manual and a
  project instance disagree on *form*, the manual wins: it is maintained, version-scoped and public,
  and a project instance is one team's choice frozen at one date. Projects remain the only source
  for what actually goes wrong — which SDK boundary needed mocking, which property was false, what a
  timeout hunt ended in — and no manual reports that. `guidance.py` is written to this rule.
- **"Quotable" means quotable after it compiles.** The manual's own examples carried two defects that
  fail against every release (recorded in [upstream-defects.md](./upstream-defects.md)), so an
  example taken from it goes through the same compile check as any other.
- **Never quote engagement code.** Paraphrase or generalize it, whether or not the repository is
  public. The reason is overfitting, not confidentiality: a quoted block teaches one project's
  house layout, helper names and era. A synthetic minimal example retrieves and generalizes better
  than an excerpt from someone's protocol. Tutorials may be quoted if they are current.
- **Rank on two axes, because frequency alone points the wrong way.** Older projects outnumber
  current ones, so a raw count promotes a superseded idiom precisely *because* it is old.
  *Recurrence* — in how many projects does the underlying problem appear — decides whether a
  problem is worth knowledge. *Currency* — does a current-practice instance of the solution exist —
  decides whether there is a solution to state. A recurring problem with only legacy solutions is a
  gap to record, not advice to ship.
- **Count clients, not repositories.** Two repositories from one client share tooling, layout and
  house style, so a team's convention can appear twice and read as an industry idiom. Two repos from
  one client are one vote.
- **Judge currency per chain.** The Soroban projects run cvlr 0.3–0.4 while the Solana ones run
  0.4–0.6, so a 0.4 crate is current on one chain and a generation behind on the other. And
  vintage belongs to an instance rather than a project: a recent project can carry copied legacy
  patterns.
- **An unreviewed how-to and an unreviewed soundness claim fail differently.** A wrong how-to wastes
  an iteration, and the compiler catches it. A wrong soundness claim produces a proof of something
  false, and nothing downstream catches it. The second kind never enters the bundle unreviewed.
- **Gate the shape of generated content, not only its properties.** A capture manifest once passed
  schema validation, sanitization and the compile gate while 97% of its blocks were single
  characters: an array field arrived as a string and the renderer iterated it. Every gate asked "is
  this well-formed?" and none asked "is this anything at all?". `cvlr_api_docs.py` is the producer
  this now applies to.

Three facts about project files the survey found the hard way, because they silently destroyed
data before they were known:

- **Certora confs are JSON5-shaped**: `//` comments and trailing commas. A strict parser rejected
  152 of 486 surveyed confs.
- **Env files comment with `;` only.** `#` opens a directive (`#[inline(never)] ^core::.*$`), so
  treating it as a comment marker discards every entry while leaving the file count healthy.
- **Read versions from `Cargo.lock`, per crate, as (name, version, source).** The CVLR family does
  not version in lockstep, one lockfile can resolve two versions of one crate, manifest dependency
  names need not match resolved package names, and a stray `Cargo.lock.backup` will match a careless
  glob. A chain crate also implies a platform generation (`cvlr-solana` 0.5 is `solana-program` 2.2),
  which no CVLR version string shows.

---

# PART II — The Solana property corpus (certorag)

> The engagements Part II draws on are client work, and most are confidential, so they are named
> here by role — *the reference project*, *Project A/B/C* — rather than by client or repository.
> The labels are this document's own. The capture survey's project inventory lists the projects
> (§9.1, and §11 on where it has to go).

## 5. Why this is a different thing, and is not affected by Part I

| | certorag | CVLR practice knowledge (Part I) |
|---|---|---|
| Captures | **what was proven** | **how it is expressed and operated** |
| Unit | a natural-language property attributed to a component of an application | an idiom / a recipe / a baseline fact |
| Retrieved by | analogy — "this component resembles that one" | keyword, or a trigger match |
| Injected at | **property inference**, per component, via `property_inference_input_hook` | authoring, via the bundle and the recipe index |
| Ages? | no — a solvency property is a solvency property | yes — which is why the bundle needs an author |
| Contains code? | never; prose only | yes, synthetic and compiled |

certorag's spec-analysis prompt spends four paragraphs forbidding the agent from describing *how* a
property was expressed. That forbidden residue is Part I's whole subject. The two are orthogonal.

A CVLR run today gets no corpus analogies at all, while an EVM run gets them from an org's worth of
Solidity engagements. Nothing in AutoProver closes that.

## 6. Preconditions

1. **The composer pin must carry the `SOLANA` ecosystem.** `composer/pipeline/ecosystem.py` on
   AutoProver `origin/master` already defines `SOLANA = RUST ⊕ solana` with `SolanaApplication` /
   `SolanaProgramInstance` / `SolanaComponentInstance`, and `composer/pipeline/core.py` calls
   `load_plugins(run, ecosystem.unit_type)`. The front half is landed; the CVLR *backend* is still on
   `eric/solanaProver` and is **not** needed. `certorag:pyproject.toml` pins `ai-composer` at
   `9e64e2e0` — confirm it is an ancestor of a master carrying `SOLANA`, and move it if not.
2. **No composer change is required by Part II.** `ForEcosystem[U]` carries the unit type as a value
   precisely so a plugin can scope itself to one ecosystem.
3. Part I's bundle is a **soft** prerequisite for §9.4: certorag's Solana extractor wants CVLR
   reading knowledge the way its CVL extractor gets `with_cvl_context`. It has landed (§4.1).

## 7. What ports, what changes, what is new

| Layer | File (`certorag:`) | Verdict |
|---|---|---|
| Clone / workspace | `corpus/gh.py` | unchanged |
| Progress, review store | `corpus/progress.py`, `corpus/review.py` | unchanged (one new verdict, §9.5) |
| Workflow provider | `corpus/context.py` | unchanged |
| Property store | `corpus/properties.py` | unchanged — already keys on (repo, branch, hashed path) |
| TUIs | `ui/*` | cosmetic; labels say "contract" |
| Corpus tools | `corpus/tools.py` | docstrings only |
| Target discovery | `scripts/cvl_corpus_audit.py`, `corpus/db.py` | **replaced** for Solana (§9.1) |
| Spec→target resolution | `corpus/spec_inference.py` | **replaced** for Solana (§9.2) |
| Job record | `corpus/models.py::ProverJob` | **new sibling type** (§9.2) |
| Application analysis | `corpus/repo_pipeline.py` | **parameterized** over the ecosystem (§9.3) |
| Spec analysis | `corpus/spec_pipeline.py` + `spec_analysis_system_prompt.j2` | **new prompt**, same graph (§9.4) |
| Mirror schema | `corpus/schema.py` | **new arm** (§8.2) |
| FTS database | `corpus/ragdb.py` | **migrated** (§8.1) |
| Ingestion policy | `scripts/corpus_ftsbuild.py` | extended (§9.5) |
| Probe agent | `plugin/corpus_probe.py` | **genericized** over `U: FeatureUnit` (§10) |
| Plugin + loader | `plugin/corpus_plugin.py` | **second loader** (§10) |
| Probe prompts | `plugin/templates/*.j2` | **new initial prompt**, amended system prompt (§10) |

## 8. The database: one corpus, chain-tagged

**Decision: one database, one FTS index, `chain` as a column — not a second database and not a hard
search filter.**

`PropertyType` (`attack_vector | safety_property | invariant`, `composer/spec/types.py`) is
ecosystem-neutral, and the property descriptions are too by construction — the spec-analysis prompt
forbids mechanism detail. "Deposited liquidity never falls below outstanding shares" is the same
property whether the state is a Solidity storage slot or a Solana account.

This matters more than it would for EVM. The Solana engagement set is ~10–12 projects against an
org-wide Solidity scan, so **cross-chain retrieval is where most of the early Solana value is**. The
probe should not filter by chain; it should *see* the chain on every hit and be told that a
cross-chain analogy needs a stronger justification, because the mechanism differs (accounts vs.
storage, signer checks vs. `msg.sender`, CPI vs. external call). Keep a flag to restrict to
same-chain so the contribution can be measured (§12.5).

### 8.1 Schema changes

`corpus_applications`:

- `+ chain TEXT NOT NULL` — `'evm' | 'solana'`.
- `contract` → `target`: EVM the main contract, Solana the program's `program_identifier`.
- `+ cvlr_version TEXT`, `+ prover_version TEXT`, nullable — **provenance only**, never a ranking or
  gating input (§12.6).

`corpus_components`: `contract_ind` / `comp_ind` / `contract_name` → `outer_ind` / `inner_ind` /
`parent_name`. The index-pair-into-`raw` design already fits: `SolanaComponentInstance` is itself an
index pair `(program, component)` over `SolanaApplication`.

`corpus_properties`: `spec_file` → `source_unit`; `+ scope_caveat TEXT` (§9.4);
`+ attested_verdict TEXT` (§9.5).

Ids are serials assigned at ingest, already documented as unstable across rebuilds, and
`scripts/corpus_ftsbuild.py` rebuilds applications wholesale from the langgraph store. **The
migration is therefore "drop the tables and rerun stage 4"** — no backfill. Do it while the only
content is EVM, and prove it is a no-op by rebuilding and diffing a probe run.

### 8.2 The mirror schema

`corpus/schema.py` exists because composer's live models keep gaining required fields and snapshots
must never be validated against them. That discipline carries over verbatim. Add a second arm
mirroring `SolanaApplication` (`composer/spec/solana/model.py`):

```
CorpusSolanaApplication   application_type, description, components
CorpusSolanaProgram       name, program_identifier, description, instructions,
                          account_types, components
CorpusInstruction         name, description, accounts[AccountConstraint], signers, cpis, requirements
CorpusProgramComponent    name, description, instructions[names], account_types[names],
                          interactions, requirements
CorpusSolanaAuthority     name, description, assumptions
```

A **tagged union across chains discriminated by the `chain` column**, not one struct with optional
fields: each arm carries only what its own render reads. Two Solana specifics:

- `ProgramComponent.instructions` are *names* resolving into the owning program. The live
  `_solana_validate` guarantees resolution; **a snapshot does not**, so the mirror must drop dangling
  names rather than raise — mirroring `SolanaComponentInstance.instructions`, which filters with
  `if n in by_name`.
- The Solana model carries **no file paths**. EVM's `_resolve_main_contract` matches on `con.path`;
  Solana matches on `program_identifier` (§9.3).

`ApplicationDetail.render()` / `ComponentDetail.render()` become per-chain. The Solana render must
show instructions with their account roles and program-enforced constraints — that is where the
Solana-native risks live (missing signer, missing owner check, PDA substitution) and it is what an
analogy most needs to see.

## 9. Extraction: the Solana front end

### 9.1 Target discovery — adopt the inventory, drop the org scan

`scripts/cvl_corpus_audit.py` scans a GitHub org for Solidity repos with a `certora/**/*.spec`
branch, newest first. **Do not port it.** Three reasons:

1. A Solana project has **two branches** — the program's `code_branch` and the spec's `spec_branch`
   — plus a `merge_base`. The spec branch head is routinely *older* than the code branch head; the
   manifest for Project A carries exactly that warning. Pairing a spec with today's mainline reads it against
   a tree it was never written for.
2. **Branch names lie in both directions.** The inventory note for Project B records a branch
   named for a 2024 release date, whose head is nearly two years newer than that date and whose
   spec content was last touched in between, while its `code_branch` stopped at the release. A date
   heuristic gets that wrong twice.
3. There is no `*.spec` to key on — a CVLR spec is Rust under `certora/`, and the real discriminator
   is "does the lockfile resolve `cvlr`".

The capture survey already solved this, in `certora-cvlr-kb`: `projects/inventory.yaml` (curated,
~10 projects, with client, chain, disclosure, both branches) plus `tools/locate.py`, which emits per-project manifests under
`projects/manifests/` naming confs, rules files, env files, `expected.json`s, munge set, build
scripts and resolved revisions. **Adopt it.** The audit sqlite db's only job — a resumable,
deduplicated target list — is served by the manifest set, and `ProcessedRepos` already handles
resumption on the extraction side.

One inventory entry has `code_branch: ~` and the inventory says extraction must not
guess. The manifest reader skips it loudly rather than defaulting to `main`.

### 9.2 The extraction unit is the rules module, not the conf

A CVL conf's `verify: "Contract:path/to/spec"` gives the unit and its target in one string. CVLR
confs give neither — they give a rule list, a `msg`, cargo features and prover args:

```json
{ "rule": ["solvency_on_init", "solvency_on_interest_accrual", "..."],
  "msg": "Solvency - deposits and withdrawals correctly change the sum",
  "cargo_features": ["certora-summarize-liquidation"] }
```

The tempting move is to make the conf the unit. **Don't.** A conf is a *run configuration* and
projects split runs for timeout reasons: Project A ships three `…_part1/2/3.conf` files against a
single rules file. Grouping by conf shatters one property family across three units, each seeing a
third of its rules. Layout varies too much to rely on anyway — Project A puts confs under
`src/certora/confs/`, Project B under `certora/CI_tests/<rule>/` with **one conf per rule**, Project
C under `<program>/certora/CI_tests/rulesN/`.

**The unit is the rules module** — the file (or directory) under the project's rules root. It is the
direct analog of a `.spec` file: hand-authored, one coherent property family, stable across layouts.
Project A's solvency module opens with a module doc comment that *is* the property statement,
exactly as a CVL rule's comment is.

Conf data attaches as **side-band** on the rules it names:

| Conf field | Use |
|---|---|
| `rule[]` | the join key: rule name → conf |
| `msg` | a weak, free prior on the title. Genuine sometimes ("Solvency: price update"), boilerplate often ("<project> rules I", "Certora Verification Rules"), absent in Project B. **A hint, never the property.** |
| `cargo_features` | the summary/mock configuration the proof ran under — feeds `scope_caveat` (§9.4) |
| `loop_iter`, `optimistic_loop`, `prover_args` | provenance; `optimistic_loop` bears on scope |
| sibling `expected.json` | the attested verdict per rule (§9.5) |

So `ProverJob` gets a sibling rather than a widened field:

```python
@dataclass(frozen=True)
class CvlrRuleGroup:
    module_path: str            # the rules module — the unit
    program_identifier: str     # the program under verification
    program_root: str           # the crate dir, for source-tool scoping
    rules: list[RuleRef]        # name, defining file, whether macro-generated
    confs: list[ConfRef]        # path, msg, cargo_features, expected verdicts
```

Discovery (`certorag:corpus/solana/discovery.py`, no LLM) lifts four already-exercised parsers from
`certora-cvlr-kb:tools/extract.py` rather than rewriting them:

- `parse_conf` / `strip_json_comments` / `drop_trailing_commas` — Certora confs are JSON with `//`
  comments and trailing commas; 152 of 486 surveyed confs need this.
- `extract_rules` — `#[rule]` → function name, body, macro-call counts.
- `extract_parametric` — `cvlr_rules! { ... }`, which declares rules that exist at no call site.
- `extract_verdicts` — `expected.json` → `{rule: "SUCCESS" | …}`.

**Known hard case: macro-generated rules.** In Project A, one invocation of a project-local macro
(`<name>_rules! { <Prop>, <rule>, … }`) generates 22 rule functions no scan finds by name. Discovery must
**record** "this conf names rules resolving to no `#[rule]`" rather than drop them, and hand the
agent the module plus its macro definitions — the same escape hatch the CVL pipeline already relies
on (`grep_files`, `code_explorer`). Do not try to expand macros.

### 9.3 Application analysis

`_analyze_application` is already close to ecosystem-generic; it hardcodes five EVM things, and the
`Ecosystem` seam already carries every one:

| Today | Solana |
|---|---|
| `ty=SourceApplication` | `ecosystem.system_model` → `SolanaApplication` |
| `expected_main_id=SolidityIdentifier(name)` | the program's `program_identifier` (a `RustIdentifier`) |
| `EVM.analysis_prompts` | `ecosystem.analysis_prompts` |
| `EVM.validate_analysis` | `ecosystem.validate_analysis` |
| `_resolve_main_contract` by `con.path`, then `Harness`-suffix fallback | match by `program_identifier`; no path on the model, no harness-suffix convention |

Two decisions this raises:

**Which tree to analyze.** The spec-branch tree contains the munge (verification's edits to
production code), the `certora/` scaffolding, mocks and nondet impls; the merge-base tree is the
clean program. **Analyze the spec-branch tree, with `forbidden_read` extended to hide the `certora/`
subtree and the mocks.** You want the program *as verified* — the munge is part of what was proven
about — but the description must not present harness scaffolding as the protocol. This is the direct
analog of EVM's `Harness` handling, which `_resolve_main_contract` and the spec-analysis prompt
already treat as first-class. The `RUST` facet's default `forbidden_read`
(`target/`, `.git`, `node_modules/`, `*.lock`) is the base to extend.

**Multi-program repos.** Project A, Project C and the reference project are Cargo workspaces with several programs. One
application analysis per (repo, spec_branch, program), shared by every rules module targeting that
program — exactly as `_analyze_specs` shares one application task per `contract_name` today.

### 9.4 Spec analysis — a new prompt, the same graph

`spec_pipeline.py`'s graph structure is ecosystem-neutral and should be reused as-is: rough-draft
tools, a result validator against the application's component set, memory, source tools, and the
non-slot-holding cache probe and app-task await (whose deadlock reasoning is documented on
`get_task_spawner` and still applies). Three things change:

1. **`with_cvl_context` → `with_cvlr_facts`** (§4.1). This is where the two halves of this
   document meet: the *practice* bundle is what lets the *property* extractor read the CVLR in front
   of it. It takes the facts without the recipe index, because it holds no recipe tool.
2. **A new `cvlr_spec_analysis_system_prompt.j2`.** Most of the existing prompt survives verbatim —
   the three property categories, the good/bad examples, "describe the property, not the spec", the
   draft/review/deliver procedure. What is replaced is Step 1 ("Understand Spec"), because CVLR has
   no rule/invariant/`require`/ghost grammar:
   - rules are `#[rule]` Rust functions, or generated by `cvlr_rules!` / project-local macros;
   - `cvlr_assume!` plays `require`'s role and carries the same "sane state vs. scenario setup"
     distinction the existing prompt already teaches;
   - `nondet()` and `Nondet` derives are the symbolic inputs;
   - `clog!` is diagnostic instrumentation and says nothing about the property — the direct analog of
     "do not describe the CVL";
   - the supporting infrastructure to inspect is **mocks, `cvlr_inlining.txt` / `cvlr_summaries.txt`
     entries, and `cargo_features`** — not ghosts and hooks.

   `TargetedPropertyFormulation.components` must name `ProgramComponent`s of the main program (the
   existing validator already checks membership — it needs the Solana component set), and
   `rules_and_invariants` becomes the `#[rule]` function names.

   **Carry over the no-quoting constraint explicitly.** The corpus never stores code, and the CVL
   prompt achieves that by forbidding descriptions of the spec. Do not let that drop out of the
   rewrite: it is what keeps client CVLR out of the corpus by construction.

3. **A new field: `scope_caveat`.** This is the one genuine schema addition, and CVLR forces it.

   A CVL spec's summaries are visible in the spec file. A CVLR proof's scope is set by a *cargo
   feature* named in a conf the rules module never mentions: Project A proves solvency under a
   feature that replaces liquidation with a partial summary. "The reserve stays solvent" and "the
   reserve stays solvent when liquidation is replaced by a partial summary" are different claims,
   and the second is what was proved. Without a field for it the corpus states the first and the
   property-inference agent inherits a claim nobody proved.

   `TargetedPropertyFormulation` gains `scope_caveat: str | None` — what was summarized, mocked,
   feature-gated or assumed away, or `None` if nothing was. Stored, ingested, and rendered in
   `relevant_properties_render.j2`. It is **not** a soundness verdict — the corpus has no authority
   to issue one, and the existing prompt correctly forbids commentary on whether a rule is right. EVM
   extraction can adopt the same field later for `summary` / `NONDET` blocks; it has the same problem,
   less acutely.

### 9.5 Ingestion policy

`scripts/corpus_ftsbuild.py` owns policy. Three additions:

- **Sanity and vacuity rules are not properties.** `is_sanity_spec` skips `*sanity*.spec`; the CVLR
  analogs are a module named `sanity_checks.rs` and rules whose body is `cvlr_satisfy!` /
  `cvlr_vacuity_check!` with no assertion. Skip with a printed note, as today.
- **Attested verdicts.** Solana projects ship `expected.json` per conf — `{"rules": {"name":
  "SUCCESS"}}`. The EVM corpus has no counterpart: this is an *attested* outcome rather than a human
  review verdict. A property whose rules are not all expected `SUCCESS` is ingested with
  `attested_verdict` set and excluded from probe results by default, the same way
  `verdict = 'rejected'` is. Cheap, mechanical, and it removes the class of "property" that is really
  an open finding.
- **A third review verdict.** `Verdict = "accepted" | "rejected"` becomes
  `… | "rescope"` — the property is real but its statement overreaches what the proof covered, which
  is the reviewer's cue to fill `scope_caveat`. The review store already guards index drift by
  recording the property title; nothing else changes.

## 10. The plugin

- **`plugin/corpus_probe.py` becomes generic over `U: FeatureUnit`.** Its only unit-specific
  references are `comp.component.name` (→ `unit.display_name`, already required by `FeatureUnit`) and
  the initial-prompt binding. `CorpusProber`, `_ProbeCache`, `_cache_key`, the id validator and the
  graph are otherwise ecosystem-neutral.
- **A second initial prompt.** `corpus_probe_initial_prompt.j2` includes
  `autoprover/application_context_new.j2`; the Solana one includes
  `autoprover/solana/component_context.j2`, which already renders the component, its instructions,
  each account's roles and program-enforced constraints, and the sibling components. Nothing new to
  write.
- **A second loader**, same entry-point group:
  ```toml
  [project.entry-points."certora.autoprove.plugins"]
  certorag = "certorag.plugin.corpus_plugin:CorpusPluginLoader"
  certorag_solana = "certorag.plugin.corpus_plugin:SolanaCorpusPluginLoader"
  ```
  scoped `ForEcosystem(SolanaComponentInstance)`. **Note the blast radius:** `initialize()` opens the
  corpus database, and the README already records that an unreachable database kills every EVM run at
  plugin load. A second loader extends that to every Solana run. Both loaders share one
  `CorpusFTSDatabase` context rather than opening two pools — and see §12.4.
- **Probe system prompt: two amendments.** Swap contract/component vocabulary for program/component,
  and — the highest-value change in this section — add an explicit instruction about cross-chain
  hits: the corpus spans chains, a hit may come from an EVM project, the property may still transfer,
  and a cross-chain analogy must carry a `why_relevant` naming the shared *behavior* rather than a
  shared mechanism. Surface the chain on every search hit so the agent can act on it.
- **`relevant_properties_render.j2`** gains the source chain and the `scope_caveat`. Its existing
  framing — "proved in another, unrelated project… consider whether it can be adapted, mutatis
  mutandis" — is already exactly what a cross-chain analogy needs.

## 11. What certorag should adopt from certora-cvlr-kb

1. **`projects/inventory.yaml` + `locate.py`** — target discovery with two-branch/merge-base
   discipline (§9.1). Replaces the org scan for Solana outright.
2. **`extract.py`'s deterministic parsers** — conf parsing with comments and trailing commas,
   `#[rule]` and `cvlr_rules!` scanning, `expected.json` reading (§9.2). Lift, don't rewrite.
3. **`sanitize-denylist.txt`** — the blocklist seed for §12.1.

**These live in `certora-cvlr-kb`, which is being retired, so they have to move before it goes.**
certorag is the natural home: it is their only remaining consumer. Until they move, the project
manifest is the interface and should be schema-stable. Tracked as `cvlr-todo.md` U6.

---

# Sequencing and risk

## 12. Risks and open questions

**12.1 Confidentiality, and it is the gating item.** certorag's corpus is not a shipped artifact — it
is an internal Postgres db read at run time — but it holds client application and component
descriptions verbatim and the plugin's whole job is to inject them into a *different* client's
property inference. `certorag:docs/client-name-scrubbing.md` is the designed-but-unimplemented
countermeasure.

Solana sharpens this. The EVM corpus is an org-wide scan where recurrence dilutes any one client; the
Solana corpus is ~10 named, mostly `disclosure: confidential` engagements, several of them the only
lending protocol or the only multisig in the set. A property there is far more traceable to its
source. So: implement the scrubbing sketch (the `post_process_property_inference` hook,
`client_identifiers()`, the lite-tier rewrite, fail-closed) **before** ingesting confidential Solana
content; seed the blocklist from `sanitize-denylist.txt`; and do the complementary input-side change
the sketch recommends (anonymize in `relevant_properties_render.j2`), which is cheaper and strictly
better than a post-hoc rewrite.

**12.2 Macro-generated rules.** Rule name → source is not a scan. Mitigated by handing the agent the
module and its macros, but the validator — which checks that reported rule names are real — has no
cheap ground truth for a generated name. Option: validate against the union of scanned `#[rule]`
names and every name any conf mentions.

**12.3 Corpus size.** ~10 projects makes Postgres FTS ranking noisy — `ts_rank` over a small table
rewards term frequency in ways that do not generalize. Cross-chain retrieval (§8) is the main
mitigation; a second is to lean on `get_application_by_id` browsing rather than search alone.

**12.4 Two loaders, one database, one failure.** Plugin construction opens the db, so installing the
distribution now risks both ecosystems' runs on its availability. Consider making `initialize()`
degrade to a no-op plugin on a connection failure rather than killing the run — arguably already the
right behavior for EVM, and the Solana loader makes it twice as costly not to. Precedent exists:
`composer/tools/rag_env.py` degrades a corpus to no-RAG for exactly this reason.

**12.5 Does cross-chain retrieval actually help?** Asserted here, not measured. Stage P2-6 ships with
a same-chain-only flag so the contribution can be A/B'd on a real Solana run.

**12.6 Vintage does not gate the property corpus.** The capture survey's tiers and currency axis
(§4.4) exist because **API idioms age**. Natural-language properties do not: a 2024
solvency property is still the solvency property. Record `cvlr_version` and prover version as
provenance (§8.1) and stop — no ranking, no gating, no ladder. Two residues, both handled: cited rule
names may use legacy `cvt_*` vocabulary (provenance strings, harmless), and a proof's scope can shift
with prover version (that is `scope_caveat`'s job).

**12.7 Where the inventory and the parsers go.** §11's three files live in a repository that is
being retired. Part II assumes they move to certorag; if they move anywhere else, the manifest
schema is the interface and §9.1–§9.2 are unchanged.

## 13. Sequencing

**Part I — AutoProver.** Done: the bundle (`2ffc1243`), the factored prompt (`f4c953fd`), the other
CVLR agents wired to it, and the recipes (`e04f2dea`). It lands as the landing plan's K1.

**Part II — certorag.** Stage P2-1 is the riskiest structural change and is deliberately first, while
the only content is EVM and a mistake costs one rebuild.

| # | Stage | LLM? | Gate |
|---|---|---|---|
| P2-0 | Move the composer pin to a master carrying `SOLANA` | no | certorag imports; EVM runs unchanged |
| P2-1 | Database migration: `chain` column, generic index pair, mirror union, per-chain render | no | rebuild the EVM corpus; a probe run produces the same hits |
| P2-2 | Solana target discovery: manifest reader + `discover_rule_groups` + lifted parsers | no | every inventory project yields rule groups; macro-generated rules are *recorded unresolved*, not dropped |
| P2-3 | Ecosystem-parameterize `repo_pipeline`; Solana application analysis | yes | the reference project analyzes and validates |
| P2-4 | `cvlr_spec_analysis_system_prompt.j2` + `scope_caveat` | yes | the reference project + Project A extract; read every property by hand before scaling |
| P2-5 | Ingestion policy: sanity/vacuity skip, attested verdicts; review `rescope` | no | corpus builds; property counts are sane |
| P2-6 | Solana plugin loader, probe prompts, cross-chain instruction | yes | a Solana run's inference gets non-empty, non-silly corpus input |
| P2-7 | Scrubbing (§12.1) | yes | **blocks ingestion of confidential Solana content** |

P2-4 is where the money goes. Run P2-3 and P2-4 against the inventory's normative reference project
first, then Project A — the hardest: macro-generated rules, split confs, heavy mocking, and a
summary-gated proof. If the prompt survives Project A it survives the set.

Part I is a soft prerequisite for P2-4 (§6.3), and it has landed on the branch.
