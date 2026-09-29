# Plan — CVLR knowledge: where it belongs, and the Solana property corpus

> Two pieces of work, one document, because the second only makes sense once the first is settled.
>
> **Part I** (work in **AutoProver**) re-homes the knowledge that
> [certora-cvlr-kb](https://github.com/Certora/certora-cvlr-kb) produces. Its 83 abstracted entries
> were routed to the `cvlr_kb` RAG corpus; that destination is wrong. The right one — a CVLR
> knowledge bundle — did not exist when this was written, and has since landed (§13).
>
> **Part II** (work in **certorag**) extends the property corpus — "what did we prove about a
> component like this one" — from CVL/Solidity to CVLR/Solana. This is the one knowledge asset on
> the CVL side with no CVLR counterpart at all.
>
> Prior art, all in this directory: [ecosystem-abstraction.md](./ecosystem-abstraction.md) (the
> seam Part II rides on), [cvlr-capture-plan.md](./cvlr-capture-plan.md) (the procedure
> certora-cvlr-kb implements — Part I revises its §7), [cvlr-backend-plan.md](./cvlr-backend-plan.md)
> (the consumer), [cvlr-todo.md](./cvlr-todo.md) U6 (the repo-boundary question). How Part I
> landed is planned in [cvlr-knowledge-bundle-plan.md](./cvlr-knowledge-bundle-plan.md). Paths below
> are relative to the repository root unless marked `certorag:` or `certora-cvlr-kb:`.

---

## 0. The one-paragraph version

On the CVL/Solidity side, exactly two knowledge assets are machine-derived: the CVL manual (sphinx →
`ragbuild`) and certorag's property corpus. **Every piece of CVL *practice* knowledge — roughly
1,500 lines of it — is hand-authored**, and it is delivered as an always-in-context bundle plus a
trigger-indexed recipe set, not as retrievable corpus prose. The CVLR side already had the
hand-authored equivalent, and more of it: a 754-line authoring system prompt, a written
`backend_guidance`, starting env files, and a dedicated munge-editor prompt. So certora-cvlr-kb's
entries are not missing a home — they are competing with an occupied one, and competing in the one
delivery channel (`cvlr_kb` search) that is explicitly allowed to be absent at run time. What CVLR
genuinely lacked was (a) a *reusable* knowledge bundle — its knowledge was trapped inside one
agent's system prompt, unreachable by the other CVLR agents — and (b) any property corpus
whatsoever.

**Status (2026-09-29).** (a) has landed on `eric/solanaProver`: the bundle exists, the author's
prompt was factored into it, each CVLR agent that can use it reads it, the judge cites it instead
of restating it, and the first two recipes are in. What is left of Part I is more recipes (with
their compile gate) and P1-5 in certora-cvlr-kb (§13). (b) is Part II and has not started.

---

# PART I — Where CVLR practice knowledge belongs

## 1. The CVL knowledge stack, in full

`composer/kb/kb_context.py` assembles four context
documents and injects them into an agent's *initial prompt* via `with_cvl_context`, behind a
`CacheMarker`:

| Layer | Artifact | Lines | Delivery |
|---|---|---|---|
| Baseline facts | `composer/kb/resources/cvl_baseline_facts.md` | 77 | always in context |
| Topic KB — summarization | `composer/kb/resources/cvl_summarization_rag_draft.md` | 939 | always in context |
| Topic KB — invariants | `composer/kb/resources/cvl_invariants_quantifiers.md` | 284 | always in context |
| Situation recipes | `composer/kb/resources/cvl_recipes_index.yaml` + 22 files under `recipes/` | 194 index, ~600 bodies | **index** always in context; body on demand via `get_cvl_recipe` |
| Backend guidance | `CERTORA_BACKEND_GUIDANCE` in `composer/spec/prop_inference.py` | inline | cached system prefix of property inference |
| Reference manual | sphinx → `composer/scripts/ragbuild.py` → RAG db | — | searched (`cvl_manual_search`, `cvl_get_section`) |
| Property corpus | `certorag` | — | analogy probe at property inference |

Two facts about this stack drive everything in Part I.

**Fact 1 — the practice layers are hand-authored, and nothing generates them.** `grep` for
`cvl_baseline_facts` / `cvl_recipes_index` / `kb/resources` across the tree hits exactly two files:
`kb_context.py` and `docs/cvlr-capture-plan.md`. `git log --follow` puts all of it in a single
commit (`b6320237`, "Extended RAG, CVL context"). There is no extraction pipeline, no ranking, no
ledger, no compile gate. An expert wrote it.

**Fact 2 — the bundle is shared across the whole CVL agent family.** `with_cvl_context` has seven
call sites: `spec/cvl_research.py`, `spec/feedback.py`, `spec/cex_remediation.py` (twice),
`spec/natspec/author.py`, `spec/source/author.py`, `spec/source/summarizer.py` — plus an eighth in
`certorag:certorag/corpus/spec_pipeline.py`. One authoring investment, eight consumers. The
knowledge is a *bundle*, deliberately separable from any one agent's prompt.

## 2. What the CVLR side already has

All on `eric/solanaProver`. This is the set this plan started from, before W1–W3. Where a row has
changed since, the change is noted in it.

| Artifact | Size | What it covers |
|---|---|---|
| `composer/templates/cvlr_property_generation_system_prompt.j2` | **754 lines** | Output shape; what a rule is; `clog!` legibility; assumptions and vacuity; nondeterminism as the quantifier; the prover configuration; loop bounds and the `optimistic_loop` ladder; asking the code editor; Anchor entry points and generated names; arithmetic and why panic-freedom is off the menu; the nonlinear-arithmetic ladder; when a rule should fail. *Now 423 lines: W2 moved 388 of them into the bundle (§4.2).* |
| `composer/spec/cvlr/guidance.py` — `SOLANA_CVLR_GUIDANCE` | ~80 lines | `backend_guidance` for property inference |
| `composer/spec/cvlr/envs/` | 154 + 108 lines | `cvlr_inlining_core.txt`, `cvlr_summaries_core.txt` + anchor variants: the **starting layers** of the two tuning files. Vendored from upstream when this plan was written; maintained in AutoProver since `67038c6d`, which deleted the refresh script and the `PROVENANCE` stamp. |
| `composer/spec/cvlr/tuning.py` | — | How each tuning file is layered: the starting layers, a per-unit layer, and generated composites that are never hand-edited. `summarize_for_prover` writes the author's points-to summaries into the unit layer. |
| `composer/spec/cvlr/env_paths.py` | — | Rewrites the starting layers into the target's platform-generation path spelling when a composite is built |
| `composer/templates/cvlr_munge_editor_system.j2` / `_review_system.j2` | 208 / 103 | The munge editor's charter and its reviewer |
| `composer/templates/cvlr_property_judge_system_prompt.j2` | — | The feedback judge |
| `composer/tools/cvlr_rag.py` + `KNOWLEDGE_BASES["cvlr_kb"]` | — | Search over `cvlr_kb`, which since `f1da5e4f` is the Solana manual and nothing else |
| `composer/tools/cvlr_api_rag.py` + `KNOWLEDGE_BASES["cvlr_api_kb"]` | — | *Added since:* the CVLR API, generated from the crates' rustdoc (`docs/cvlr-api-docs-plan.md`) |
| `composer/spec/cvlr_research.py` | — | *Added since:* a research sub-agent that holds the search tools for both corpora and is told which one wins. The author and the judge get `cvlr_research`, not the search tools. |

Three properties of this set matter.

**The guidance is written from the manual, not from the engagements.** `guidance.py`'s own docstring:
*"Everything asserted here is traceable: the Methodology chapter of the published Solana manual… Where
the manual and the surveyed projects disagree, the manual wins — see `docs/cvlr-capture-plan.md` §4.5
for why recurrence alone is the wrong axis."* The backend has already decided that recurrence across
engagements does not outrank the manual.

**The starting env files are data, not advice.** The interesting problem there is not *which*
symbols to inline — that list was upstream's, and is now maintained in place under
`composer/spec/cvlr/envs/` — it is that a directive written in the monolith's spelling
(`solana_program::account_info::AccountInfo`) silently matches nothing on `solana-program` 2.2+,
which is what `env_paths.py` exists to fix. The author's only write into the tuning files is a
points-to summary in its own unit layer (`summarize_for_prover`). It cannot add inlining directives,
and it never edits the starting layers.

**The `cvlr_kb` corpus is explicitly degradable.** `composer/tools/rag_env.py`:
*"A search aid must never fail a run over that, so `build_rag_tools` degrades to **no RAG** (the
static cheat-sheet in the prompt suffices)."* Knowledge that lives only in `cvlr_kb` is knowledge
that may simply not be present for a run. Anything load-bearing must be in the prompt or the bundle.

## 3. The consequence for the 83 entries

`certora-cvlr-kb:entries/` holds 27 `rule_shape`, 18 `prover_flag`, 17 `conf_key`, 11 `env_inlining`,
6 `munge_form`, 3 `env_summaries`. Against §2:

| Kind | n | Status |
|---|---|---|
| `prover_flag` | 18 | **Outside the action space.** `adjust_prover_config` moves exactly two settings: the loop bound and `optimistic_loop`. (The nonlinear solver portfolio was the second setting when this plan was written. `ec01e2f9` removed it, and `3f3c84c1` added `optimistic_loop`, with the prompt warning that it changes what a verdict means.) The prompt says the rest are "not yours" and to `record_skip` with something actionable instead. And the run's actual conf is injected verbatim — `{{ conf \| tojson(indent=2) }}` — so "read it rather than assuming a default" is already the instruction. |
| `conf_key` | 17 | Same, plus: several are `status: quarantined` on a ledger question the prompt has already answered. `conf-key-setting-optimistic-loop-explicitly-in-every-conf` is quarantined "because the pass reports that this changes what the program means" — which is precisely the prompt's position, stated as policy. |
| `env_inlining` | 11 | **Mostly restating the starting layers.** The real problem is the path spelling, which `env_paths.py` owns and no entry addresses. Inlining directives are not in the authoring agent's action space. One exception, noticed late: "force-inline `AccountInfo::realloc`" is **not** in `cvlr_inlining_core.txt`, as this row used to say. `cvlr_summaries_core.txt` summarizes it as an opaque call instead. That is a question about our starting layer, not an entry to retire, and it is lead L1 in `cvlr-todo.md`. |
| `env_summaries` | 3 | **Restating the starting layers**, like `env_inlining`. Summaries are in the author's action space (`summarize_for_prover` writes them into the unit's own layer, §2), so these were triaged rather than retired unread. All three are already lines in `cvlr_summaries_core.txt`: `^memhavoc_c$`, the soft-float intrinsics (`^__muldf3$` and its siblings), and `std::io::error::Error::new`. No author will need to write them. |
| `munge_form` | 6 | A dedicated munge editor agent with a 208-line charter already owns this. |
| `rule_shape` | 27 | **The survivors** — and even these overlap "What a rule is", "Assumptions are the sharpest tool", "Nondeterminism is the quantifier". |

This is not a failure of the capture pipeline. It is what happens when a machine pass and an expert
work the same seam in parallel and the expert finishes first. The pipeline's *evidence* is still
good; its *entries* are the wrong output shape.

## 4. Decision

**The deliverable of certora-cvlr-kb is the ledger and the evidence behind it, not the entries.** The
CVL precedent is unambiguous: a human writes the practice knowledge. certora-cvlr-kb's value is
telling that human what to write about and what the field actually does, with recurrence counts and
divergences attached. Entries become evidence hung off questions rather than rows in a corpus.

Four work items follow, three in AutoProver and one in certora-cvlr-kb.

### 4.1 W1 — Factor a CVLR knowledge bundle (`with_cvlr_context`)

**This is the gap, and it exists independently of anything certora-cvlr-kb produces.** CVL's
knowledge is a bundle with eight consumers; CVLR's is trapped inside
`cvlr_property_generation_system_prompt.j2`. The munge editor, the property judge, the feedback
loop, the cex analyzer and (Part II) certorag's Solana extractor cannot reach a word of it. Each
either restates it or goes without.

Generalize `composer/kb/kb_context.py`. Everything in
it is chain-neutral except four things: the `RecipeChannel` literal, `KB_TOOL_NAME`, the `_INDEX`
list of `ContextSpec`s, and the two resource filenames. Introduce a `KnowledgeBundle` record
carrying exactly those, with two instances — `CVL_BUNDLE` and `CVLR_BUNDLE`. **Two instances of one
record, not a flag or an optional-field struct**: each bundle carries its own channel vocabulary and
its own resource set, and nothing is meaningful-only-when.

New files:

```
composer/kb/resources/cvlr_baseline_facts.md      # W2 fills this
composer/kb/resources/cvlr_recipes_index.yaml     # W3 fills this
composer/kb/resources/recipes/cvlr-*.md
composer/templates/cvlr_kb_index.j2               # or parameterize cvl_kb_index.j2
```

New API: `with_cvlr_context(prompt)` and `cvlr_kb_tools()` (tool name `get_cvlr_recipe`). Wire into
the CVLR author, the munge editor, the property judge and the feedback graph. Note that
`composer/spec/services.py::build_basic_rag_tools` bundles `kb_tools()` with the *CVL* manual tools;
the CVLR side goes through `composer/tools/rag_env.py` instead, so the recipe tool must be added
there rather than inherited.

**As built** (`560e2327`, `0ad9d826`, `0cd4c530`). The design above held, with four differences:

- The recipe half is its own record. `KnowledgeBundle[C]` carries a label, its `ContextSpec`s, and
  an optional `RecipeSet[C]` (tool name, index resource, parser, index template). A bundle without
  recipes has none; there is no empty index.
- The channel vocabulary is a type parameter. `KBRecipe[C]` / `IndexModel[C]` validate each index
  against its own family's `Literal` (`CVL / CONF / EDIT` for CVL), so a CVLR recipe cannot name a
  CVL channel. The CVL index always had a legend explaining its channels, but it was never
  rendered, so an agent saw a channel name with nothing saying what it meant. Both indexes'
  legends now render with the index. For CVL that is the one deliberate change to the rendered
  prompt; everything else in P1-1 was a no-op.
- Both families share one index template, `kb_index.j2`, parameterized by label and tool name.
  There is no `cvlr_kb_index.j2`.
- `kb_tools(bundle)` replaces a separate `cvlr_kb_tools()`. The CVLR recipe tool is added in
  `composer/spec/cvlr/entry.py`, next to the `cvlr_research` sub-agent, not in `rag_env.py`.
  `rag_env.py` only builds corpus search tools, and those now go to the researcher rather than to
  the author.

Consumers, as P1-3 left them. Each reader gets what it can act on, which is why there are two
bundles. `with_cvlr_context` (`CVLR_BUNDLE`, facts plus the recipe index) is the initial prompt of
the author and the judge in `cvlr/author.py`, the two agents holding `get_cvlr_recipe`.
`with_cvlr_facts` (`CVLR_FACTS_BUNDLE`, the facts alone) is for the munge reviewer and
`cvlr_research`, which have no recipe tool. The index tells its reader to retrieve a file, and its
channels name the author's tools. The munge editor gets no bundle at all: its charter forbids
reasoning about the Prover's internals, which are the bundle's subject, and the legend's `EDIT`
channel told it to hand the problem to `code_editor`, which is itself. The counterexample analysis
needs no wiring of its own. `TrivialFanoutCexHandler` analyzes a
counterexample over the author's live message list, and the bundle is already at the head of that
list.

### 4.2 W2 — Populate the bundle by factoring the 754-line prompt

Do not write `cvlr_baseline_facts.md` from scratch and do not write it from the entries. Split
`cvlr_property_generation_system_prompt.j2` along one line:

- **Stays in the prompt** — this agent's contract: output shape, the `put_harness` / `verify_rules` /
  `record_skip` / `result` tool list, the two publish stamps, the workflow.
- **Moves to the bundle** — CVLR and prover facts true for any CVLR agent: what a rule is, assumptions
  and vacuity, nondeterminism as the quantifier, loop bounds and the `optimistic_loop` ladder,
  arithmetic and panic-freedom, the nonlinear ladder, Anchor entry points and generated names,
  `clog!` legibility.

The test that the split is right: the munge editor and the judge stop restating what the author
knows. Moving text costs nothing in tokens while one agent reads it; the point is that five others
then can.

**As built** (`0ad9d826`). 388 lines moved into `cvlr_baseline_facts.md`, and the prompt went from
754 lines to 423. The line moved in two places:

- **Anchor entry points and generated names stayed in the prompt.** The worked example runs through
  that section, and the worked example is per-run material. The bundle is a cached prefix and cannot
  carry anything that varies by run. The same reasoning keeps the injected conf and the worked
  example in the prompt.
- **The pointer-analysis material moved**, although the list above did not name it: the section
  "When the Prover cannot follow the program's own data" and its four symptoms. That material is
  true for any CVLR agent, which is the rule that decides what moves.

The knowledge tests (`tests/test_cvlr_knowledge.py`, `tests/test_cvlr_bundle.py`) read the bundle
and the prompt together. What they assert is what the author knows, not which file says it.

P1-3 met the test in the first paragraph above in its intended sense: the munge editor no longer
reads the bundle, and the judge cites the bundle instead of restating it. It did not always make
the prompts shorter (§13).

**certora-cvlr-kb's role here is review input, not source text.** Run the entry set against the
factored document and ask, per entry, one of: *the document already says this* (drop), *the document
is silent and N projects agree* (a ledger question, addressed to the document's author), *the
document takes a position the field contradicts* (the highest-value output the capture pipeline can
produce — surface it, do not silently overrule the manual, per `guidance.py`'s rule).

### 4.3 W3 — CVLR recipes, with the channel vocabulary taken from the action space

Recipes are the right shape for the surviving `rule_shape` material: a cvlr-kb entry already carries
`id / title / trigger / pattern / example`, which is near field-for-field `KBRecipe` plus a markdown
body.

Two corrections to `cvlr-capture-plan.md` §7.2 before writing any:

**The trigger vocabulary is wider than the plan allows.** §7.2 requires a trigger be "an observable
symptom" and says a trigger "phrasable only as a situation belongs in an entry". The CVL recipes it
cites as the format to follow exactly do not obey that: R1's trigger is *"Struct annotated
`@custom:storage-location erc7201:...` plus an assembly slot getter"* (a code situation) and R18's is
*"reentrancy safety to verify"* (a verification goal). Adopt the CVL-actual vocabulary — symptom,
code situation, or verification goal. Keep §7.2.1's separate rejection of *reference prose*
(the docs run offering "Overview" and "Advanced Topics" as recipes); that filter was right and is
about something else.

**Derive the channels from the tool list, not from the capture data.** §7.2 proposed
`RULE / MOCK / GATE / ENVFILE / CONF / SCAFFOLD` from the artifact taxonomy. A channel's job is to
tell a stuck agent whether the fix is in its action space — and that action space is now literally
enumerable from the author's tool list:

| Channel | Tool | Note |
|---|---|---|
| `RULE` | `put_harness` | the default |
| `MOCK` | `summarize_for_prover` | unsound by construction; invalidates the prover stamp |
| `EDIT` | `code_editor` | the agent describes the problem, not the edit |
| `CONF` | `adjust_prover_config` | **only** the loop bound and `optimistic_loop` (which changes what a verdict means); anything else is a `record_skip` |
| `SKIP` | `record_skip` | the honest terminal channel — no CVL peer, and worth having |

`ENVFILE` and `SCAFFOLD` are **not channels**. The starting env files are maintained in AutoProver
and rendered by `env_paths.py`, and the composites are generated. The one tuning-file write the
author has, a summary in its unit layer, is `MOCK`. Scaffolding happens in a separate phase. A
recipe whose action is "add an inlining line to `cvlr_inlining.txt`" points at something this agent
cannot do. Keep the §7.2 naming lesson (`ENV` lost to the word it looks like) — it applies to any
short channel name.

**As built** (`0cd4c530`). The five channels are exactly the ones in the table, as `CvlrChannel` in
`kb_context.py`. Each channel's meaning is in `cvlr_recipes_index.yaml` and is rendered with the
index. The 27 `rule_shape` entries were triaged against the W2 document, and two recipes survived:

- **K1** — satisfy rules for reachability and witnesses, and the acceptance property they must not
  stand in for. The prompt and the judge mention `cvlr_satisfy!` only as a thing not to do. Its
  legitimate uses are attested in 4 projects and were written down nowhere.
- **K2** — `clog!` before an assertion over an enum or a bitfield. The corpus records this as a
  solver workaround, not a legibility habit: logging gives the value a use site, which stops the
  comparison chain being folded into a bitmask. The bundle's `clog!` section says nothing about it.

Most of the other entries are the nondet / assume / act / assert skeleton the bundle now states
outright, and were dropped. One was dropped for a reason worth keeping: "end every rule with a
vacuity check" is attested but does not transfer. `cvlr_vacuity_check!` is gated on a cargo feature
AutoProver never enables. The 3 `env_summaries` entries were triaged too, and none survived (§3).

### 4.4 W4 — In certora-cvlr-kb: retire, re-aim, keep

| Asset | Disposition |
|---|---|
| `entries/` `prover_flag`, `conf_key`, `env_inlining`, `env_summaries`, `munge_form` (55) | **Retire as corpus content.** Keep the extracted rows and recurrence counts as ledger evidence against the W2 document. |
| `entries/` `rule_shape` (27) | Triage against the factored document (W2); what survives becomes a W3 recipe. Done: two survived, as K1 and K2 (§4.3). |
| `ledger/` + `gen_ledger.py` | **Promote to the primary deliverable.** Re-aim the questions at the W2 document rather than at entry status. |
| `cvlr-crates.rag.json` + `crate_reference.py` + `crate_inventory.py` | **Retire as corpus content** (revised 2026-09-29; this row previously read "unaffected and keep"). Superseded by AutoProver's `cvlr_api_kb`, generated from rustdoc rather than from a regex scan plus a model — see [cvlr-api-docs-plan.md](./cvlr-api-docs-plan.md) §4.2. The reasoning here was sound and is simply out of date: reference *does* belong in a corpus, but there is now a better-produced one, and it is a **separate** corpus, so keeping this would put two crate references in front of one agent with no way to tell which is current. `cvlr_kb` is the manual and nothing else as of that plan's step 6. **`certora-cvlr-kb:tools/publish.py` still hard-codes `KNOWLEDGE_BASE = "cvlr_kb"`**, so this is the remaining path by which retired content can reach the corpus — AutoProver stopped *discovering* these manifests, it cannot stop them being pushed. |
| `projects/inventory.yaml`, `locate.py`, `extract.py`'s parsers, `sanitize-denylist.txt` | **Unaffected, and Part II consumes them** (§10). |
| `compile_gate.py`, `publish_gate.py`, `validate_manifest.py`, `publish.py` | Follow whatever survives. The compile gate still earns its keep for W3 recipe examples, which carry code. |
| `rank.py`, `template.py`, tiers, currency gaps | Keep for the ledger's prioritization; they were always about *which question to ask first*, which is the deliverable now. |

---

# PART II — The Solana property corpus (certorag)

> The engagements Part II draws on are client work, and most are confidential, so they are named
> here by role — *the reference project*, *Project A/B/C* — rather than by client or repository.
> The labels are this document's own. certora-cvlr-kb's `projects/inventory.yaml` lists the projects.

## 5. Why this is a different thing, and is not affected by Part I

| | certorag | certora-cvlr-kb |
|---|---|---|
| Captures | **what was proven** | **how it is expressed and operated** |
| Unit | a natural-language property attributed to a component of an application | an idiom / a recipe / a baseline fact |
| Retrieved by | analogy — "this component resembles that one" | keyword, or a trigger match |
| Injected at | **property inference**, per component, via `property_inference_input_hook` | authoring, via the bundle and the recipe index |
| Ages? | no — a solvency property is a solvency property | yes — which is why W2's document needs an author and a ledger |
| Contains code? | never; prose only | yes, synthetic and compiled |

certorag's spec-analysis prompt spends four paragraphs forbidding the agent from describing *how* a
property was expressed. That forbidden residue is Part I's whole subject. The two are orthogonal.

A CVLR run today gets no corpus analogies at all, while an EVM run gets them from an org's worth of
Solidity engagements. Nothing in AutoProver or certora-cvlr-kb closes that.

## 6. Preconditions

1. **The composer pin must carry the `SOLANA` ecosystem.** `composer/pipeline/ecosystem.py` on
   AutoProver `origin/master` already defines `SOLANA = RUST ⊕ solana` with `SolanaApplication` /
   `SolanaProgramInstance` / `SolanaComponentInstance`, and `composer/pipeline/core.py` calls
   `load_plugins(run, ecosystem.unit_type)`. The front half is landed; the CVLR *backend* is still on
   `eric/solanaProver` and is **not** needed. `certorag:pyproject.toml` pins `ai-composer` at
   `9e64e2e0` — confirm it is an ancestor of a master carrying `SOLANA`, and move it if not.
2. **No composer change is required by Part II.** `ForEcosystem[U]` carries the unit type as a value
   precisely so a plugin can scope itself to one ecosystem.
3. Part I's W1 is a **soft** prerequisite for §9.4: certorag's Solana extractor wants CVLR reading
   knowledge the way its CVL extractor gets `with_cvl_context`. Without the bundle it works, with a
   weaker prompt.

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

certora-cvlr-kb already solved this: `projects/inventory.yaml` (curated, ~10 projects, with client,
chain, disclosure, both branches) plus `tools/locate.py`, which emits per-project manifests under
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

1. **`with_cvl_context` → `with_cvlr_context`** (Part I W1). This is where the two halves of this
   document meet: the *practice* bundle is what lets the *property* extractor read the CVLR in front
   of it. If W1 has not landed, fall back to `cvlr_rag_tools` alone and accept a weaker prompt.
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

If the two repos merge (`cvlr-todo.md` U6), these are the files that move. If they stay apart, the
project manifest is the interface and should be schema-stable.

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

**12.6 Vintage does not gate the property corpus.** certora-cvlr-kb's tiers, `cvlr_observed` ranges
and currency gaps exist because **API idioms age**. Natural-language properties do not: a 2024
solvency property is still the solvency property. Record `cvlr_version` and prover version as
provenance (§8.1) and stop — no ranking, no gating, no ladder. Two residues, both handled: cited rule
names may use legacy `cvt_*` vocabulary (provenance strings, harmless), and a proof's scope can shift
with prover version (that is `scope_caveat`'s job).

**12.7 The repo boundary is still open.** `cvlr-todo.md` U6 asks whether the practice corpus moves
into certorag, whether certorag adopts the manifest-and-importer split, and if they stay apart what
the boundary is. This document does not decide it. It assumes they stay apart and names §11's three
files as the interface.

**12.8 Does Part I shrink `cvlr_kb` to the crate reference and the manual?** *Answered, and the
answer moved:* it shrinks to **the manual alone**. This asked whether retiring the practice entries
(§4.4) would leave the manual plus `cvlr-crates.rag.json`, and judged that a coherent corpus. Since
then AutoProver generated its own crate reference from rustdoc into a separate corpus
(`cvlr_api_kb`) and re-scoped `cvlr_kb` down to the manual, so the crate reference leaves too
(§4.4, revised).

Two consequences worth keeping. The producer/importer split of `rag-import-format.md` now serves
*two* producers rather than one, but they live in different repositories and fill different
corpora — which is what makes the authority ordering between them expressible at all. And when
AutoProver checked its live database on 2026-09-29, `cvlr_kb` already held nothing but the manual:
neither the crate reference nor the practice entries had been ingested there. The shrink this
question anticipated had, in practice, already happened.

## 13. Sequencing

**Part I — AutoProver.** W1 and W2 are one change and should land together: a bundle with nothing in
it proves nothing, and factoring the 754-line prompt is only safe if the factored halves are wired at
the same time.

| # | Stage | Gate | Status (2026-09-29) |
|---|---|---|---|
| P1-1 | Generalize `kb_context.py` into `KnowledgeBundle`; `CVL_BUNDLE` is a no-op refactor | every existing CVL agent's rendered prompt is byte-identical | **Done**, `560e2327`. P1-4 later made one deliberate change to what CVL agents see: the channel legend now renders (§4.1). |
| P1-2 | Factor `cvlr_property_generation_system_prompt.j2` → `cvlr_baseline_facts.md` + a slimmer prompt; add `with_cvlr_context`; wire the author | the CVLR authoring run is unchanged in behavior | **Done**, `0ad9d826`. 754 → 423 prompt lines + 388 bundle lines. Two places where the split differs from §4.2 are recorded there. |
| P1-3 | Wire the bundle into the other CVLR agents that can use it; delete what they restate | no fact is stated in two places, and every citation of a bundle section names a heading the bundle has; no new knowledge is invented | **Done.** The judge's items 2, 5 and 7 cite the bundle's sections and keep only the reviewer's checks. Item 5 gained the `optimistic_loop` checks, and item 7 gained recipe K2's case. The judge's input shows `optimistic_loop` with the author's `why` (`HarnessAssumptions.settings`). The munge reviewer and `cvlr_research` get the facts without the recipe index; the munge editor gets no bundle (§4.1). A test checks each cited heading against the bundle. The gate originally read "each prompt shrinks"; item 5 grew, because it gained checks it had been missing, so the gate now names what it was for. |
| P1-4 | `cvlr_recipes_index.yaml` + `get_cvlr_recipe`, channels from §4.3's table | an index with real triggers; recipe bodies compile under the §4.4 gate | **Started**, `0cd4c530`. The index, the tool, the channels and two recipes (K1, K2) are in. The compile half of the gate has not run: nothing compiles the recipe examples. Every entry has now been triaged: of the 27 `rule_shape` entries two survived, and the 3 `env_summaries` entries all restate the starting layers (§3). |
| P1-5 | In certora-cvlr-kb: retire 55 entries to ledger evidence; re-aim the ledger at the P1-2 document | the ledger's questions name sections of that document | Not checked from AutoProver. |

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

The two parts are independent except that P1-2 is a soft prerequisite for P2-4 (§6.3). They can run
in parallel.
